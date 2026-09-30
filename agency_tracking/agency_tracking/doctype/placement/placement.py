# Copyright (c) 2026, Agency and contributors
# License: MIT. See LICENSE

import frappe
from frappe.model.document import Document
from frappe.utils import now_datetime
from agency_tracking.state_machine import guard_status_write


class Placement(Document):
	def before_insert(self):
		# P4-13: a placement belongs to the applicant's current cycle (was always the JSON default 1).
		self.cycle_number = frappe.db.get_value("Applicant", self.applicant, "cycle_number") or 1

	def validate(self):
		guard_status_write(self, ("Selected",))  # QA A1: status only via the app's actions
		self.stamp_departed_on()
		applicant = frappe.get_doc("Applicant", self.applicant)

		# Standard: only past CV Generated (portal-selected, Step 3). Muayena: Registered is
		# their terminal intake status (Part A.1 — they never touch CV Generated at all), so
		# Registered is the floor for their direct contract-upload entry (Step 4).
		valid_applicant_status = {
			"Standard": "CV Generated",
			"Muayena": "Registered",
		}[applicant.entry_track]
		if applicant.status != valid_applicant_status:
			frappe.throw(
				f"{applicant.name} ({applicant.entry_track}) must be '{valid_applicant_status}' "
				f"before a Placement can be created (currently '{applicant.status}').",
				frappe.ValidationError,
			)

		if applicant.active_placement and applicant.active_placement != self.name:
			frappe.throw(
				f"{applicant.name} already has an active Placement ({applicant.active_placement}).",
				frappe.ValidationError,
			)

		if self.destination_country != applicant.destination_country:
			frappe.throw(
				"Placement destination_country must match the Applicant's destination_country.",
				frappe.ValidationError,
			)

	def stamp_departed_on(self):
		if self.status == "Departed" and not self.departed_on:
			self.departed_on = now_datetime()


def get_permission_query_conditions(user):
	"""S-3 (2026-09-05): clearance-country roles (Saudi/Kuwait LMIS, Taeshir, Telesign, Embassy)
	see only placements that have a clearance step of their own type. Management and every other
	internal role keep full access. Applies to frappe.get_list (Desk + list_placements)."""
	from agency_tracking.agency_tracking.doctype.clearance_step.clearance_step import scoped_clearance_step_types

	types = scoped_clearance_step_types(user)
	if types is None:
		return ""
	if not types:
		return "1=0"
	escaped = ", ".join(frappe.db.escape(t) for t in types)
	return f"`tabPlacement`.name in (select placement from `tabClearance Step` where step_type in ({escaped}))"


def has_permission(doc, ptype=None, user=None):
	"""Single-document mirror of get_permission_query_conditions above (2026-09-11, same class
	of gap found and fixed for Clearance Step/Background Job/Process Event/Applicant Transaction
	this session): every corridor role (Saudi/Kuwait LMIS, Taeshir, Telesign, Embassy) also has
	blanket DocType-level read on Placement (placement.json), so without this any of them could
	read ANY placement by name -- not just ones with a clearance step of their own type -- despite
	being correctly scoped in every list view."""
	from agency_tracking.agency_tracking.doctype.clearance_step.clearance_step import scoped_clearance_step_types

	user = user or frappe.session.user
	if not doc or not doc.get("name"):
		return True
	types = scoped_clearance_step_types(user)
	if types is None:
		return True
	if not types:
		return False
	return bool(frappe.db.exists("Clearance Step", {"placement": doc.name, "step_type": ["in", types]}))
