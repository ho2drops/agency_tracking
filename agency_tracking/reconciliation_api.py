# Copyright (c) 2026, Agency and contributors
# License: MIT. See LICENSE
#
# Part F: module-scoped whitelisted functions, no raw /api/resource/* exposure.

import frappe

from agency_tracking.finance_engine import birr_conversion_on, settle_batch_request
from agency_tracking.reconciliation_engine import match_statement_lines, parse_bank_statement_csv


def _require_finance_role():
	if not ({"Finance Manager", "Admin"} & set(frappe.get_roles())):
		frappe.throw("Not permitted.", frappe.PermissionError)


@frappe.whitelist()
def upload_bank_statement(file_url=None, url=None, file=None, statement_file=None, csv_content=None, currency=None, **kwargs):
	"""currency: which currency the statement's account is in. Required when amounts are not
	converted to Birr, because a line is then matched to an invoice in the invoice's own currency."""
	_require_finance_role()
	file_url = file_url or url or file or statement_file or kwargs.get("file_url")
	csv_content = csv_content or kwargs.get("content")
	if not file_url and not csv_content:
		frappe.throw("file_url or csv_content is required.", frappe.ValidationError)
	if not currency and not birr_conversion_on():
		frappe.throw("Say which currency this statement is in.", frappe.ValidationError)
	rows = parse_bank_statement_csv(file_url=file_url, csv_content=csv_content)
	statement = frappe.get_doc(
		{
			"doctype": "Bank Statement",
			"statement_file": file_url or "direct-input.csv",
			"currency": currency,
			"uploaded_by": frappe.session.user,
			"status": "Uploaded",
			"lines": rows,
		}
	).insert(ignore_permissions=True)
	match_statement_lines(statement)
	return statement.as_dict()


@frappe.whitelist()
def manually_match_line(statement_line_name=None, batch_name=None, line_name=None, batch=None, **kwargs):
	"""Escape hatch for lines the automatic matcher couldn't confidently resolve (ambiguous
	amount collisions, missing reference text) — Finance Manager/Admin only, same shape as
	every other manual-override path in this build."""
	_require_finance_role()
	statement_line_name = statement_line_name or line_name or kwargs.get("statement_line")
	batch_name = batch_name or batch or kwargs.get("batch_name")
	if not statement_line_name or not batch_name:
		frappe.throw("Both statement_line_name and batch_name are required.", frappe.ValidationError)
	line = frappe.get_doc("Bank Statement Line", statement_line_name)
	line.matched_batch = batch_name
	line.match_status = "Manually Matched"
	line.save(ignore_permissions=True)
	settle_batch_request(batch_name, line.reference or f"Manually matched: {statement_line_name}")
	return line.as_dict()
