# Copyright (c) 2026, Agency and contributors
# License: MIT. See LICENSE
#
# Part E + addendum-post-spec-refinements.md's Chat section. Pure logic here (mirrors
# clearance_engine.py/finance_engine.py's role); whitelisted entry points in chat_api.py.

import frappe
from frappe.utils import now_datetime

from agency_tracking.notification_engine import notify


def validate_thread_participants(requester, other_user):
	"""Transcribed from the addendum, with one deliberate change: "is this user an agency" is
	determined by an actual linked Contractor record, not role membership. The addendum's own
	pseudocode checks `"Foreign Agency" in frappe.get_roles(...)`, which breaks for the special
	Administrator user — Administrator carries every role in the system (confirmed empirically
	in Step 4, and hit again here by this function's first draft, which wrongly rejected an
	agency messaging Administrator as "agencies cannot message each other"). Same fix shape as
	Step 4's upload_contract().
	"""
	requester_is_agency = bool(frappe.db.get_value("Contractor", {"user": requester}, "name"))
	other_is_agency = bool(frappe.db.get_value("Contractor", {"user": other_user}, "name"))

	if requester_is_agency:
		if other_is_agency:
			frappe.throw("Agencies cannot message each other.", frappe.ValidationError)
		# Communication Manager only (D-21): an agency never gets a direct line to an Admin.
		if "Communication Manager" not in frappe.get_roles(other_user):
			frappe.throw("Agencies can only message a Communication Manager.", frappe.ValidationError)


def route_agency_to_communication_manager(contractor_name):
	"""Open design call, resolved (see BUILD_LOG.md): per-contractor mapping for continuity
	when configured (Contractor.communication_manager), round-robin among all Communication
	Manager users otherwise — so an unconfigured contractor is never simply blocked."""
	contractor = frappe.get_doc("Contractor", contractor_name)
	if contractor.communication_manager:
		return contractor.communication_manager

	# A Communication Manager who opens the thread takes it. Anyone else opening it for the agency
	# (Admin, Manager) hands it to a Communication Manager below, never to themselves (D-21).
	if frappe.session.user != "Administrator" and "Communication Manager" in frappe.get_roles():
		return frappe.session.user

	managers = sorted(
		frappe.get_all(
			"Has Role", filters={"role": "Communication Manager", "parenttype": "User"}, pluck="parent"
		)
	)
	if not managers:
		return "Administrator"

	# Deterministic round-robin: how many Agency threads already exist, mod the manager count —
	# no separate "next index" counter to maintain/reset, and it's stable under concurrent
	# thread creation (worst case, two threads created in the same instant land on the same
	# manager, which is a fine outcome — not a correctness bug like the Applicant selection
	# lock in Step 3, since two threads landing on the same manager isn't a data race, just an
	# even-distribution nicety).
	existing_agency_thread_count = frappe.db.count("Chat Thread", filters={"thread_type": "Agency"})
	return managers[existing_agency_thread_count % len(managers)]


def get_or_create_agency_thread(contractor_name):
	"""Reopens an existing thread rather than duplicating — same "find or create" pattern the
	addendum specifies for create_internal_thread, applied here too since an agency should
	only ever have one thread with its Communication Manager, not a new one per conversation."""
	existing = frappe.db.get_value("Chat Thread", {"thread_type": "Agency", "contractor": contractor_name}, "name")
	if existing:
		return frappe.get_doc("Chat Thread", existing)

	contractor = frappe.get_doc("Contractor", contractor_name)
	manager = route_agency_to_communication_manager(contractor_name)
	thread = frappe.get_doc(
		{
			"doctype": "Chat Thread",
			"thread_type": "Agency",
			"contractor": contractor_name,
			"context_type": "General",
			"participants": [{"user": contractor.user}, {"user": manager}],
		}
	).insert(ignore_permissions=True)
	return thread


def get_or_create_internal_thread(user_a, user_b, context_type="General", context_reference=None):
	"""Reopens an existing thread between the same two users in the same context rather than
	duplicating (addendum: create_internal_thread "reopens an existing thread rather than
	duplicating")."""
	validate_thread_participants(user_a, user_b)
	validate_thread_participants(user_b, user_a)

	candidate_threads = frappe.get_all(
		"Chat Thread",
		filters={"thread_type": "Internal", "context_type": context_type, "context_reference": context_reference},
		pluck="name",
	)
	for thread_name in candidate_threads:
		participant_users = set(
			frappe.get_all("Chat Thread Participant", filters={"parent": thread_name}, pluck="user")
		)
		if participant_users == {user_a, user_b}:
			return frappe.get_doc("Chat Thread", thread_name)

	thread = frappe.get_doc(
		{
			"doctype": "Chat Thread",
			"thread_type": "Internal",
			"context_type": context_type,
			"context_reference": context_reference,
			"participants": [{"user": user_a}, {"user": user_b}],
		}
	).insert(ignore_permissions=True)
	return thread


def is_participant(user, thread_name):
	return frappe.db.exists("Chat Thread Participant", {"parent": thread_name, "user": user})


def deliver_message(message, thread):
	"""Part E: "Chat adds frappe.publish_realtime for instant in-app delivery when both
	parties are online, falling through to the same queued path when not." Both are fired
	unconditionally rather than trying to detect presence — publish_realtime is a no-op if
	nobody's listening on that channel, and notify() already only actually delivers if a push
	subscription/config exists, so this doesn't double-notify a genuinely-online user in any
	harmful way, it just means the queued path is always primed as a fallback."""
	for row in thread.participants:
		if row.user == message.sender:
			continue
		frappe.publish_realtime(
			event="agency_tracking:chat_message",
			message={"thread": thread.name, "sender": message.sender, "message": message.message},
			user=row.user,
		)
		notify(row.user, "chat_message", {"thread": thread.name, "sender": message.sender})


@frappe.whitelist()
def get_placement_officers(placement_name=None, **kwargs):
	"""Transcribed from the addendum verbatim — "every Placement detail view exposes who's
	currently assigned each clearance step, sourced from the same ToDo data already driving
	Clearance Step permissions." The addendum's own snippet doesn't gate this, but a Placement
	detail view already implies the caller can see that placement — added explicitly since
	placement_name is a guessable, sequential-looking ID (PLM-00001, ...), not a secret.
	"""
	placement_name = placement_name or kwargs.get("placement") or kwargs.get("name")
	if not placement_name:
		frappe.throw("placement_name is required.", frappe.ValidationError)
	if not frappe.has_permission("Placement", "read", doc=placement_name):
		frappe.throw("Not permitted.", frappe.PermissionError)
	steps = frappe.get_all(
		"Clearance Step", filters={"placement": placement_name, "status": ["!=", "Cancelled"]}, fields=["step_type", "name"]
	)

	# Batch-fetch open ToDos for every step, then every allocated user's full_name, in 2 queries
	# total instead of a query-per-step plus a query-per-ToDo (was a nested N+1).
	step_names = [s.name for s in steps]
	todos_by_step = {}
	if step_names:
		todos = frappe.get_all(
			"ToDo",
			filters={"reference_type": "Clearance Step", "reference_name": ["in", step_names], "status": "Open"},
			fields=["reference_name", "allocated_to"],
		)
		for t in todos:
			todos_by_step.setdefault(t.reference_name, []).append(t.allocated_to)

	user_names = list({u for users in todos_by_step.values() for u in users})
	full_name_by_user = {}
	if user_names:
		users = frappe.get_all("User", filters={"name": ["in", user_names]}, fields=["name", "full_name"])
		full_name_by_user = {u.name: u.full_name for u in users}

	officers = []
	for step in steps:
		for allocated_to in todos_by_step.get(step.name, []):
			officers.append(
				{
					"step_type": step.step_type,
					"user": allocated_to,
					"full_name": full_name_by_user.get(allocated_to),
				}
			)
	return officers
