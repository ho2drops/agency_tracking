# Copyright (c) 2026, Agency and contributors
# License: MIT. See LICENSE
#
# Part I Step 7: Clearance Step creation from Corridor Definition data, default assignment via
# Step Officer Mapping, and the LMIS -> Ticketing -> Departure auto-chain (Part A.2: "Officer
# holding LMIS is auto-assigned Ticketing and Departure-confirmation by default... reassignable
# by a manager if needed"). Registered into state_machine.TRANSITION_SIDE_EFFECTS at the bottom
# of this module — transition() calls these, nothing calls them directly except tests.

import frappe

from agency_tracking.corridor_engine import get_corridor_steps
from agency_tracking.notification_engine import notify
from agency_tracking.state_machine import TRANSITION_SIDE_EFFECTS

# Step types considered part of the "LMIS family" across corridors — matched by prefix rather
# than an exact-name list per corridor, consistent with Part A.3's "common step types are
# reusable across corridors" rather than each corridor needing bespoke handling.
LMIS_STEP_TYPE_PREFIX = "LMIS"


def _step_label(clearance_step_name):
	"""Clearance Step.title is already '{Step Type} — {Applicant} [{step ID}]' (see
	clearance_step.py's _set_title) -- reused here so ToDo descriptions read the same way
	instead of a bare 'Clearance Step CLR-2026-00045' a reader has to click into to identify."""
	return frappe.db.get_value("Clearance Step", clearance_step_name, "title") or clearance_step_name


def _placement_label(placement_name):
	"""'{Applicant} [{placement ID}]', mirroring _step_label -- Placement has no cached title
	field of its own, so this looks the applicant name up directly."""
	applicant = frappe.db.get_value("Placement", placement_name, "applicant")
	full_name = frappe.db.get_value("Applicant", applicant, "full_name") if applicant else None
	return f"{full_name} [{placement_name}]" if full_name else placement_name


def assign_clearance_step(clearance_step_name, user):
	"""Create a ToDo for user against this Clearance Step, closing any existing open one first
	(reassignment — "reassignable by a manager if needed")."""
	open_todos = frappe.get_all(
		"ToDo",
		filters={"reference_type": "Clearance Step", "reference_name": clearance_step_name, "status": "Open"},
		pluck="name",
	)
	for todo_name in open_todos:
		frappe.db.set_value("ToDo", todo_name, "status", "Cancelled")

	frappe.get_doc(
		{
			"doctype": "ToDo",
			"reference_type": "Clearance Step",
			"reference_name": clearance_step_name,
			"allocated_to": user,
			"description": _step_label(clearance_step_name),
			"status": "Open",
		}
	).insert(ignore_permissions=True)

	notify(user, "clearance_step_assigned", {"clearance_step": clearance_step_name})


def _broadcast_todo_to_role_holders(clearance_step_name, role):
	"""2026-08-29: for the six country+step roles, permission is role membership (see
	clearance_step.py's get_permission_query_conditions), not a single exclusive ToDo
	assignment -- so every holder gets their own open ToDo purely for the notification/queue
	UX, none of them "own" the row exclusively the way assign_clearance_step's single-officer
	model does. The two mechanisms never conflict because ToDo here is notification-only."""
	label = f"{_step_label(clearance_step_name)} ({role})"
	for user in frappe.get_all("Has Role", filters={"role": role}, pluck="parent"):
		frappe.get_doc(
			{
				"doctype": "ToDo",
				"reference_type": "Clearance Step",
				"reference_name": clearance_step_name,
				"allocated_to": user,
				"description": label,
				"status": "Open",
			}
		).insert(ignore_permissions=True)
		notify(user, "clearance_step_assigned", {"clearance_step": clearance_step_name})


def create_clearance_steps(placement, from_status=None):
	"""Placement enters Processing (Part A.2 Stage 5): materialize one Clearance Step per
	Corridor Step for this destination, in order. Notification routing: the six country+step
	roles (see clearance_step.CLEARANCE_ROLE_BY_STEP_TYPE) get a broadcast ToDo to every
	holder; anything else falls back to the legacy single Step Officer Mapping default_officer
	if one's configured."""
	from agency_tracking.agency_tracking.doctype.clearance_step.clearance_step import CLEARANCE_ROLE_BY_STEP_TYPE

	for step in get_corridor_steps(placement.destination_country):
		clearance_step = frappe.get_doc(
			{
				"doctype": "Clearance Step",
				"placement": placement.name,
				"step_type": step["step_type"],
				"sequence_order": step["sequence_order"],
				"is_mandatory": step["is_mandatory"],
				"status": "Pending",
			}
		).insert(ignore_permissions=True)

		role = CLEARANCE_ROLE_BY_STEP_TYPE.get(step["step_type"])
		if role:
			_broadcast_todo_to_role_holders(clearance_step.name, role)
			continue

		default_officer = frappe.db.get_value(
			"Step Officer Mapping", {"step_type": step["step_type"]}, "default_officer"
		)
		if default_officer:
			assign_clearance_step(clearance_step.name, default_officer)


def get_lmis_officer(placement):
	"""Whoever actually completed this placement's LMIS-family Clearance Step --
	Clearance Step.completed_by, set by clearance_api.complete_clearance_step() -- rather than
	inferred from ToDo rows.

	2026-09-16 fix: LMIS steps broadcast an open ToDo to every holder of the matching country
	role (create_clearance_steps -> _broadcast_todo_to_role_holders), not to one exclusive
	assignee -- so the previous "most recently ToDo'd" lookup was really just picking an
	arbitrary Saudi/Kuwait LMIS role holder (whichever happened to have the latest ToDo
	`creation` timestamp), unrelated to who actually did the work. With two role holders, the
	"continuity" ping meant to carry them into ticketing could land on either one. completed_by
	is set unconditionally on every real completion (and re-set on a later correction re-save,
	which is the right behavior here too -- it should track whoever most recently owned the
	outcome, not freeze on the first save)."""
	lmis_step, completed_by = frappe.db.get_value(
		"Clearance Step",
		{"placement": placement.name, "step_type": ["like", f"{LMIS_STEP_TYPE_PREFIX}%"]},
		["name", "completed_by"],
		order_by="sequence_order asc",
	) or (None, None)
	if not lmis_step:
		return None
	return completed_by


def _chain_todo_to_lmis_officer(placement, description):
	officer = get_lmis_officer(placement)
	if not officer:
		return
	if TICKETER_ROLE in frappe.get_roles(officer):
		# They already got this exact ToDo (same description) via the broadcast to every
		# Ticketer-role holder in _placement_todo_to_role, called right before this -- an LMIS
		# officer who also holds Ticketer would otherwise see "Book ticket for X" twice.
		return
	frappe.get_doc(
		{
			"doctype": "ToDo",
			"reference_type": "Placement",
			"reference_name": placement.name,
			"allocated_to": officer,
			"description": description,
			"status": "Open",
		}
	).insert(ignore_permissions=True)
	notify(officer, "placement_todo_assigned", {"placement": placement.name, "description": description})


TICKETER_ROLE = "Ticketer"


def _placement_todo_to_role(placement, role, description):
	"""Broadcast a Placement-level ToDo + push to every holder of `role` -- the same
	notification/queue UX the six clearance roles get, but for a Placement task (ticketing /
	departure) rather than a Clearance Step."""
	for user in frappe.get_all("Has Role", filters={"role": role}, pluck="parent"):
		frappe.get_doc(
			{
				"doctype": "ToDo",
				"reference_type": "Placement",
				"reference_name": placement.name,
				"allocated_to": user,
				"description": description,
				"status": "Open",
			}
		).insert(ignore_permissions=True)
		notify(user, "placement_todo_assigned", {"placement": placement.name, "description": description})


def close_open_todos(reference_type, reference_names):
	"""Close every open ToDo on these records (a name or a list of names)."""
	if isinstance(reference_names, str):
		reference_names = [reference_names]
	if not reference_names:
		return
	open_todos = frappe.get_all(
		"ToDo",
		filters={"reference_type": reference_type, "reference_name": ["in", reference_names], "status": "Open"},
		pluck="name",
	)
	for todo_name in open_todos:
		frappe.db.set_value("ToDo", todo_name, "status", "Closed")


def _close_placement_todos(placement_name):
	"""Close every open Placement-level ToDo for this placement -- the only two kinds that ever
	exist are "Book ticket..." and "Confirm departure..." (both created above), so this is safe
	to call unconditionally whenever one of those tasks is genuinely done, without needing to
	match on description text."""
	close_open_todos("Placement", placement_name)


def notify_ticketing_due(placement, from_status=None):
	"""Placement reached Stamped -> a ticket needs booking. Owned by the Ticketer(s); the LMIS
	officer who handled the case is also pinged for continuity (2026-09-05: was LMIS-only)."""
	description = f"Book ticket for {_placement_label(placement.name)}"
	_placement_todo_to_role(placement, TICKETER_ROLE, description)
	_chain_todo_to_lmis_officer(placement, description)


def notify_departure_due(placement, from_status=None):
	"""Placement reached Ticketed -> departure needs confirming. The Ticketer both books the ticket
	and confirms departure (2026-09-05: was the LMIS officer). Reaching Ticketed at all means
	ticket_recorded_gate already passed (state_machine.py) -- i.e. the ticket genuinely was
	booked -- so the "Book ticket..." ToDo(s) this closes are always done, not just superseded."""
	_close_placement_todos(placement.name)
	_placement_todo_to_role(placement, TICKETER_ROLE, f"Confirm departure for {_placement_label(placement.name)}")


TRANSITION_SIDE_EFFECTS[("Placement", "Processing")] = create_clearance_steps
TRANSITION_SIDE_EFFECTS[("Placement", "Stamped")] = notify_ticketing_due
TRANSITION_SIDE_EFFECTS[("Placement", "Ticketed")] = notify_departure_due
