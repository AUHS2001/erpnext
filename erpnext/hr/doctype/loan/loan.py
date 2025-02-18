# -*- coding: utf-8 -*-
# Copyright (c) 2015, Frappe Technologies Pvt. Ltd. and contributors
# For license information, please see license.txt

import frappe, math, json
import erpnext
from frappe import _
from frappe.utils import flt, rounded, add_months, nowdate, getdate
from erpnext.controllers.accounts_controller import AccountsController


class Loan(AccountsController):
	def validate(self):
		validate_repayment_method(self.repayment_method, self.loan_amount, self.monthly_repayment_amount, self.repayment_periods)
		self.set_missing_fields()
		self.make_repayment_schedule()
		self.set_repayment_period()
		self.validate_repayment_schedule_amount()
		self.calculate_totals()
		self.set_status()

	def on_cancel(self):
		self.db_set('status', 'Cancelled')

	def before_update_after_submit(self):
		self.validate_cannot_change_schedule()
		if not self.rate_of_interest:
			self.update_repayment_schedule()
		self.validate_repayment_schedule_amount()
		self.calculate_totals()

	def set_missing_fields(self):
		if not self.company:
			self.company = erpnext.get_default_company()

		if not self.posting_date:
			self.posting_date = nowdate()

		if self.loan_type and not self.rate_of_interest:
			self.rate_of_interest = frappe.db.get_value("Loan Type", self.loan_type, "rate_of_interest")

		if self.repayment_method == "Repay Over Number of Periods":
			self.monthly_repayment_amount = get_monthly_repayment_amount(self.repayment_method, self.loan_amount, self.rate_of_interest, self.repayment_periods)

		if self.status == "Repaid/Closed":
			self.total_amount_paid = self.total_payment

	def make_repayment_schedule(self):
		self.repayment_schedule = []
		payment_date = self.repayment_start_date
		balance_amount = self.loan_amount
		while balance_amount > 0:
			interest_amount = rounded(balance_amount * flt(self.rate_of_interest) / (12*100))
			principal_amount = self.monthly_repayment_amount - interest_amount
			balance_amount = rounded(balance_amount + interest_amount - self.monthly_repayment_amount)

			if balance_amount < 0:
				principal_amount += balance_amount
				balance_amount = 0.0

			total_payment = principal_amount + interest_amount
			self.append("repayment_schedule", {
				"payment_date": payment_date,
				"principal_amount": principal_amount,
				"interest_amount": interest_amount,
				"total_payment": total_payment,
				"balance_loan_amount": balance_amount
			})
			next_payment_date = add_months(payment_date, 1)
			payment_date = next_payment_date

	def set_repayment_period(self):
		if self.repayment_method == "Repay Fixed Amount per Period":
			repayment_periods = len(self.repayment_schedule)

			self.repayment_periods = repayment_periods

	@frappe.whitelist()
	def update_repayment_schedule(self):
		if self.rate_of_interest:
			frappe.throw(_("Cannot change repayment schedule for loan with interest"))

		balance_amount = self.loan_amount
		for d in self.repayment_schedule:
			balance_amount = rounded(balance_amount - flt(d.principal_amount))
			d.balance_loan_amount = balance_amount
			d.total_payment = d.principal_amount

	def validate_cannot_change_schedule(self):
		has_changes = False

		schedule_row_names = [d.name for d in self.repayment_schedule]
		if not schedule_row_names:
			frappe.throw(_("Repayment schedule is empty"))

		paid_schedule_removed = frappe.db.sql("""
			select name
			from `tabRepayment Schedule`
			where parenttype = %s and parent = %s and paid = 1 and name not in %s
		""", (self.doctype, self.name, schedule_row_names))

		if paid_schedule_removed:
			frappe.throw(_("Cannot remove paid schedule"))

		for d in self.repayment_schedule:
			previous_values = frappe.db.get_value("Repayment Schedule", filters={
				"name": d.name,
				"parent": self.name,
				"parenttype": self.doctype,
			}, fieldname=['paid', 'payment_date', 'principal_amount'], as_dict=1)

			row_changed = False

			if not previous_values:
				row_changed = True
			else:
				if getdate(d.payment_date) != previous_values.payment_date:
					row_changed = True
				if flt(d.principal_amount) != flt(previous_values.principal_amount):
					row_changed = True

			if row_changed:
				has_changes = True

				if previous_values and previous_values.paid:
					frappe.throw(_("Row {0}: Cannot change repayment schedule because it is already paid").format(d.idx))

		if has_changes and self.rate_of_interest:
			frappe.throw(_("Cannot change repayment schedule for loan with interest"))

	def validate_repayment_schedule_amount(self):
		loan_amount_df = self.meta.get_field('loan_amount')

		total_principal_amount = sum([d.principal_amount for d in self.repayment_schedule])
		if flt(total_principal_amount, self.precision('loan_amount')) != flt(self.loan_amount, self.precision('loan_amount')):
			frappe.throw(_("Total Repayment Schedule Principal Amount {0} does not match Loan Amount {1}")
				.format(
					frappe.bold(frappe.format(total_principal_amount, df=loan_amount_df)),
					frappe.bold(frappe.format(self.loan_amount, df=loan_amount_df))
				))

	def calculate_totals(self):
		self.total_payment = 0
		self.total_interest_payable = 0
		self.total_amount_paid = 0
		for schedule in self.repayment_schedule:
			self.total_payment += flt(schedule.total_payment)
			self.total_interest_payable += flt(schedule.interest_amount)
			if schedule.paid:
				self.total_amount_paid += flt(schedule.total_payment)

	def update_total_amount_paid(self, update_modified=True):
		self.total_amount_paid = 0
		for schedule in self.repayment_schedule:
			if schedule.paid:
				self.total_amount_paid += schedule.total_payment

		self.db_set("total_amount_paid", self.total_amount_paid, update_modified=update_modified)

	def set_status(self, update=False, status=None, update_modified=True):
		disbursement_date = None

		if self.docstatus == 0:
			self.status = "Draft"
		elif self.docstatus == 2:
			self.status = "Cancelled"
		else:
			self.status = "Sanctioned"

			disbursement = self.get_disbursement_entry()
			if disbursement:
				self.validate_disbursed_amount_and_loan_amount(disbursement.disbursed_amount)
				if disbursement.disbursed_amount == self.loan_amount and disbursement.disbursed_amount != 0:
					self.status = "Disbursed"
					disbursement_date = disbursement.posting_date

			if self.total_amount_paid == self.total_payment:
				self.status = "Repaid/Closed"

		self.disbursement_date = disbursement_date
		if update:
			self.db_set({
				"status": self.status,
				"disbursement_date": self.disbursement_date
			}, update_modified=update_modified)

	def validate_disbursement_date(self):
		if self.disbursement_date and getdate(self.disbursement_date) > getdate(self.repayment_start_date):
			frappe.throw(_("Disbursement Date cannot be after Loan Repayment Start Date"))

	def validate_disbursed_amount_and_loan_amount(self, disbursed_amount):
		if disbursed_amount > self.loan_amount:
			frappe.throw(_("Disbursed Amount cannot be greater than Loan Amount {0}").format(self.loan_amount))

	def get_disbursement_entry(self):
		return frappe.db.sql("""
			select posting_date, ifnull(sum(debit-credit), 0) as disbursed_amount
			from `tabGL Entry`
			where account = %s and against_voucher_type = 'Loan' and against_voucher = %s and debit-credit > 0
		""", (self.loan_account, self.name), as_dict=1)[0]

	def make_jv_entry(self):
		self.check_permission('write')
		journal_entry = frappe.new_doc('Journal Entry')
		journal_entry.voucher_type = 'Bank Entry'
		journal_entry.user_remark = _('Against Loan: {0}').format(self.name)
		journal_entry.company = self.company
		journal_entry.posting_date = nowdate()

		account_amt_list = []

		account_amt_list.append({
			"account": self.loan_account,
			"party_type": self.applicant_type,
			"party": self.applicant,
			"debit_in_account_currency": self.loan_amount,
			"reference_type": "Loan",
			"reference_name": self.name,
			})
		account_amt_list.append({
			"account": self.payment_account,
			"credit_in_account_currency": self.loan_amount,
			"reference_type": "Loan",
			"reference_name": self.name,
			})
		journal_entry.set("accounts", account_amt_list)
		return journal_entry.as_dict()


def validate_repayment_method(repayment_method, loan_amount, monthly_repayment_amount, repayment_periods):
	if repayment_method == "Repay Over Number of Periods" and not repayment_periods:
		frappe.throw(_("Please enter Repayment Periods"))

	if repayment_method == "Repay Fixed Amount per Period":
		if not monthly_repayment_amount:
			frappe.throw(_("Please enter repayment Amount"))
		if monthly_repayment_amount > loan_amount:
			frappe.throw(_("Monthly Repayment Amount cannot be greater than Loan Amount"))


def get_monthly_repayment_amount(repayment_method, loan_amount, rate_of_interest, repayment_periods):
	if rate_of_interest:
		monthly_interest_rate = flt(rate_of_interest) / (12 *100)
		monthly_repayment_amount = math.ceil((loan_amount * monthly_interest_rate *
			(1 + monthly_interest_rate)**repayment_periods) \
			/ ((1 + monthly_interest_rate)**repayment_periods - 1))
	else:
		monthly_repayment_amount = math.ceil(flt(loan_amount) / repayment_periods)
	return monthly_repayment_amount


@frappe.whitelist()
def get_loan_application(loan_application):
	loan = frappe.get_doc("Loan Application", loan_application)
	if loan:
		return loan.as_dict()


@frappe.whitelist()
def make_repayment_entry(payment_rows, loan, company, loan_account, applicant_type, applicant, \
	payment_account=None, interest_income_account=None):

	if isinstance(payment_rows, str):
		payment_rows_list = json.loads(payment_rows)
	else:
		frappe.throw(_("No repayments available for Journal Entry"))

	if payment_rows_list:
		row_name = list(set(d["name"] for d in payment_rows_list))
	else:
		frappe.throw(_("No repayments selected for Journal Entry"))
	total_payment = 0
	principal_amount = 0
	interest_amount = 0
	for d in payment_rows_list:
		total_payment += d["total_payment"]
		principal_amount += d["principal_amount"]
		interest_amount += d["interest_amount"]

	journal_entry = frappe.new_doc('Journal Entry')
	journal_entry.voucher_type = 'Bank Entry'
	journal_entry.user_remark = _('Against Loan: {0}').format(loan)
	journal_entry.company = company
	journal_entry.posting_date = nowdate()
	journal_entry.paid_loan = json.dumps(row_name)
	account_amt_list = []

	account_amt_list.append({
		"account": payment_account,
		"debit_in_account_currency": total_payment,
		"reference_type": "Loan",
		"reference_name": loan,
		})
	account_amt_list.append({
		"account": loan_account,
		"credit_in_account_currency": principal_amount,
		"party_type": applicant_type,
		"party": applicant,
		"reference_type": "Loan",
		"reference_name": loan,
		})

	if interest_amount:
		account_amt_list.append({
			"account": interest_income_account,
			"credit_in_account_currency": interest_amount,
			"reference_type": "Loan",
			"reference_name": loan,
			})

	journal_entry.set("accounts", account_amt_list)

	return journal_entry.as_dict()


@frappe.whitelist()
def make_jv_entry(loan, company, loan_account, applicant_type, applicant, loan_amount,payment_account=None):

	journal_entry = frappe.new_doc('Journal Entry')
	journal_entry.voucher_type = 'Bank Entry'
	journal_entry.user_remark = _('Against Loan: {0}').format(loan)
	journal_entry.company = company
	journal_entry.posting_date = nowdate()
	account_amt_list = []

	account_amt_list.append({
		"account": loan_account,
		"debit_in_account_currency": loan_amount,
		"party_type": applicant_type,
		"party": applicant,
		"reference_type": "Loan",
		"reference_name": loan,
		})
	account_amt_list.append({
		"account": payment_account,
		"credit_in_account_currency": loan_amount,
		"reference_type": "Loan",
		"reference_name": loan,
		})
	journal_entry.set("accounts", account_amt_list)
	return journal_entry.as_dict()
