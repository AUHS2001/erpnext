// Copyright (c) 2017, Frappe Technologies Pvt. Ltd. and contributors
// For license information, please see license.txt

frappe.ui.form.on("Work Order", {
	setup: function(frm) {
		frm.doc.disable_item_formatter = true;

		frm.custom_make_buttons = {
			'Stock Entry': 'Start',
			'Pick List': 'Pick List',
			'Job Card': 'Create Job Card'
		};

		// Set query for warehouses
		frm.set_query("wip_warehouse", function() {
			return {
				filters: {
					'company': frm.doc.company,
				}
			};
		});

		frm.set_query("source_warehouse", function() {
			return {
				filters: {
					'company': frm.doc.company,
				}
			};
		});

		frm.set_query("source_warehouse", "required_items", function() {
			return {
				filters: {
					'company': frm.doc.company,
				}
			};
		});

		frm.set_query("sales_order", function() {
			return {
				filters: {
					"docstatus": 1,
					"status": ["not in", ["Closed", "On Hold"]]
				}
			};
		});

		frm.set_query("fg_warehouse", function() {
			return {
				filters: {
					'company': frm.doc.company,
					'is_group': 0
				}
			};
		});

		frm.set_query("scrap_warehouse", function() {
			return {
				filters: {
					'company': frm.doc.company,
					'is_group': 0
				}
			};
		});

		// Set query for BOM
		frm.set_query("bom_no", function() {
			if (frm.doc.production_item) {
				return {
					query: "erpnext.controllers.queries.bom",
					filters: {item: cstr(frm.doc.production_item)}
				};
			} else {
				frappe.msgprint(__("Please enter Production Item first"));
			}
		});

		// Set query for FG Item
		frm.set_query("production_item", function() {
			return {
				query: "erpnext.controllers.queries.item_query",
				filters:[
					['is_stock_item', '=',1],
					['default_bom', '!=', '']
				]
			};
		});

		// Set query for FG Item
		frm.set_query("project", function() {
			return{
				filters:[
					['Project', 'status', 'not in', 'Completed, Cancelled']
				]
			};
		});

		frm.set_query("operation", "required_items", function() {
			return {
				query: "erpnext.manufacturing.doctype.work_order.work_order.get_bom_operations",
				filters: {
					'parent': frm.doc.bom_no,
					'parenttype': 'BOM'
				}
			};
		});
	},

	onload: function(frm) {
		if (!frm.doc.status)
			frm.doc.status = 'Draft';

		frm.add_fetch("sales_order", "project", "project");

		if(frm.doc.__islocal) {
			frm.set_value({
				"actual_start_date": "",
				"actual_end_date": ""
			});
			erpnext.work_order.set_default_warehouse(frm);
		}
	},

	source_warehouse: function(frm) {
		let stock_controller = new erpnext.stock.StockController();
		stock_controller.autofill_warehouse(frm.doc.required_items, "source_warehouse", frm.doc.source_warehouse);
	},

	refresh: function(frm) {
		erpnext.toggle_naming_series();
		erpnext.hide_company();
		erpnext.work_order.set_custom_buttons(frm);

		frm.set_intro("");

		frm.doc.disable_item_formatter = true;

		if (frm.doc.docstatus === 0 && !frm.doc.__islocal) {
			frm.set_intro(__("Submit this Work Order for further processing."));
		}

		if (frm.doc.docstatus===1) {
			frm.trigger('show_progress_for_production');
			frm.trigger('show_progress_for_packing');
			frm.trigger('show_progress_for_operations');
		}

		if (frm.doc.docstatus === 1
			&& frm.doc.operations && frm.doc.operations.length
			&& frm.doc.qty != frm.doc.produced_qty) {

			const not_completed = frm.doc.operations.filter(d => {
				if(d.status != 'Completed') {
					return true;
				}
			});

			if(not_completed && not_completed.length) {
				frm.add_custom_button(__('Create Job Card'), () => {
					frm.trigger("make_job_card");
				}).addClass('btn-primary');
			}
		}

		if(frm.doc.required_items && frm.doc.allow_alternative_item) {
			const has_alternative = frm.doc.required_items.find(i => i.allow_alternative_item === 1);
			if (frm.doc.docstatus == 0 && has_alternative) {
				frm.add_custom_button(__('Alternate Item'), () => {
					erpnext.utils.select_alternate_items({
						frm: frm,
						child_docname: "required_items",
						warehouse_field: "source_warehouse",
						child_doctype: "Work Order Item",
						original_item_field: "original_item",
						condition: (d) => {
							if (d.allow_alternative_item) {return true;}
						}
					});
				});
			}
		}

		if (frm.doc.status == "Completed" &&
			frm.doc.__onload.backflush_raw_materials_based_on == "Material Transferred for Manufacture") {
			frm.add_custom_button(__('Create BOM'), () => {
				frm.trigger("make_bom");
			});
		}

		// formatter for work order operation
		frm.set_indicator_formatter('operation',
			function(doc) { return (frm.doc.qty==doc.completed_qty) ? "green" : "orange"; });
	},

	make_job_card: function(frm) {
		let qty = 0;
		let operations_data = [];

		const dialog = frappe.prompt({fieldname: 'operations', fieldtype: 'Table', label: __('Operations'),
			fields: [
				{
					fieldtype:'Link',
					fieldname:'operation',
					label: __('Operation'),
					read_only:1,
					in_list_view:1
				},
				{
					fieldtype:'Link',
					fieldname:'workstation',
					label: __('Workstation'),
					read_only:1,
					in_list_view:1
				},
				{
					fieldtype:'Data',
					fieldname:'name',
					label: __('Operation Id')
				},
				{
					fieldtype:'Float',
					fieldname:'pending_qty',
					label: __('Pending Qty'),
				},
				{
					fieldtype:'Float',
					fieldname:'qty',
					label: __('Quantity to Manufacture'),
					read_only:0,
					in_list_view:1,
				},
			],
			data: operations_data,
			in_place_edit: true,
			get_data: function() {
				return operations_data;
			}
		}, function(data) {
			frappe.call({
				method: "erpnext.manufacturing.doctype.work_order.work_order.make_job_card",
				args: {
					work_order: frm.doc.name,
					operations: data.operations,
				}
			});
		}, __("Job Card"), __("Create"));

		dialog.fields_dict["operations"].grid.wrapper.find('.grid-add-row').hide();

		var pending_qty = 0;
		frm.doc.operations.forEach(data => {
			if(data.completed_qty != frm.doc.qty) {
				pending_qty = frm.doc.qty - flt(data.completed_qty);

				dialog.fields_dict.operations.df.data.push({
					'name': data.name,
					'operation': data.operation,
					'workstation': data.workstation,
					'qty': pending_qty,
					'pending_qty': pending_qty,
				});
			}
		});
		dialog.fields_dict.operations.grid.refresh();
	},

	make_bom: function(frm) {
		frappe.call({
			method: "make_bom",
			doc: frm.doc,
			callback: function(r){
				if (r.message) {
					var doc = frappe.model.sync(r.message)[0];
					frappe.set_route("Form", doc.doctype, doc.name);
				}
			}
		});
	},

	show_progress_for_production: function(frm) {
		erpnext.manufacturing.show_progress_for_production(frm.doc, frm);
	},

	show_progress_for_packing: function (frm) {
		if (!frm.doc.packing_slip_required) {
			return;
		}
		erpnext.manufacturing.show_progress_for_packing(frm.doc, frm);
	},

	show_progress_for_operations: function(frm) {
		if (frm.doc.operations && frm.doc.operations.length) {

			let progress_class = {
				"Work in Progress": "progress-bar-warning",
				"Completed": "progress-bar-success"
			};

			let bars = [];
			let message = '';
			let title = '';
			let status_wise_oprtation_data = {};
			let total_completed_qty = frm.doc.qty * frm.doc.operations.length;

			frm.doc.operations.forEach(d => {
				if (!status_wise_oprtation_data[d.status]) {
					status_wise_oprtation_data[d.status] = [d.completed_qty, d.operation];
				} else {
					status_wise_oprtation_data[d.status][0] += d.completed_qty;
					status_wise_oprtation_data[d.status][1] += ', ' + d.operation;
				}
			});

			for (let key in status_wise_oprtation_data) {
				title = __("{0} Operations: {1}", [key, status_wise_oprtation_data[key][1].bold()]);
				bars.push({
					'title': title,
					'width': status_wise_oprtation_data[key][0] / total_completed_qty * 100  + '%',
					'progress_class': progress_class[key]
				});

				message += title + '. ';
			}

			frm.dashboard.add_progress(__('Operation Status'), bars, message);
		}
	},

	production_item: function(frm) {
		if (frm.doc.production_item) {
			frappe.call({
				method: "erpnext.manufacturing.doctype.work_order.work_order.get_item_details",
				args: {
					item: frm.doc.production_item,
					project: frm.doc.project
				},
				freeze: true,
				callback: function(r) {
					if(r.message) {
						frm.set_value('sales_order', "");
						frm.trigger('set_sales_order');
						erpnext.in_production_item_onchange = true;

						$.each(["description", "stock_uom", "project", "bom_no", "allow_alternative_item",
							"transfer_material_against", "item_name"], function(i, field) {
							frm.set_value(field, r.message[field]);
						});

						if(r.message["set_scrap_wh_mandatory"]){
							frm.toggle_reqd("scrap_warehouse", true);
						}
						erpnext.in_production_item_onchange = false;
					}
				}
			});
		}
	},

	project: function(frm) {
		if(!erpnext.in_production_item_onchange && !frm.doc.bom_no) {
			frm.trigger("production_item");
		}
	},

	bom_no: function(frm) {
		return frm.call({
			doc: frm.doc,
			method: "get_items_and_operations_from_bom",
			freeze: true,
			callback: function(r) {
				if(r.message["set_scrap_wh_mandatory"]){
					frm.toggle_reqd("scrap_warehouse", true);
				}
			}
		});
	},

	use_multi_level_bom: function(frm) {
		if(frm.doc.bom_no) {
			frm.trigger("bom_no");
		}
	},

	skip_transfer: function (frm) {
		frm.toggle_reqd("wip_warehouse", !frm.doc.skip_transfer);
	},

	qty: function(frm) {
		frm.trigger('bom_no');
	},

	before_submit: function(frm) {
		frm.toggle_reqd("fg_warehouse", true);
		frm.toggle_reqd("wip_warehouse", !frm.doc.skip_transfer);

		frm.fields_dict.required_items.grid.toggle_reqd("source_warehouse", true);
		frm.toggle_reqd("transfer_material_against",
			frm.doc.operations && frm.doc.operations.length > 0);
		frm.fields_dict.operations.grid.toggle_reqd("workstation", frm.doc.operations);
	},

	set_sales_order: function(frm) {
		if(frm.doc.production_item) {
			frappe.call({
				method: "erpnext.manufacturing.doctype.work_order.work_order.query_sales_order",
				args: { production_item: frm.doc.production_item },
				callback: function(r) {
					frm.set_query("sales_order", function() {
						erpnext.in_production_item_onchange = true;
						return {
							filters: [
								["Sales Order","name", "in", r.message]
							]
						};
					});
				}
			});
		}
	},

	additional_operating_cost: function(frm) {
		erpnext.work_order.calculate_cost(frm.doc);
		erpnext.work_order.calculate_total_cost(frm);
	}
});

frappe.ui.form.on("Work Order Item", {
	source_warehouse: function(frm, cdt, cdn) {
		var row = locals[cdt][cdn];
		if(!row.item_code) {
			frappe.throw(__("Please set the Item Code first"));
		} else if(row.source_warehouse) {
			frappe.call({
				"method": "erpnext.stock.utils.get_latest_stock_qty",
				args: {
					item_code: row.item_code,
					warehouse: row.source_warehouse
				},
				callback: function (r) {
					frappe.model.set_value(row.doctype, row.name,
						"available_qty_at_source_warehouse", r.message);
				}
			});
		}
	}
});

frappe.ui.form.on("Work Order Operation", {
	workstation: function(frm, cdt, cdn) {
		var d = locals[cdt][cdn];
		if (d.workstation) {
			frappe.call({
				"method": "frappe.client.get",
				args: {
					doctype: "Workstation",
					name: d.workstation
				},
				callback: function (data) {
					frappe.model.set_value(d.doctype, d.name, "hour_rate", data.message.hour_rate);
					erpnext.work_order.calculate_cost(frm.doc);
					erpnext.work_order.calculate_total_cost(frm);
				}
			});
		}
	},
	time_in_mins: function(frm, cdt, cdn) {
		erpnext.work_order.calculate_cost(frm.doc);
		erpnext.work_order.calculate_total_cost(frm);
	},
});

frappe.ui.form.on("Work Order Additional Cost", "rate", function(frm) {
	erpnext.work_order.calculate_cost(frm.doc);
	erpnext.work_order.calculate_total_cost(frm);
});

erpnext.work_order = {
	set_custom_buttons: function(frm) {
		let doc = frm.doc;
		if (doc.docstatus === 1) {
			if (doc.status != 'Stopped' && doc.status != 'Completed') {
				frm.add_custom_button(__('Stop'), function() {
					erpnext.work_order.stop_work_order(frm, "Stopped");
				}, __("Status"));
			} else if (doc.status == 'Stopped') {
				frm.add_custom_button(__('Re-Open'), function() {
					erpnext.work_order.stop_work_order(frm, "Resumed");
				}, __("Status"));
			}
		}
		erpnext.work_order.setup_start_finish_buttons(frm);
	},

	setup_start_finish_buttons: function (frm) {
		let doc = frm.doc;
		if (doc.docstatus != 1 || doc.status == "Stopped") {
			return;
		}

		// Start Button
		let qty_with_allowance = erpnext.manufacturing.get_qty_with_allowance(frm.doc);

		const show_start_btn = (
			!doc.skip_transfer
			&& doc.transfer_material_against != 'Job Card'
			&& flt(doc.material_transferred_for_manufacturing) < qty_with_allowance
			&& flt(doc.produced_qty) < flt(doc.qty)
		);

		if (show_start_btn) {
			frm.has_start_btn = true;

			frm.add_custom_button(__('Pick List'), function() {
				erpnext.work_order.create_pick_list(frm);
			}, __("Create"));

			let start_btn = frm.add_custom_button(__('Start'), function() {
				erpnext.manufacturing.make_stock_entry(doc, 'Material Transfer for Manufacture');
			});

			if (
				!flt(doc.material_transferred_for_manufacturing)
				|| flt(doc.produced_qty) >= flt(doc.material_transferred_for_manufacturing)
			) {
				start_btn.removeClass('btn-default').addClass('btn-primary');
			}
		}

		// Finish Button
		if (doc.skip_transfer) {
			let max_qty = flt(doc.max_qty) || flt(doc.qty);

			if (flt(doc.produced_qty) < max_qty) {
				let finish_btn = frm.add_custom_button(__('Finish'), function () {
					erpnext.manufacturing.make_stock_entry(doc, 'Manufacture');
				});
				finish_btn.removeClass('btn-default').addClass('btn-primary');
			}
		} else {
			if (flt(doc.produced_qty) < flt(doc.material_transferred_for_manufacturing)) {
				frm.has_finish_btn = true;

				let finish_btn = frm.add_custom_button(__('Finish'), function() {
					erpnext.manufacturing.make_stock_entry(doc, 'Manufacture');
				});
				if (flt(doc.material_transferred_for_manufacturing) >= flt(doc.produced_qty)) {
					// all materials transferred for manufacturing, make this primary
					finish_btn.removeClass('btn-default').addClass('btn-primary');
				}

				// If "Material Consumption is check in Manufacturing Settings, allow Material Consumption
				if (doc.__onload && doc.__onload.material_consumption) {
					// Only show "Material Consumption" when required_qty > consumed_qty
					let required_items = doc.required_items || [];

					if (required_items.some(d => flt(d.required_qty) > flt(d.consumed_qty))) {
						let consumption_btn = frm.add_custom_button(__('Material Consumption'), function() {
							const backflush_raw_materials_based_on = doc.__onload.backflush_raw_materials_based_on;
							erpnext.work_order.make_consumption_se(frm, backflush_raw_materials_based_on);
						});
						consumption_btn.addClass('btn-primary');
					}
				}
			}
		}
	},

	calculate_cost: function(doc) {
		if (doc.operations){
			var op = doc.operations;
			doc.planned_operating_cost = 0.0;
			for(let i=0;i<op.length;i++) {
				var planned_operating_cost = flt(flt(op[i].hour_rate) * flt(op[i].time_in_mins) / 60, 2);
				frappe.model.set_value('Work Order Operation', op[i].name,
					"planned_operating_cost", planned_operating_cost);
				doc.planned_operating_cost += planned_operating_cost;
			}
			refresh_field('planned_operating_cost');
		}

		doc.additional_operating_cost = 0;
		var additional_costs = doc.additional_costs || [];
		for(let i=0; i < additional_costs.length; i++) {
			var amount = flt(flt(additional_costs[i].rate) * flt(doc.qty), precision('amount', additional_costs[i]));

			frappe.model.set_value(additional_costs[i].doctype, additional_costs[i].name, 'amount', amount);

			doc.additional_operating_cost += amount;
		}
	},

	calculate_total_cost: function(frm) {
		let variable_cost = flt(frm.doc.actual_operating_cost) || flt(frm.doc.planned_operating_cost);
		frm.set_value("total_operating_cost", (flt(frm.doc.additional_operating_cost) + variable_cost));

		frm.set_value("total_cost", frm.doc.total_operating_cost + flt(frm.doc.raw_material_cost));
	},

	set_default_warehouse: function(frm) {
		if (!(frm.doc.wip_warehouse || frm.doc.fg_warehouse)) {
			frappe.call({
				method: "erpnext.manufacturing.doctype.work_order.work_order.get_default_warehouse",
				callback: function(r) {
					if (!r.exe && r.message) {
						frm.set_value(r.message);
					}
				}
			});
		}
	},

	create_pick_list: function(frm, purpose='Material Transfer for Manufacture') {
		erpnext.manufacturing.show_prompt_for_qty_input(frm.doc, purpose)
			.then(r => {
				return frappe.xcall('erpnext.manufacturing.doctype.work_order.work_order.create_pick_list', {
					'source_name': frm.doc.name,
					'for_qty': r.data.qty
				});
			}).then(pick_list => {
				frappe.model.sync(pick_list);
				frappe.set_route('Form', pick_list.doctype, pick_list.name);
			});
	},

	make_consumption_se: function(frm, backflush_raw_materials_based_on) {
		if(!frm.doc.skip_transfer){
			var max = (backflush_raw_materials_based_on === "Material Transferred for Manufacture") ?
				flt(frm.doc.material_transferred_for_manufacturing) - flt(frm.doc.produced_qty) :
				flt(frm.doc.qty) - flt(frm.doc.produced_qty);
				// flt(frm.doc.qty) - flt(frm.doc.material_transferred_for_manufacturing);
		} else {
			var max = flt(frm.doc.qty) - flt(frm.doc.produced_qty);
		}

		frappe.call({
			method:"erpnext.manufacturing.doctype.work_order.work_order.make_stock_entry",
			args: {
				"work_order_id": frm.doc.name,
				"purpose": "Material Consumption for Manufacture",
				"qty": max
			},
			callback: function(r) {
				var doclist = frappe.model.sync(r.message);
				frappe.set_route("Form", doclist[0].doctype, doclist[0].name);
			}
		});
	},

	stop_work_order: function(frm, status) {
		frappe.call({
			method: "erpnext.manufacturing.doctype.work_order.work_order.stop_unstop",
			args: {
				work_order: frm.doc.name,
				status: status
			},
			callback: function(r) {
				if(r.message) {
					frm.set_value("status", r.message);
					frm.reload_doc();
				}
			}
		});
	}
};
