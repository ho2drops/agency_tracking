# Copyright (c) 2026, Agency and contributors
# License: MIT. See LICENSE
#
# Part F names this file explicitly: chat_api.py (create_thread, send_message, list_threads,
# get_thread_messages, mark_read). No raw /api/resource/* exposure — Chat Thread/Chat Message
# doctype permissions grant nothing beyond System Manager (see their JSONs); every access path
# is mediated here with its own explicit checks.

import frappe
from frappe.utils import now_datetime

from agency_tracking.chat_engine import (
	deliver_message,
	get_or_create_agency_thread,
	get_or_create_internal_thread,
	is_participant,
	validate_thread_participants,
)


def _linked_contractor(user):
	if user == "Administrator":
		return None
	return frappe.db.get_value("Contractor", {"user": user}, "name")


@frappe.whitelist()
def create_agency_thread(contractor=None, **kwargs):
	"""Foreign Agency opens their support thread (no params needed).
	Admin, Manager, and Communication Manager can also pass `contractor` (Contractor name or user email)
	to open/create a thread with that agency directly."""
	contractor = contractor or kwargs.get("contractor_name") or kwargs.get("agency") or kwargs.get("agency_name")
	if contractor:
		if frappe.session.user != "Administrator" and not ({"Communication Manager", "Manager", "Admin", "System Manager"} & set(frappe.get_roles())):
			frappe.throw("Not permitted.", frappe.PermissionError)
		contractor_name = (
			frappe.db.get_value("Contractor", contractor, "name")
			or frappe.db.get_value("Contractor", {"user": contractor}, "name")
			or frappe.db.get_value("Contractor", {"contractor_name": contractor}, "name")
		)
		if not contractor_name:
			frappe.throw(f"Contractor '{contractor}' not found.", frappe.DoesNotExistError)
	else:
		contractor_name = _linked_contractor(frappe.session.user)
		if not contractor_name:
			frappe.throw("Not permitted. Pass a 'contractor' parameter to open an agency chat as staff.", frappe.PermissionError)

	thread = get_or_create_agency_thread(contractor_name)
	return thread.as_dict()


@frappe.whitelist()
def create_internal_thread(other_user=None, context_type="General", context_reference=None, **kwargs):
	"""Internal staff only — "internal chat is open between all staff, no role restriction,"
	but never a route into an agency's thread (validate_thread_participants blocks a Foreign
	Agency target here just as it blocks one as the requester elsewhere)."""
	other_user = other_user or kwargs.get("user") or kwargs.get("user_email") or kwargs.get("target_user") or kwargs.get("recipient") or kwargs.get("with_user")
	context_type = context_type or kwargs.get("type") or "General"
	context_reference = context_reference or kwargs.get("reference") or kwargs.get("docname") or kwargs.get("applicant") or kwargs.get("placement")

	if not other_user:
		frappe.throw("'other_user' (or 'user') is required.", frappe.ValidationError)

	if _linked_contractor(frappe.session.user):
		frappe.throw(
			"Agencies use create_agency_thread(), not create_internal_thread().", frappe.PermissionError
		)
	thread = get_or_create_internal_thread(frappe.session.user, other_user, context_type, context_reference)
	return thread.as_dict()


@frappe.whitelist()
def send_message(thread_name=None, message=None, mentioned_applicant=None, mentioned_placement=None, attachment=None, **kwargs):
	thread_name = thread_name or kwargs.get("thread_id") or kwargs.get("thread")
	message = message or kwargs.get("content") or kwargs.get("text")
	if not thread_name:
		frappe.throw("thread_name is required.", frappe.ValidationError)
	if frappe.session.user != "Administrator" and not is_participant(frappe.session.user, thread_name):
		frappe.throw("Not permitted.", frappe.PermissionError)
	if not (message or attachment):
		frappe.throw("A message must have text or an attachment.", frappe.ValidationError)

	# addendum: "the mention is a link, not a permission grant" — read access to the mentioned
	# record still goes through its own permission check, so a mention can't be used to prove
	# a record's existence to someone who couldn't otherwise see it.
	if mentioned_applicant and not frappe.has_permission("Applicant", "read", doc=mentioned_applicant):
		frappe.throw("Not permitted to mention this Applicant.", frappe.PermissionError)
	if mentioned_placement and not frappe.has_permission("Placement", "read", doc=mentioned_placement):
		frappe.throw("Not permitted to mention this Placement.", frappe.PermissionError)

	msg = frappe.get_doc(
		{
			"doctype": "Chat Message",
			"thread": thread_name,
			"sender": frappe.session.user,
			"message": message,
			"attachment": attachment,
			"mentioned_applicant": mentioned_applicant,
			"mentioned_placement": mentioned_placement,
		}
	).insert(ignore_permissions=True)

	frappe.db.set_value("Chat Thread", thread_name, "last_message_at", now_datetime())
	deliver_message(msg, frappe.get_doc("Chat Thread", thread_name))
	return msg.as_dict()


@frappe.whitelist()
def list_threads():
	"""addendum: "Agencies cannot know other agencies exist... this needs an explicit test,
	not just reliance on the participant filter." Belt-and-suspenders: the participant filter
	alone should already be sufficient (an agency user is never a participant on another
	agency's thread), but a Foreign Agency caller gets an *additional*, independent filter by
	their own contractor — so even a hypothetical bug that let a stray participant row through
	still couldn't leak another agency's thread to this endpoint.
	"""
	thread_names = frappe.get_all(
		"Chat Thread Participant", filters={"user": frappe.session.user}, pluck="parent"
	)
	filters = {"name": ["in", thread_names or [""]]}

	contractor_name = _linked_contractor(frappe.session.user)
	if contractor_name:
		filters["contractor"] = contractor_name

	threads = frappe.get_all(
		"Chat Thread",
		filters=filters,
		fields=["name", "thread_type", "contractor", "context_type", "context_reference", "last_message_at", "creation"],
		order_by="last_message_at desc",
	)
	# The caller is a participant of every thread here, so its participant list is theirs to see
	# (the frontend proxy used to fetch this with a system token -- QA P8-05).
	participants_by_thread = _participants_by_thread([t["name"] for t in threads])
	for t in threads:
		t["participants"] = participants_by_thread.get(t["name"], [])
	return threads


def _participants_by_thread(thread_names):
	"""{thread: [user, ...]} in one query."""
	out = {}
	if thread_names:
		for r in frappe.get_all(
			"Chat Thread Participant", filters={"parent": ["in", thread_names]}, fields=["parent", "user"]
		):
			out.setdefault(r.parent, []).append(r.user)
	return out


@frappe.whitelist()
def list_all_threads():
	"""Agency-wide thread listing for oversight roles (Admin, Manager, Communication Manager, System Manager)."""
	oversight_roles = {"Admin", "Manager", "Communication Manager", "System Manager", "Administrator"}
	if not (oversight_roles & set(frappe.get_roles())):
		frappe.throw("Not permitted.", frappe.PermissionError)

	threads = frappe.get_all(
		"Chat Thread",
		fields=["name", "thread_type", "contractor", "context_type", "context_reference", "last_message_at", "creation"],
		order_by="last_message_at desc, creation desc",
	)
	# Batch-fetch every thread's participants in 1 query instead of 1 query per thread (was N+1).
	participants_by_thread = _participants_by_thread([t["name"] for t in threads])
	for t in threads:
		t["participants"] = participants_by_thread.get(t["name"], [])
	return threads


@frappe.whitelist()
def get_thread_messages(thread_name=None, **kwargs):
	thread_name = _readable_thread(thread_name or kwargs.get("thread_id") or kwargs.get("thread"))
	return frappe.get_all(
		"Chat Message",
		filters={"thread": thread_name},
		fields=["name", "sender", "message", "attachment", "mentioned_applicant", "mentioned_placement", "creation"],
		order_by="creation asc",
	)


@frappe.whitelist()
def get_thread_participants(thread_name=None, **kwargs):
	"""Participants and their read receipts, for whoever may read the thread's messages (same gate
	as get_thread_messages). The frontend proxy used to read the Chat Thread doc with a system
	token for this -- QA P8-05."""
	thread_name = _readable_thread(thread_name or kwargs.get("thread_id") or kwargs.get("thread"))
	return frappe.get_all(
		"Chat Thread Participant",
		filters={"parent": thread_name, "parenttype": "Chat Thread"},
		fields=["user", "last_read_at"],
		order_by="idx asc",
	)


def _readable_thread(thread_name):
	"""Thread read gate: participants, plus the oversight roles."""
	if not thread_name:
		frappe.throw("thread_name is required.", frappe.ValidationError)
	oversight_roles = {"Admin", "Manager", "Communication Manager", "System Manager", "Administrator"}
	if not (oversight_roles & set(frappe.get_roles())) and not is_participant(frappe.session.user, thread_name):
		frappe.throw("Not permitted.", frappe.PermissionError)
	return thread_name


@frappe.whitelist()
def mark_read(thread_name=None, **kwargs):
	thread_name = thread_name or kwargs.get("thread_id") or kwargs.get("thread")
	if not thread_name:
		frappe.throw("thread_name is required.", frappe.ValidationError)
	if frappe.session.user != "Administrator" and not is_participant(frappe.session.user, thread_name):
		frappe.throw("Not permitted.", frappe.PermissionError)
	thread = frappe.get_doc("Chat Thread", thread_name)
	for row in thread.participants:
		if row.user == frappe.session.user:
			row.last_read_at = now_datetime()
	thread.save(ignore_permissions=True)
	return {"status": "read"}


@frappe.whitelist()
def add_participant(thread_name=None, user=None, **kwargs):
	"""addendum: "adding participants to an agency thread stays restricted" — enforced here as
	an outright block for Agency threads (their shape is fixed at exactly two participants,
	Chat Thread.validate() also enforces this). Internal threads can grow freely between staff.
	"""
	thread_name = thread_name or kwargs.get("thread_id") or kwargs.get("thread")
	user = user or kwargs.get("user_email") or kwargs.get("participant") or kwargs.get("new_user") or kwargs.get("member")
	if not thread_name or not user:
		frappe.throw("Both 'thread_name' and 'user' are required.", frappe.ValidationError)

	thread = frappe.get_doc("Chat Thread", thread_name)
	if thread.thread_type == "Agency":
		frappe.throw(
			"Cannot add participants to an Agency thread — start a separate internal thread instead.",
			frappe.ValidationError,
		)
	if not is_participant(frappe.session.user, thread_name):
		frappe.throw("Not permitted.", frappe.PermissionError)
	validate_thread_participants(frappe.session.user, user)

	thread.append("participants", {"user": user})
	thread.save(ignore_permissions=True)
	return thread.as_dict()
