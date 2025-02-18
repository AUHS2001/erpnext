# Copyright (c) 2015, Frappe Technologies Pvt. Ltd. and Contributors
# License: GNU General Public License v3. See license.txt

import frappe

def execute():
	# update the selling settings and set the close_opportunity_after_days
	frappe.reload_doc("crm", "doctype", "crm_settings")
	frappe.db.set_value("CRM Settings", "CRM Settings", "close_opportunity_after_days", 15)

	# Auto close Replied opportunity
	frappe.db.sql("""update `tabOpportunity` set status='Closed' where status='Replied'
		 and date_sub(curdate(), interval 15 Day)>modified""")

	# create Support Settings doctype and update close_issue_after_days
	frappe.reload_doc("support", "doctype", "support_settings")
	frappe.db.set_value("Support Settings", "Support Settings", "close_issue_after_days", 7)