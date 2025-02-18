frappe.provide("frappe.treeview_settings")

frappe.treeview_settings["Department"] = {
	ignore_fields: ["parent_department"],
	get_tree_nodes: 'erpnext.hr.doctype.department.department.get_children',
	add_tree_node: 'erpnext.hr.doctype.department.department.add_node',
	filters: [{
		fieldname: "company",
		fieldtype: "Select",
		options: erpnext.utils.get_tree_options("company"),
		label: __("Company"),
		default: erpnext.utils.get_tree_default("company")
	}],
	breadcrumb: "HR",
	root_label: "All Departments",
	menu_items: [
		{
			label: __("New Department"),
			action: function() {
				frappe.new_doc("Department", true);
			},
			condition: 'frappe.boot.user.can_create.indexOf("Department") !== -1'
		}
	],
	onload: function(treeview) {
		treeview.make_tree();
	}
};
