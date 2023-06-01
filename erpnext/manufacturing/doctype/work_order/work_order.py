# Copyright (c) 2015, Frappe Technologies Pvt. Ltd. and Contributors
# License: GNU General Public License v3. See license.txt

import frappe
from frappe import _
from frappe.utils import flt, getdate, cint, nowdate, get_link_to_form, round_up
from erpnext.manufacturing.doctype.bom.bom import validate_bom_no, get_bom_items_as_dict
from erpnext.stock.doctype.item.item import validate_end_of_life
from erpnext.stock.get_item_details import get_conversion_factor
from erpnext.stock.stock_balance import get_planned_qty, update_bin_qty
from erpnext.stock.utils import get_bin, validate_warehouse_company, get_latest_stock_qty
from erpnext.utilities.transaction_base import validate_uom_is_integer
from erpnext.controllers.status_updater import StatusUpdater
from frappe.model.mapper import get_mapped_doc
import json
import math
import copy


class OverProductionError(frappe.ValidationError): pass
class StockOverProductionError(frappe.ValidationError): pass
class OperationTooLongError(frappe.ValidationError): pass
class ItemHasVariantError(frappe.ValidationError): pass


form_grid_templates = {
	"operations": "templates/form_grid/work_order_grid.html"
}


class WorkOrder(StatusUpdater):
	def get_feed(self):
		return "{0} {1} of {2}".format(
			frappe.format(self.get_formatted('qty')), self.get('stock_uom'), self.get('item_name') or self.get('production_item')
		)

	def onload(self):
		ms = frappe.get_cached_doc("Manufacturing Settings", None)
		self.set_onload("material_consumption", ms.material_consumption)
		self.set_onload("backflush_raw_materials_based_on", ms.backflush_raw_materials_based_on)

		self.set_available_qty()

	def validate(self):
		self.validate_qty()
		self.validate_operation_time()
		self.validate_production_item()
		self.validate_bom()
		self.validate_sales_order()
		self.set_default_warehouse()
		self.validate_warehouses()

		self.set_required_items(reset_only_qty=bool(len(self.get("required_items"))))
		self.set_completion_status()
		self.set_required_items_status()

		self.calculate_raw_material_cost()
		self.calculate_operating_cost()
		self.calculate_total_cost()

		validate_uom_is_integer(self, "stock_uom", ["qty", "produced_qty"])

		self.set_status()

	def on_submit(self):
		self.update_sales_order_work_order_qty()
		self.update_material_request_ordered_qty()
		self.update_production_plan_ordered_qty()
		self.update_reserved_qty_for_production()
		self.update_planned_qty()

		self.create_job_card()

	def on_cancel(self):
		self.validate_cancel()
		self.db_set("status", "Cancelled")

		self.update_sales_order_work_order_qty()
		self.update_material_request_ordered_qty()
		self.update_production_plan_ordered_qty()
		self.update_reserved_qty_for_production()
		self.update_planned_qty()

		self.delete_job_card()

	def validate_qty(self):
		self.qty = flt(self.qty, self.precision("qty"))
		if self.qty <= 0:
			frappe.throw(_("Quantity to Produce must be greater than 0."))

	def validate_operation_time(self):
		for d in self.operations:
			if d.time_in_mins <= 0:
				frappe.throw(_("Operation Time must be greater than 0 for Operation {0}".format(d.operation)))

	def validate_production_item(self):
		if self.production_item:
			item = frappe.get_cached_value("Item", self.production_item, ['has_variants', 'end_of_life', 'disabled'], as_dict=1)
			validate_end_of_life(self.production_item, end_of_life=item.end_of_life, disabled=item.disabled)
			if item.has_variants:
				frappe.throw(_("Work Order cannot be raised against an Item Template"), ItemHasVariantError)

	def validate_bom(self):
		if self.get("bom_no"):
			validate_bom_no(self.production_item, self.bom_no)

	def validate_sales_order(self):
		if not self.sales_order:
			return

		self.check_sales_order_on_hold_or_close()

		so = frappe.db.sql("""
			select so.name, so_item.delivery_date, so.project
			from `tabSales Order` so
			inner join `tabSales Order Item` so_item on so_item.parent = so.name
			left join `tabProduct Bundle Item` pk_item on so_item.item_code = pk_item.parent
			where so.name = %s
				and so.docstatus = 1
				and so.skip_delivery_note = 0
				and (so_item.item_code = %s or pk_item.item_code = %s)
		""", (self.sales_order, self.production_item, self.production_item), as_dict=1)

		if not so:
			so = frappe.db.sql("""
				select so.name, so_item.delivery_date, so.project
				from `tabSales Order` so, `tabSales Order Item` so_item, `tabPacked Item` packed_item
				where so.name = %s
					and so.name = so_item.parent
					and so.name = packed_item.parent
					and so.skip_delivery_note = 0
					and so_item.item_code = packed_item.parent_item
					and so.docstatus = 1
					and packed_item.item_code=%s
			""", (self.sales_order, self.production_item), as_dict=1)

		if len(so):
			if not self.expected_delivery_date:
				self.expected_delivery_date = so[0].delivery_date

			if so[0].project:
				self.project = so[0].project

			if not self.material_request:
				self.validate_sales_order_qty()
		else:
			frappe.throw(_("Sales Order {0} is not valid").format(self.sales_order))

	def check_sales_order_on_hold_or_close(self):
		if not self.sales_order:
			return

		status = frappe.db.get_value("Sales Order", self.sales_order, "status", cache=1)
		if status in ("Closed", "On Hold"):
			frappe.throw(_("{0} is {1}").format(frappe.get_desk_link("Sales Order", self.sales_order), status))

	def validate_sales_order_qty(self):
		# already ordered qty
		ordered_qty_against_so = frappe.db.sql("""
			select sum(qty)
			from `tabWork Order`
			where production_item = %s and sales_order = %s and docstatus < 2 and name != %s
		""", (self.production_item, self.sales_order, self.name))[0][0]

		total_qty = flt(flt(ordered_qty_against_so) + flt(self.qty), self.precision('qty'))

		# get qty from Sales Order Item table
		so_item_qty = frappe.db.sql("""
			select sum(stock_qty)
			from `tabSales Order Item`
			where parent = %s and item_code = %s
		""", (self.sales_order, self.production_item))[0][0]

		# get qty from Packing Item table
		dnpi_qty = frappe.db.sql("""
			select sum(qty)
			from `tabPacked Item`
			where parent = %s and parenttype = 'Sales Order' and item_code = %s
		""", (self.sales_order, self.production_item))[0][0]

		# total qty in SO
		so_qty = round_up(flt(so_item_qty) + flt(dnpi_qty), self.precision('qty'))

		allowance_percentage = flt(frappe.db.get_single_value("Manufacturing Settings", "overproduction_percentage_for_sales_order"))

		if total_qty > so_qty + (allowance_percentage/100 * so_qty):
			frappe.throw(_("Cannot produce more Item {0} than Sales Order quantity {1}").format(
				frappe.bold(self.production_item), frappe.format(so_qty)
			), OverProductionError)

	def set_default_warehouse(self):
		warehouse_map = get_default_warehouse()

		for field, warehouse in warehouse_map.items():
			if not self.get(field) and warehouse:
				self.set(field, warehouse)

	def validate_warehouses(self):
		warehouses = [self.fg_warehouse, self.wip_warehouse]
		for d in self.get("required_items"):
			if d.source_warehouse not in warehouses:
				warehouses.append(d.source_warehouse)

		for wh in warehouses:
			validate_warehouse_company(wh, self.company)

		if self.docstatus == 1:
			if not self.wip_warehouse and not self.skip_transfer:
				frappe.throw(_("Work-in-Progress Warehouse is required before Submit"))
			if not self.fg_warehouse:
				frappe.throw(_("Target Warehouse is required before Submit"))

	@frappe.whitelist()
	def get_items_and_operations_from_bom(self):
		self.set_required_items()
		self.calculate_raw_material_cost()
		self.set_work_order_operations()
		self.calculate_total_cost()

		return check_if_scrap_warehouse_mandatory(self.bom_no)

	def set_required_items(self, reset_only_qty=False):
		'''set required_items for production to keep track of reserved qty'''
		if not reset_only_qty:
			self.required_items = []

		if self.bom_no and self.qty:
			item_dict = get_bom_items_as_dict(self.bom_no, self.company, qty=self.qty,
				fetch_exploded=self.use_multi_level_bom, fetch_qty_in_stock_uom=False)

			if reset_only_qty:
				for d in self.get("required_items"):
					if item_dict.get(d.item_code):
						item = item_dict.get(d.item_code)
						d.required_qty = flt(item.get("qty"))
						d.stock_uom = item.get('stock_uom') or frappe.get_cached_value("Item", d.item_code, 'stock_uom')
						d.conversion_factor = flt(item.get('conversion_factor')) or 1
						d.stock_required_qty = d.required_qty * d.conversion_factor
			else:
				# Attribute a big number (999) to idx for sorting putpose in case idx is NULL
				# For instance in BOM Explosion Item child table, the items coming from sub assembly items
				for item in sorted(item_dict.values(), key=lambda d: d['idx'] or 9999):
					row = self.append('required_items', {
						'operation': item.operation,
						'item_code': item.item_code,
						'item_name': item.item_name,
						'description': item.description,
						'allow_alternative_item': item.allow_alternative_item,
						'required_qty': item.qty,
						'stock_required_qty': item.qty * flt(item.conversion_factor) or 1,
						'uom': item.uom,
						'stock_uom': item.stock_uom,
						'conversion_factor': flt(item.conversion_factor) or 1,
						'source_warehouse': self.source_warehouse or item.source_warehouse or item.default_warehouse,
						'include_item_in_manufacturing': item.include_item_in_manufacturing
					})

					if not row.conversion_factor:
						row.conversion_factor = flt(get_conversion_factor(row.item_code, row.uom)['conversion_factor'])

					if not self.project:
						self.project = item.get("project")

			self.set_available_qty()

	def set_required_items_status(self, update=False, update_modified=True):
		for d in self.required_items:
			if self.docstatus == 1:
				ste_qty_map = dict(frappe.db.sql("""
					select ste.purpose, sum(i.qty)
					from `tabStock Entry` ste
					inner join `tabStock Entry Detail` i on i.parent = ste.name
					where ste.work_order = %(work_order)s
						and ste.purpose in ('Manufacture', 'Material Transfer for Manufacture', 'Material Consumption for Manufacture')
						and (i.item_code = %(item_code)s or i.original_item = %(item_code)s)
						and ste.docstatus = 1
					group by ste.purpose
				""", {"work_order": self.name, "item_code": d.item_code}))
			else:
				ste_qty_map = {}

			d.consumed_qty = flt(ste_qty_map.get("Manufacture")) + flt(ste_qty_map.get("Material Consumption for Manufacture"))
			d.transferred_qty = flt(ste_qty_map.get("Material Transfer for Manufacture"))

			if update:
				d.db_set({
					"consumed_qty": d.consumed_qty,
					"transferred_qty": d.transferred_qty,
				}, update_modified=update_modified)

	def set_work_order_operations(self):
		"""Fetch operations from BOM and set in 'Work Order'"""
		from erpnext.manufacturing.doctype.bom.bom import get_additional_operating_cost_per_unit

		self.set('operations', [])

		if not self.bom_no or cint(frappe.db.get_single_value("Manufacturing Settings", "disable_capacity_planning")):
			return

		if self.use_multi_level_bom:
			bom_list = frappe.get_doc("BOM", self.bom_no).traverse_tree()
		else:
			bom_list = [self.bom_no]

		operations = []
		if bom_list:
			operations = frappe.db.sql("""
				select operation, description, workstation, idx, base_hour_rate as hour_rate, time_in_mins,
					'Pending' as status, parent as bom, batch_size
				from `tabBOM Operation`
				where parent in %s order by idx
			""", [bom_list], as_dict=1)

		self.set('operations', operations)

		if self.use_multi_level_bom and self.get('operations') and self.get('items'):
			raw_material_operations = [d.operation for d in self.get('items')]
			operations = [d.operation for d in self.get('operations')]

			for operation in raw_material_operations:
				if operation not in operations:
					self.append('operations', {
						'operation': operation
					})

		# Additional costs
		additional_costs = get_additional_operating_cost_per_unit(self.bom_no, self.use_multi_level_bom, bom_list=bom_list)
		if additional_costs:
			self.set('additional_costs', [])
			for d in additional_costs:
				self.append('additional_costs', d)

		self.calculate_time()

	def update_operation_status(self):
		allowance_percentage = flt(frappe.db.get_single_value("Manufacturing Settings", "overproduction_percentage_for_work_order"))
		max_allowed_qty_for_wo = flt(self.qty) + (allowance_percentage/100 * flt(self.qty))

		for d in self.get("operations"):
			if not d.completed_qty:
				d.status = "Pending"
			elif flt(d.completed_qty) < flt(self.qty):
				d.status = "Work in Progress"
			elif flt(d.completed_qty) == flt(self.qty):
				d.status = "Completed"
			elif flt(d.completed_qty) <= max_allowed_qty_for_wo:
				d.status = "Completed"
			else:
				frappe.throw(_("Completed Qty can not be greater than 'Qty to Produce'"))

	def calculate_time(self):
		bom_qty = frappe.db.get_value("BOM", self.bom_no, "quantity", cache=1)

		for d in self.get("operations"):
			d.time_in_mins = flt(d.time_in_mins) / flt(bom_qty) * math.ceil(flt(self.qty) / flt(d.batch_size))

		self.calculate_operating_cost()

	def calculate_raw_material_cost(self):
		bom_cost, bom_qty = frappe.db.get_value("BOM", self.bom_no, ["base_raw_material_cost", "quantity"])
		self.raw_material_cost = bom_cost * flt(self.qty) / bom_qty if bom_qty else 0
		self.total_raw_material_qty = sum([d.required_qty for d in self.required_items])

	def calculate_operating_cost(self):
		self.planned_operating_cost = 0.0
		self.actual_operating_cost = 0.0

		for d in self.get("operations"):
			d.planned_operating_cost = flt(d.hour_rate) * (flt(d.time_in_mins) / 60.0)
			d.actual_operating_cost = flt(d.hour_rate) * (flt(d.actual_operation_time) / 60.0)

			self.planned_operating_cost += flt(d.planned_operating_cost)
			self.actual_operating_cost += flt(d.actual_operating_cost)

		self.additional_operating_cost = 0.0
		for d in self.get('additional_costs'):
			d.amount = flt(flt(d.rate) * flt(self.qty), d.precision('amount'))
			self.additional_operating_cost += d.amount

		variable_cost = self.actual_operating_cost if self.actual_operating_cost else self.planned_operating_cost

		self.total_operating_cost = flt(self.additional_operating_cost) + flt(variable_cost)

	def calculate_total_cost(self):
		self.total_cost = self.raw_material_cost + self.total_operating_cost

	def set_available_qty(self):
		for d in self.get("required_items"):
			if d.source_warehouse:
				d.available_qty_at_source_warehouse = get_latest_stock_qty(d.item_code, d.source_warehouse)

			if self.wip_warehouse:
				d.available_qty_at_wip_warehouse = get_latest_stock_qty(d.item_code, self.wip_warehouse)

	def update_status(self, status=None):
		self.set_completion_status(update=True)
		self.set_required_items_status(update=True)

		self.validate_overproduction()

		self.set_status(status=status, update=True)

		self.update_sales_order_produced_qty()
		self.update_production_plan_produced_qty()

		self.update_planned_qty()
		self.update_reserved_qty_for_production()

		return self.status

	def set_completion_status(self, update=False, update_modified=True):
		ste_qty_map = {}
		if self.docstatus == 1:
			ste_qty_data = frappe.db.sql("""
				select purpose, sum(fg_completed_qty) as fg_completed_qty, sum(scrap_qty) as scrap_qty
				from `tabStock Entry`
				where work_order = %s and docstatus = 1 and purpose in ('Manufacture', 'Material Transfer for Manufacture')
				group by purpose
			""", self.name, as_dict=1)

			for d in ste_qty_data:
				ste_qty_map[d.purpose] = d

		to_update = frappe._dict({
			"produced_qty": flt(ste_qty_map.get("Manufacture", {}).get("fg_completed_qty")),
			"scrap_qty": flt(ste_qty_map.get("Manufacture", {}).get("scrap_qty")),
			"material_transferred_for_manufacturing": flt(ste_qty_map.get("Material Transfer for Manufacture", {}).get("fg_completed_qty")),
		})

		if self.operations and self.transfer_material_against == 'Job Card':
			del to_update["material_transferred_for_manufacturing"]

		to_update.per_produced = flt(to_update.produced_qty / self.qty * 100, 6)
		to_update.per_material_transferred = flt(to_update.material_transferred_for_manufacturing / self.qty * 100, 6)

		self.update(to_update)
		if update:
			self.db_set(to_update, update_modified=update_modified)

	def validate_overproduction(self):
		max_qty = flt(self.get_qty_with_allowance(self.qty), self.precision("qty"))
		for fieldname in ["produced_qty", "scrap_qty", "material_transferred_for_manufacturing"]:
			qty = flt(self.get(fieldname), self.precision("qty"))
			if qty > max_qty:
				frappe.throw(_("{0} {1} cannot be greater than planned quantity {2} in Work Order {3}").format(
					self.meta.get_label(fieldname),
					frappe.bold(self.get_formatted(fieldname)),
					frappe.bold(frappe.format(max_qty)),
					self.name
				), StockOverProductionError)

		produced_qty = flt(self.produced_qty, self.precision("qty"))
		transferred_qty = flt(self.material_transferred_for_manufacturing, self.precision("qty"))
		if not self.skip_transfer and produced_qty > transferred_qty:
			frappe.throw(_("Produced Qty cannot more than the Material Transferred for Manufacturing {0}").format(
				frappe.bold(self.get_formatted("material_transferred_for_manufacturing"))
			), StockOverProductionError)

	def get_qty_with_allowance(self, qty):
		allowance_percentage = flt(frappe.get_cached_value("Manufacturing Settings", None, "overproduction_percentage_for_work_order"))
		return flt(qty) + flt(qty) * allowance_percentage / 100

	def set_status(self, status=None, update=False, update_modified=True):
		previous_status = self.status

		if status:
			self.status = status

		if self.docstatus == 0:
			self.status = 'Draft'

		elif self.docstatus == 1:
			under_production_allowance = flt(frappe.db.get_single_value("Manufacturing Settings", "under_production_allowance"))
			completed_qty = flt(self.produced_qty + self.scrap_qty, self.precision("qty"))
			min_qty = flt(self.qty - (self.qty * under_production_allowance / 100), self.precision("qty"))

			if completed_qty >= min_qty:
				self.status = "Completed"
			elif self.status == "Stopped":
				self.status = "Stopped"
			elif self.has_stock_entry():
				self.status = "In Process"
			else:
				self.status = "Not Started"

		elif self.docstatus == 2:
			self.status = "Cancelled"

		self.add_status_comment(previous_status)

		if update:
			self.db_set('status', self.status, update_modified=update_modified)

	def has_stock_entry(self):
		return frappe.db.get_value("Stock Entry", {"work_order": self.name, "docstatus": 1})

	def update_sales_order_work_order_qty(self):
		if not self.sales_order and not self.sales_order_item:
			return

		total_bundle_qty = 1
		if self.product_bundle_item:
			total_bundle_qty = frappe.db.sql("""
				select sum(qty)
				from `tabProduct Bundle Item`
				where parent = %s
			""", (frappe.db.escape(self.product_bundle_item)))[0][0]

			if not total_bundle_qty:
				# product bundle is 0 (product bundle allows 0 qty for items)
				total_bundle_qty = 1

		cond = "product_bundle_item = %s" if self.product_bundle_item else "production_item = %s"

		qty = frappe.db.sql("""
			select sum(qty)
			from `tabWork Order`
			where sales_order = %s and docstatus = 1 and {0}
		""".format(cond), (self.sales_order, (self.product_bundle_item or self.production_item)), as_list=1)

		work_order_qty = flt(qty[0][0]) if qty else 0

		precision = frappe.get_precision("Sales Order Item", "work_order_qty")
		frappe.db.set_value('Sales Order Item', self.sales_order_item, 'work_order_qty',
			flt(work_order_qty/total_bundle_qty, precision))

	def update_sales_order_produced_qty(self):
		from erpnext.selling.doctype.sales_order.sales_order import update_produced_qty_in_so_item
		if self.sales_order and self.sales_order_item:
			update_produced_qty_in_so_item(self.sales_order, self.sales_order_item)

	def update_production_plan_ordered_qty(self):
		if self.production_plan and self.production_plan_item:
			qty = self.qty if self.docstatus == 1 else 0
			frappe.db.set_value('Production Plan Item', self.production_plan_item, 'ordered_qty', qty)

			doc = frappe.get_doc('Production Plan', self.production_plan)
			doc.set_status()
			doc.db_set('status', doc.status)

	def update_production_plan_produced_qty(self):
		if not self.production_plan:
			return

		production_plan = frappe.get_doc('Production Plan', self.production_plan)
		produced_qty = 0
		if self.production_plan_item:
			total_qty = frappe.get_all("Work Order", fields="sum(produced_qty) as produced_qty", filters={
				'docstatus': 1,
				'production_plan': self.production_plan,
				'production_plan_item': self.production_plan_item
			}, as_list=1)

			produced_qty = total_qty[0][0] if total_qty else 0

		production_plan.run_method("update_produced_qty", produced_qty, self.production_plan_item)

	def update_material_request_ordered_qty(self):
		if self.material_request:
			doc = frappe.get_doc("Material Request", self.material_request)
			doc.set_completion_status(update=True)
			doc.validate_ordered_qty(from_doctype=self.doctype, row_names=[self.material_request_item])
			doc.set_status(update=True)

			doc.update_requested_qty([self.material_request_item])

			doc.notify_update()

	def update_planned_qty(self):
		update_bin_qty(self.production_item, self.fg_warehouse, {
			"planned_qty": get_planned_qty(self.production_item, self.fg_warehouse)
		})

	def update_reserved_qty_for_production(self):
		for d in self.required_items:
			if d.source_warehouse:
				stock_bin = get_bin(d.item_code, d.source_warehouse)
				stock_bin.update_reserved_qty_for_production()

	def create_job_card(self):
		for row in self.operations:
			if not row.workstation:
				frappe.throw(_("Row {0}: Select the Workstation against the Operation {1}")
					.format(row.idx, row.operation))

			create_job_card(self, row, auto_create=True)

	def delete_job_card(self):
		for d in frappe.get_all("Job Card", ["name"], {"work_order": self.name}):
			frappe.delete_doc("Job Card", d.name)

	def validate_cancel(self):
		if self.status == "Stopped":
			frappe.throw(_("Stopped Work Order cannot be cancelled, Unstop it first to cancel"))

		# Check whether any stock entry exists against this Work Order
		stock_entry = frappe.db.sql("""
			select name
			from `tabStock Entry`
			where work_order = %s and docstatus = 1
		""", self.name)

		if stock_entry:
			frappe.throw(_("Cannot cancel because submitted Stock Entry {0} exists").format(
				frappe.utils.get_link_to_form('Stock Entry', stock_entry[0][0]))
			)

	def get_holidays(self, workstation):
		holiday_list = frappe.db.get_value("Workstation", workstation, "holiday_list")

		holidays = {}

		if holiday_list not in holidays:
			holiday_list_days = [getdate(d[0]) for d in frappe.get_all("Holiday", fields=["holiday_date"],
				filters={"parent": holiday_list}, order_by="holiday_date", limit_page_length=0, as_list=1)]

			holidays[holiday_list] = holiday_list_days

		return holidays[holiday_list]

	def set_actual_dates(self):
		self.actual_start_date = None
		self.actual_end_date = None
		if self.get("operations"):
			actual_start_dates = [d.actual_start_time for d in self.get("operations") if d.actual_start_time]
			if actual_start_dates:
				self.actual_start_date = min(actual_start_dates)

			actual_end_dates = [d.actual_end_time for d in self.get("operations") if d.actual_end_time]
			if actual_end_dates:
				self.actual_end_date = max(actual_end_dates)

	def get_required_items_dict(self):
		item_dict = {}
		for d in self.required_items:
			item_dict[d.item_code] = d

		return item_dict

	@frappe.whitelist()
	def make_bom(self):
		data = frappe.db.sql(""" select sed.item_code, sed.qty, sed.s_warehouse
			from `tabStock Entry Detail` sed, `tabStock Entry` se
			where se.name = sed.parent and se.purpose = 'Manufacture'
			and (sed.t_warehouse is null or sed.t_warehouse = '') and se.docstatus = 1
			and se.work_order = %s""", (self.name), as_dict=1)

		bom = frappe.new_doc("BOM")
		bom.item = self.production_item
		bom.conversion_rate = 1

		for d in data:
			bom.append('items', {
				'item_code': d.item_code,
				'qty': d.qty,
				'source_warehouse': d.s_warehouse
			})

		if self.operations:
			bom.set('operations', self.operations)
			bom.with_operations = 1

		bom.set_bom_material_details()
		return bom


@frappe.whitelist()
@frappe.validate_and_sanitize_search_inputs
def get_bom_operations(doctype, txt, searchfield, start, page_len, filters):
	if txt:
		filters['operation'] = ('like', '%%%s%%' % txt)

	return frappe.get_all('BOM Operation', filters=filters, fields=['operation'], as_list=1)


@frappe.whitelist()
def get_item_details(item, project=None):
	res = frappe.db.sql("""
		select item_name, stock_uom, description
		from `tabItem`
		where disabled=0
			and (end_of_life is null or end_of_life='0000-00-00' or end_of_life > %s)
			and name=%s
	""", (nowdate(), item), as_dict=1)

	if not res:
		return {}

	res = res[0]

	filters = {"item": item, "is_default": 1}

	if project:
		filters = {"item": item, "project": project}

	res["bom_no"] = frappe.db.get_value("BOM", filters = filters)

	if not res["bom_no"]:
		variant_of = frappe.db.get_value("Item", item, "variant_of")

		if variant_of:
			res["bom_no"] = frappe.db.get_value("BOM", filters={"item": variant_of, "is_default": 1})

	if not res["bom_no"]:
		if project:
			res = get_item_details(item)
			frappe.msgprint(_("Default BOM not found for Item {0} and Project {1}").format(item, project), alert=1)
		else:
			frappe.throw(_("Default BOM for {0} not found").format(item))

	bom_data = frappe.db.get_value('BOM', res['bom_no'],
		['project', 'allow_alternative_item', 'transfer_material_against', 'item_name'], as_dict=1)

	res['project'] = project or bom_data.pop("project")
	res.update(bom_data)
	res.update(check_if_scrap_warehouse_mandatory(res["bom_no"]))

	return res


@frappe.whitelist()
def make_work_order(bom_no, item, qty=0, project=None):
	if not frappe.has_permission("Work Order", "write"):
		frappe.throw(_("Not permitted"), frappe.PermissionError)

	item_details = get_item_details(item, project)

	wo_doc = frappe.new_doc("Work Order")
	wo_doc.production_item = item
	wo_doc.update(item_details)
	wo_doc.bom_no = bom_no

	if flt(qty) > 0:
		wo_doc.qty = flt(qty)
		wo_doc.get_items_and_operations_from_bom()

	return wo_doc


@frappe.whitelist()
def check_if_scrap_warehouse_mandatory(bom_no):
	res = {"set_scrap_wh_mandatory": False}
	if bom_no:
		bom = frappe.get_doc("BOM", bom_no)

		if len(bom.scrap_items) > 0:
			res["set_scrap_wh_mandatory"] = True

	return res


@frappe.whitelist()
def set_work_order_ops(name):
	po = frappe.get_doc('Work Order', name)
	po.set_work_order_operations()
	po.save()


@frappe.whitelist()
def make_stock_entry(work_order_id, purpose, qty=None, scrap_remaining=False):
	work_order = frappe.get_doc("Work Order", work_order_id)
	if not frappe.db.get_value("Warehouse", work_order.wip_warehouse, "is_group") and not work_order.skip_transfer:
		wip_warehouse = work_order.wip_warehouse
	else:
		wip_warehouse = None

	stock_entry = frappe.new_doc("Stock Entry")
	stock_entry.purpose = purpose
	stock_entry.work_order = work_order_id
	stock_entry.company = work_order.company
	stock_entry.from_bom = 1
	stock_entry.bom_no = work_order.bom_no
	stock_entry.use_multi_level_bom = work_order.use_multi_level_bom

	stock_entry.fg_completed_qty = flt(qty) or (flt(work_order.qty) - flt(work_order.produced_qty))
	scrap_remaining = cint(scrap_remaining)
	stock_entry.scrap_qty = max(0.0, flt(work_order.qty) - flt(work_order.produced_qty) - flt(qty)) if scrap_remaining and qty else 0

	if work_order.bom_no:
		stock_entry.inspection_required = frappe.db.get_value('BOM', work_order.bom_no, 'inspection_required')

	if purpose == "Material Transfer for Manufacture":
		stock_entry.to_warehouse = wip_warehouse
		stock_entry.project = work_order.project
	else:
		stock_entry.from_warehouse = wip_warehouse
		stock_entry.to_warehouse = work_order.fg_warehouse
		stock_entry.project = work_order.project

	stock_entry.set_stock_entry_type()
	stock_entry.get_items()

	def submit_stock_entry(ste):
		ste_copy = frappe.get_doc(copy.deepcopy(ste))
		ste_copy.save()
		ste_copy.submit()
		frappe.msgprint(_("{0} {1} submitted successfully").format(
			purpose, frappe.get_desk_link("Stock Entry", ste_copy.name))
		)
		return ste_copy

	try:
		if purpose == "Material Transfer for Manufacture":
			if frappe.db.get_single_value("Manufacturing Settings", "auto_submit_material_transfer_entry"):
				stock_entry = submit_stock_entry(stock_entry)
		else:
			if frappe.db.get_single_value("Manufacturing Settings", "auto_submit_manufacture_entry"):
				stock_entry = submit_stock_entry(stock_entry)
	except StockOverProductionError:
		raise
	except frappe.ValidationError:
		frappe.db.rollback()

	return stock_entry.as_dict()


@frappe.whitelist()
def get_default_warehouse():
	wip_warehouse = frappe.get_cached_value("Manufacturing Settings", None, "default_wip_warehouse")
	fg_warehouse = frappe.get_cached_value("Manufacturing Settings", None, "default_fg_warehouse")
	rm_warehouse = frappe.get_cached_value("Manufacturing Settings", None, "default_rm_warehouse")
	return {"wip_warehouse": wip_warehouse, "fg_warehouse": fg_warehouse, "source_warehouse": rm_warehouse}


@frappe.whitelist()
def stop_unstop(work_order, status):
	""" Called from client side on Stop/Unstop event"""

	if not frappe.has_permission("Work Order", "write"):
		frappe.throw(_("Not permitted"), frappe.PermissionError)

	pro_order = frappe.get_doc("Work Order", work_order)
	pro_order.run_method("update_status", status)
	pro_order.notify_update()

	frappe.msgprint(_("Work Order has been {0}").format(frappe.bold(status)))

	return pro_order.status


@frappe.whitelist()
def query_sales_order(production_item):
	out = frappe.db.sql_list("""
		select distinct so.name from `tabSales Order` so, `tabSales Order Item` so_item
		where so_item.parent=so.name and so_item.item_code=%s and so.docstatus=1
	union
		select distinct so.name from `tabSales Order` so, `tabPacked Item` pi_item
		where pi_item.parent=so.name and pi_item.item_code=%s and so.docstatus=1
	""", (production_item, production_item))

	return out


@frappe.whitelist()
def make_job_card(work_order, operations):
	if isinstance(operations, str):
		operations = json.loads(operations)

	work_order = frappe.get_doc('Work Order', work_order)
	for row in operations:
		validate_operation_data(row)
		create_job_card(work_order, row, row.get("qty"), auto_create=True)


def validate_operation_data(row):
	if row.get("qty") <= 0:
		frappe.throw(_("Quantity to Produce can not be zero for the operation {0}").format(
			frappe.bold(row.get("operation"))
		))

	if row.get("qty") > row.get("pending_qty"):
		frappe.throw(_("For operation {0}: Quantity ({1}) can not be greter than pending quantity ({2})").format(
			frappe.bold(row.get("operation")), frappe.bold(row.get("qty")), frappe.bold(row.get("pending_qty"))
		))


def create_job_card(work_order, row, qty=0, auto_create=False):
	doc = frappe.new_doc("Job Card")
	doc.update({
		'work_order': work_order.name,
		'operation': row.get("operation"),
		'workstation': row.get("workstation"),
		'posting_date': nowdate(),
		'for_quantity': qty or work_order.get('qty', 0),
		'operation_id': row.get("name"),
		'bom_no': work_order.bom_no,
		'project': work_order.project,
		'company': work_order.company,
		'wip_warehouse': work_order.wip_warehouse
	})

	if work_order.transfer_material_against == 'Job Card' and not work_order.skip_transfer:
		doc.get_required_items()

	if auto_create:
		doc.flags.ignore_mandatory = True
		doc.insert()
		frappe.msgprint(_("Job card {0} created").format(get_link_to_form("Job Card", doc.name)))

	return doc


def get_work_order_operation_data(work_order, operation, workstation):
	for d in work_order.operations:
		if d.operation == operation and d.workstation == workstation:
			return d


@frappe.whitelist()
def create_pick_list(source_name, target_doc=None, for_qty=None):
	for_qty = for_qty or json.loads(target_doc).get('for_qty')
	max_finished_goods_qty = frappe.db.get_value('Work Order', source_name, 'qty')

	def update_item_quantity(source, target, source_parent, target_parent):
		pending_to_issue = flt(source.required_qty) - flt(source.transferred_qty)
		desire_to_transfer = flt(source.required_qty) / max_finished_goods_qty * flt(for_qty)

		qty = 0
		if desire_to_transfer <= pending_to_issue:
			qty = desire_to_transfer
		elif pending_to_issue > 0:
			qty = pending_to_issue

		if qty:
			target.qty = qty
			target.stock_qty = qty
			target.uom = frappe.get_value('Item', source.item_code, 'stock_uom')
			target.stock_uom = target.uom
			target.conversion_factor = 1
		else:
			target.delete()

	doc = get_mapped_doc('Work Order', source_name, {
		'Work Order': {
			'doctype': 'Pick List',
			'validation': {
				'docstatus': ['=', 1]
			}
		},
		'Work Order Item': {
			'doctype': 'Pick List Item',
			'postprocess': update_item_quantity,
			'condition': lambda doc, source, target: abs(doc.transferred_qty) < abs(doc.required_qty)
		},
	}, target_doc)

	doc.for_qty = for_qty

	doc.set_item_locations()

	return doc
