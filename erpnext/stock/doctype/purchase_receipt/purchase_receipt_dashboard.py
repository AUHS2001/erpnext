import frappe
from frappe import _

def get_data():
	return {
		'fieldname': 'purchase_receipt_no',
		'non_standard_fieldnames': {
			'Purchase Invoice': 'purchase_receipt',
			'Asset': 'purchase_receipt',
			'Landed Cost Voucher': 'receipt_document',
			'Auto Repeat': 'reference_document',
			'Purchase Receipt': 'return_against',
		},
		'internal_links': {
			'Purchase Order': ['items', 'purchase_order'],
			'Project': ['items', 'project'],
			'Quality Inspection': ['items', 'quality_inspection'],
			'Vehicle': ['items', 'vehicle'],
			'Material Request': ['items', 'material_request'],
		},
		'transactions': [
			{
				'label': _('Fulfilment'),
				'items': ['Purchase Invoice', 'Landed Cost Voucher']
			},
			{
				'label': _('Reference'),
				'items': ['Purchase Order', 'Material Request', 'Asset']
			},
			{
				'label': _('Reference'),
				'items': ['Quality Inspection', 'Project', 'Auto Repeat']
			},
			{
				'label': _('Returns'),
				'items': ['Purchase Receipt']
			},
		]
	}
