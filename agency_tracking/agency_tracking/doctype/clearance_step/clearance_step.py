# Copyright (c) 2026, Agency and contributors
# License: MIT. See LICENSE

import frappe
from frappe.model.document import Document
from agency_tracking.state_machine import guard_status_write

# 2026-08-29: role-based access replaces per-row ToDo *permission* for the six country+step
# roles (Clearance Officer/Ticketer keep the old ToDo-scoped model). Anyone holding the mapped
# role can read/act on every Clearance Step row of that type, full stop -- ToDo assignment is
# kept only for the notification/queue UX (clearance_engine.create_clearance_steps still
# creates one per matching-role user), never as the permission gate, so the two mechanisms
# can't disagree with each other.
CLEARANCE_ROLE_BY_STEP_TYPE = {
	"LMIS Clearance": "Saudi LMIS",
	"Taeshir": "Saudi Taeshir",
	"Embassy": "Saudi Embassy",
	"Kuwait LMIS": "Kuwait LMIS",
	"Telesign": "Kuwait Telesign",
	"Kuwait Embassy": "Kuwait Embassy",
}

CLEARANCE_COUNTRY_ROLES = set(CLEARANCE_ROLE_BY_STEP_TYPE.values())
_MGMT_ROLES = {"Manager", "Admin", "System Manager", "Administrator"}


def scoped_clearance_step_types(user=None):
	"""S-3 row-scoping (2026-09-05): to which clearance step_types is a user's Placement/Applicant
	visibility limited?

	  None   -> full access (management, or any non-clearance app role -- Registrar, Contract Parser,
	            Ticketer, Finance Manager, Complaint/Communication Manager, Medical Officer,
	            Clearance Officer).
	  [types]-> the user holds ONLY clearance-country role(s), so they see only placements/applicants
	            that have a step of these types.
	  []     -> no relevant app role -> nothing (defensive).
	"""
	if not user:
		user = frappe.session.user
	roles = set(frappe.get_roles(user))
	if _MGMT_ROLES & roles:
		return None
	from agency_tracking.install import ROLES as APP_ROLES

	app_roles = roles & set(APP_ROLES)
	country = app_roles & CLEARANCE_COUNTRY_ROLES
	if app_roles - country:  # holds a non-clearance app role -> full access
		return None
	if country:
		return [st for st, role in CLEARANCE_ROLE_BY_STEP_TYPE.items() if role in country]
	return []


class ClearanceStep(Document):
	def validate(self):
		guard_status_write(self, ("Pending",))  # QA A1: status only via the app's actions
		if self.status == "Rejected" and not self.rejection_remark:
			frappe.throw(
				"A rejection remark is required when an Embassy step is Rejected.",
				frappe.ValidationError,
			)
		self._require_paid_injaz_to_complete_taeshir()
		self._keep_lmis_status_in_step()
		self._set_title()

	def _keep_lmis_status_in_step(self):
		"""An LMIS step's working status says ISSUED exactly when the step is Issued, however it got
		there (Issue button, grid, undo). Other step types have no LMIS status."""
		from agency_tracking.clearance_api import LMIS_ISSUED, LMIS_STEP_TYPES

		if self.step_type not in LMIS_STEP_TYPES:
			self.lmis_status = None
		elif self.status == "Issued":
			self.lmis_status = LMIS_ISSUED
		elif self.lmis_status == LMIS_ISSUED:
			self.lmis_status = None

	def _require_paid_injaz_to_complete_taeshir(self):
		"""2026-09-23: Taeshir can't be completed until the Injaz attempt it completes on is Paid
		(record_injaz_payment). Checked only on the move INTO a done status, so correction re-saves
		of an already-done step aren't blocked. Here rather than in complete_clearance_step so the
		Desk form and every other completion path are covered too."""
		from agency_tracking.stage_fees import final_injaz_attempt
		from agency_tracking.state_machine import CLEARANCE_STEP_DONE_STATUSES

		if self.step_type != "Taeshir" or self.status not in CLEARANCE_STEP_DONE_STATUSES:
			return
		before = self.get_doc_before_save()
		if before and before.status in CLEARANCE_STEP_DONE_STATUSES:
			return
		attempt = final_injaz_attempt(self)
		if not attempt or attempt.payment_status != "Paid":
			frappe.throw(
				"Taeshir can't be completed until the Injaz payment is marked Paid (record_injaz_payment).",
				frappe.ValidationError,
			)

	def _set_title(self):
		"""'{Step Type} — {Applicant} [{step ID}]', e.g. 'Embassy — Fatuma Muhammed Abi
		[CLR-2026-00045]' -- so this step reads as something other than a bare sequence number
		everywhere it shows up (lists, link fields, notifications). self.name is already assigned
		by the time validate() runs (autoname happens before the before_save hooks)."""
		applicant_name = frappe.db.get_value("Placement", self.placement, "applicant") if self.placement else None
		full_name = frappe.db.get_value("Applicant", applicant_name, "full_name") if applicant_name else None
		if self.step_type and full_name and self.name:
			self.title = f"{self.step_type} — {full_name} [{self.name}]"
		else:
			self.title = self.name

	def on_update(self):
		# Client item #12 (2026-09-23): a completed step records its configured corridor fees as
		# auto-Approved Expenses. Idempotent, so a correction never re-posts. Undoing the step voids
		# the fee, and finishing it again records it again (reversal.py, Stage B).
		from agency_tracking.stage_fees import post_step_fees

		post_step_fees(self)

	def before_save(self):
		from agency_tracking.storage_engine import migrate_attach_to_r2

		applicant_name = frappe.db.get_value("Placement", self.placement, "applicant") if self.placement else None
		# Injaz receipt photos now live per-attempt (Injaz Attempt.receipt_photo).
		for attempt in self.get("injaz_attempts") or []:
			migrate_attach_to_r2(attempt, "receipt_photo", "injaz", applicant_name=applicant_name)
		for payment in self.get("payments") or []:
			migrate_attach_to_r2(payment, "receipt_url", "finance-receipts", applicant_name=applicant_name)


def has_permission(doc, ptype=None, user=None):
	"""Single-document permission gate (2026-09-11). get_permission_query_conditions below only
	filters list/report queries -- Frappe does NOT consult it for a single frappe.get_doc(),
	doc.save(), or the generic REST /api/resource/Clearance Step/<name> read or write. Without
	this, a role with blanket DocType-level write (every clearance-country role has one) could
	act on ANY Clearance Step row by name regardless of step_type, even though it never appears
	in any list/report for them -- confirmed live: a Saudi-Taeshir-only user could
	frappe.client.set_value() an Embassy step's Wakala fields despite that row being completely
	invisible to them in every list view. Mirrors get_permission_query_conditions' own row
	scoping so the two can't disagree: management always; the officer with an open ToDo on this
	exact row (Clearance Officer/Ticketer's per-row model); or whoever holds the role mapped to
	this row's own step_type (the six country+step roles' per-step_type model)."""
	user = user or frappe.session.user
	roles = set(frappe.get_roles(user))
	if _MGMT_ROLES & roles:
		return True
	if not doc or not doc.get("name"):
		# No existing row to scope by (e.g. a create check on an unsaved doc) -- defer to the
		# blanket DocType-level role permissions already in clearance_step.json.
		return True
	if {"Clearance Officer", "Ticketer"} & roles:
		if frappe.db.exists(
			"ToDo",
			{
				"reference_type": "Clearance Step",
				"reference_name": doc.name,
				"allocated_to": user,
				"status": "Open",
			},
		):
			return True
	required_role = CLEARANCE_ROLE_BY_STEP_TYPE.get(doc.get("step_type"))
	return bool(required_role and required_role in roles)


def get_permission_query_conditions(user):
	"""Part G, extended 2026-08-29: Clearance Officer / Ticketer still see rows only via ToDo
	assignment (per-row, cross-step-type). The six country+step roles instead see *every* row
	of their own step_type, regardless of ToDo assignment."""
	if not user:
		user = frappe.session.user
	roles = set(frappe.get_roles(user))
	if {"Admin", "Manager", "System Manager"} & roles:
		return ""

	conditions = []
	if {"Clearance Officer", "Ticketer"} & roles:
		conditions.append(
			"`tabClearance Step`.name in ("
			"select reference_name from `tabToDo` "
			"where reference_type='Clearance Step' "
			f"and allocated_to={frappe.db.escape(user)} and status='Open')"
		)
	matching_step_types = [
		step_type for step_type, role in CLEARANCE_ROLE_BY_STEP_TYPE.items() if role in roles
	]
	if matching_step_types:
		escaped = ", ".join(frappe.db.escape(st) for st in matching_step_types)
		conditions.append(f"`tabClearance Step`.step_type in ({escaped})")

	if not conditions:
		return "1=0"
	return "(" + " or ".join(conditions) + ")"
