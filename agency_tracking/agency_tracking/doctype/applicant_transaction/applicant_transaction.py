# Copyright (c) 2026, Agency and contributors
# License: MIT. See LICENSE

import frappe
from frappe.model.document import Document
from agency_tracking.state_machine import guard_locked_fields, guard_status_write


class ApplicantTransaction(Document):
	def validate(self):
		guard_status_write(self, ("Pending",))  # QA A1: status only via the app's actions
		guard_locked_fields(self, ("Approved", "Voided"), ("transaction_type", "amount_original", "currency_original", "fx_rate", "fx_rate_date", "amount_birr"))  # QA P5-07
		# fx_rate/fx_rate_date are required unless the row is awaiting an FX rate (2026-09-23).
		# Enforced here because mandatory_depends_on is only applied in the Desk form, not on the
		# server -- the fields used to be plain reqd.
		if not self.awaiting_fx_rate and (self.fx_rate is None or not self.fx_rate_date):
			frappe.throw(
				"FX Rate and FX Rate Date are required (unless the transaction is awaiting an FX rate).",
				frappe.MandatoryError,
			)

		# Defense in depth: amount_birr must always be the product of the two figures that
		# produced it, regardless of which code path created this row.
		self.amount_birr = round((self.amount_original or 0) * (self.fx_rate or 0), 2)

		if self.placement and not self.cycle_number:
			self.cycle_number = frappe.db.get_value("Placement", self.placement, "cycle_number")

	def before_save(self):
		from agency_tracking.storage_engine import migrate_attach_to_r2

		applicant_name = self.applicant or (
			frappe.db.get_value("Placement", self.placement, "applicant") if self.placement else None
		)
		migrate_attach_to_r2(self, "receipt_image", "finance-receipts", applicant_name=applicant_name)


def get_permission_query_conditions(user):
	"""Part D + 2026-08-29: Finance Manager/Admin see every row (the full ledger). Everyone
	else who's allowed to log an entry (any internal staff role, per doctype-level create
	permission) can only see their *own* rows -- not "1=0 for everyone else" anymore, since
	that would make it impossible for staff to review what they themselves already submitted.
	"""
	if not user:
		user = frappe.session.user
	if {"Finance Manager", "Admin"} & set(frappe.get_roles(user)):
		return ""
	return f"`tabApplicant Transaction`.logged_by = {frappe.db.escape(user)}"


def has_permission(doc, ptype=None, user=None):
	"""Single-document mirror of get_permission_query_conditions above (2026-09-11, same class
	of gap found and fixed for Clearance Step/Background Job earlier this session): every
	internal-staff role that can create a transaction (Registrar, Clearance Officer, Ticketer,
	Complaint Manager, Contract Parser, all six corridor roles -- per
	applicant_transaction.json) also has blanket DocType-level read, so without this any of them
	could read ANY Applicant Transaction by name -- amounts, currency, applicant, everything --
	despite being restricted to rows they themselves logged in every list view."""
	user = user or frappe.session.user
	if not doc or not doc.get("name"):
		return True
	if {"Finance Manager", "Admin"} & set(frappe.get_roles(user)):
		return True
	return doc.get("logged_by") == user
