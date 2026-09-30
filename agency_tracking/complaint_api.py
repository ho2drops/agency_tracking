# Copyright (c) 2026, Agency and contributors
# License: MIT. See LICENSE
#
# Part F: module-scoped whitelisted functions, no raw /api/resource/* exposure.

import frappe
from frappe.utils import today

from agency_tracking.pagination import count_rows, page_args, paged_result
from agency_tracking.roles import is_internal_staff
from agency_tracking.state_machine import transition

TERMINAL_STATUSES = {"Resolved", "Returned - Free Replacement Required", "Escalated", "Dismissed"}


@frappe.whitelist()
def create_complaint(placement=None, description=None, worker_status_at_complaint=None, **kwargs):
	"""business-workflow-srs.md Part 5: "Foreign agencies (or occasionally internal staff on
	their behalf) can log a complaint against any worker." An agency can only complain about
	their own placement; internal staff need some recognized staff role, but creation itself
	isn't restricted the way resolution is. Anyone else is refused (S-13)."""
	placement = placement or kwargs.get("placement_name")
	applicant = kwargs.get("applicant") or kwargs.get("applicant_name")
	if placement and not frappe.db.exists("Placement", placement) and frappe.db.exists("Applicant", placement):
		applicant = placement
		placement = None

	if not placement and applicant:
		placement = frappe.db.get_value("Placement", {"applicant": applicant}, "name", order_by="creation desc")

	description = description or kwargs.get("details") or "General Complaint"
	if worker_status_at_complaint not in ("Deployed", "Returned"):
		worker_status_at_complaint = "Deployed"
	if not placement or not frappe.db.exists("Placement", placement):
		# No silent fallback to an arbitrary placement (2026-09-05): a complaint must name the real
		# placement it's about, or it lands on a random worker.
		frappe.throw("A valid placement linked to an applicant is required.", frappe.ValidationError)

	linked_contractor = (
		None
		if frappe.session.user == "Administrator"
		else frappe.db.get_value("Contractor", {"user": frappe.session.user}, "name")
	)
	placement_doc = frappe.get_doc("Placement", placement)
	contractor_name = placement_doc.contractor

	if linked_contractor:
		if linked_contractor != placement_doc.contractor:
			frappe.throw("Not permitted.", frappe.PermissionError)
		raised_by = "Foreign Agency"
	elif is_internal_staff():
		raised_by = "Internal Staff"
	else:
		frappe.throw("Not permitted.", frappe.PermissionError)

	complaint = frappe.get_doc(
		{
			"doctype": "Complaint",
			"placement": placement,
			"contractor": contractor_name,
			"raised_by": raised_by,
			"worker_status_at_complaint": worker_status_at_complaint,
			"description": description,
			"status": "New",
		}
	).insert(ignore_permissions=True)
	return complaint.as_dict()


@frappe.whitelist()
def list_unresolved_complaints():
	"""business-workflow-srs.md Part 5: "sorted oldest-first so nothing quietly sits forgotten
	at the bottom of a list." """
	allowed_roles = {"Complaint Manager", "Admin", "Manager", "System Manager"}
	if frappe.session.user != "Administrator" and not (allowed_roles & set(frappe.get_roles())):
		frappe.throw("Not permitted.", frappe.PermissionError)
	return frappe.get_list(
		"Complaint",
		filters={"status": "Unresolved"},
		fields=["name", "display_no", "placement", "contractor", "description", "creation"],
		order_by="creation asc",
	)


@frappe.whitelist()
def list_new_complaints():
	"""Freshly-raised complaints (status "New") that haven't been acknowledged yet -- the triage
	inbox. These do NOT show up in list_unresolved_complaints (which is "New"->"Unresolved"),
	so without this endpoint a just-logged complaint is invisible in any listing until someone
	happens to acknowledge it. Oldest-first, same management-role gate as the other lists."""
	allowed_roles = {"Complaint Manager", "Admin", "Manager", "System Manager"}
	if frappe.session.user != "Administrator" and not (allowed_roles & set(frappe.get_roles())):
		frappe.throw("Not permitted.", frappe.PermissionError)
	return frappe.get_list(
		"Complaint",
		filters={"status": "New"},
		fields=["name", "display_no", "placement", "contractor", "raised_by", "worker_status_at_complaint",
		        "description", "status", "creation"],
		order_by="creation asc",
	)


@frappe.whitelist()
def list_complaints(status=None, limit_start=0, limit_page_length=0, with_total=0, **kwargs):
	"""All complaints, optionally filtered by a single status (e.g. "New", "Unresolved",
	"Resolved"). Oldest-first. Same management-role gate. Convenience over the two status-
	specific lists for dashboards that want the whole picture or an arbitrary status slice."""
	allowed_roles = {"Complaint Manager", "Admin", "Manager", "System Manager"}
	if frappe.session.user != "Administrator" and not (allowed_roles & set(frappe.get_roles())):
		frappe.throw("Not permitted.", frappe.PermissionError)
	status = status or kwargs.get("complaint_status")
	filters = {"status": status} if status else {}
	# default=0: still returns every complaint when no page size is given (its historical behavior).
	start, length = page_args(limit_start, limit_page_length, default=0)
	rows = frappe.get_list(
		"Complaint",
		filters=filters,
		fields=["name", "display_no", "placement", "contractor", "raised_by", "worker_status_at_complaint",
		        "description", "status", "resolution_notes", "resolved_by", "resolved_on", "creation"],
		order_by="creation asc",
		limit_start=start,
		limit_page_length=length,
	)
	return paged_result(rows, with_total, lambda: count_rows("Complaint", filters))


@frappe.whitelist()
def acknowledge_complaint(complaint_name=None, **kwargs):
	"""New -> Unresolved."""
	allowed_roles = {"Complaint Manager", "Admin", "System Manager", "Manager"}
	if frappe.session.user != "Administrator" and not (allowed_roles & set(frappe.get_roles())):
		frappe.throw("Not permitted.", frappe.PermissionError)
	complaint_name = complaint_name or kwargs.get("name")
	if not complaint_name or not frappe.db.exists("Complaint", complaint_name):
		frappe.throw("A valid complaint_name is required.", frappe.ValidationError)
	complaint = frappe.get_doc("Complaint", complaint_name)
	if complaint.status == "Unresolved":
		return complaint.as_dict()
	if complaint.status == "New":
		transition(complaint, "Unresolved")
	return complaint.as_dict()


@frappe.whitelist()
def resolve_complaint(complaint_name=None, new_status=None, resolution_notes=None, override_reason=None, **kwargs):
	"""Master spec Part A.5: "Only Complaint Manager and Admin can move resolution status."
	"""
	allowed_roles = {"Complaint Manager", "Admin", "Manager", "System Manager"}
	if frappe.session.user != "Administrator" and not (allowed_roles & set(frappe.get_roles())):
		frappe.throw("Not permitted.", frappe.PermissionError)
	complaint_name = complaint_name or kwargs.get("name") or kwargs.get("complaint")
	new_status = new_status or kwargs.get("status") or "Resolved"
	resolution_notes = resolution_notes or kwargs.get("remarks") or "Resolved by management"
	if not complaint_name or not frappe.db.exists("Complaint", complaint_name):
		frappe.throw("A valid complaint_name is required.", frappe.ValidationError)
	if new_status not in TERMINAL_STATUSES:
		frappe.throw(f"'{new_status}' is not a resolution outcome.", frappe.ValidationError)
	if new_status == "Dismissed" and not resolution_notes:
		frappe.throw("A written reason is required to dismiss a complaint.", frappe.ValidationError)

	complaint = frappe.get_doc("Complaint", complaint_name)
	if complaint.status == new_status:
		return complaint.as_dict()
	if complaint.status == "New":
		transition(complaint, "Unresolved")
	complaint.resolution_notes = resolution_notes
	complaint.resolved_by = frappe.session.user
	complaint.resolved_on = today()
	# P4-08: override only with the caller's own written reason (was always on, with a placeholder).
	transition(complaint, new_status, override=bool(override_reason), override_reason=override_reason)
	return complaint.as_dict()
