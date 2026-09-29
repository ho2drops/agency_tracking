# Copyright (c) 2026, Agency and contributors
# License: MIT. See LICENSE
#
# Part F: no raw /api/resource/* exposure. Every client-facing operation is a whitelisted
# function in a module-scoped file. This is the first of those files (applicant_api.py named
# explicitly in Part F's surface list); more (placement_api.py, finance_api.py, chat_api.py,
# report_api.py) are added as their build steps land.

import frappe

from agency_tracking.pagination import count_rows, page_args, paged_result
from agency_tracking.state_machine import LIFECYCLE_FIELDS, transition

CYCLE_REGRESSION_STATUSES = ("Registered", "CV Generated")

# Set only by the system, never by an edit call (2026-09-23 fix: update_applicant used to save any
# field it was sent -- confirmed live, it could clear active_placement, detaching a placed applicant
# so another agency could select her, or rewrite cycle_number / fee_transaction). entry_track stays
# editable here on purpose (update_applicant handles its cycle regression below).
APPLICANT_SYSTEM_FIELDS = (LIFECYCLE_FIELDS - {"entry_track"}) | {
	"cycle_number",
	"fee_transaction",
	"fee_log",
	"age",
	"full_name",
	"passport_issue_date",
	"owner",
	"creation",
	"modified",
	"modified_by",
}


@frappe.whitelist()
def create_applicant(**data):
	"""Open a new Applicant file at Draft. Registrar, Manager, Admin only
	(doctype-level create permission, Part G)."""
	if not frappe.has_permission("Applicant", "create"):
		frappe.throw("Not permitted.", frappe.PermissionError)
	# System fields (APPLICANT_SYSTEM_FIELDS) are never taken from the caller, and a new file always
	# starts at Draft -- a caller-supplied status would skip register_applicant's checks.
	data = {k: v for k, v in data.items() if k not in APPLICANT_SYSTEM_FIELDS or k == "full_name"}
	data["doctype"] = "Applicant"
	data["status"] = "Draft"
	data.setdefault("entry_track", "Standard")
	if data.get("nationality") == "Ethiopian":
		data["nationality"] = "Ethiopia"
	data.setdefault("nationality", "Ethiopia")
	data.setdefault("gender", "Female")
	if not data.get("full_name") and data.get("first_name"):
		data["full_name"] = " ".join(
			filter(None, [data.get("first_name"), data.get("middle_name"), data.get("last_name")])
		).strip()
	doc = frappe.get_doc(data).insert()
	return doc.as_dict()


def _check_country_ban_or_throw(applicant_name, country, override, override_reason, action=None):
	"""'Ashara Teyezuwal' (2026-08-29): a permanent per-(Applicant, Country) blacklist. Only
	active=1 rows block anything -- a lifted ban (remove_country_ban) is kept for history but no
	longer enforced. Manager/Admin/System Manager can force past an active ban with a written reason -- same
	override shape as a blocked STAGE_GATES transition, even though this check sits outside the
	state machine itself (it's a field-level guard, not a status move).

	Called from every point that can put a banned (applicant, country) pair in front of a
	foreign agency or commit them to that country: update_applicant (on a destination_country
	change), register_applicant, cv_api.generate_cv, restart_applicant, and as a backstop in
	portal_api.select_candidate. portal_api.list_portal_candidates filters banned
	applicants out of the marketplace listing separately (a list filter, not a throw).

	`action` (one of COUNTRY_BAN_ACTIONS) lets a blocked staff member retry after a Manager
	approved a Country Ban Request Override for exactly that action: the pass is consumed here
	(status Approved -> Used) and the check passes once. A blocked attempt no longer notifies
	anyone (2026-09-23) -- the blocked user raises a request via request_country_ban_exception
	instead, and Managers are notified once per request."""
	if not country:
		return
	ban = frappe.db.get_value(
		"Applicant Country Ban",
		{"applicant": applicant_name, "country": country, "active": 1},
		["name", "reason"],
	)
	if not ban:
		return
	ban_name, ban_reason = ban

	if not override:
		if action and _consume_override_pass(ban_name, action):
			return
		frappe.throw(
			f"{applicant_name} is permanently banned from {country} (see {ban_name}). "
			"A Manager or Admin must override this with a written reason, or you can request an "
			"override or lift (request_country_ban_exception).",
			frappe.PermissionError,
		)

	# Same role set that can lift a ban or decide a Country Ban Request (System Manager added
	# 2026-09-23 -- it could already lift a ban outright, so refusing it a one-off override was
	# an inconsistency, not a restriction).
	if not _is_ban_decider():
		frappe.throw("Only a Manager, Admin or System Manager can override a country ban.", frappe.PermissionError)
	if not override_reason:
		frappe.throw("A written reason is required to override a country ban.", frappe.ValidationError)

	_notify_management_of_ban_event(applicant_name, country, ban_name, "overridden", override_reason)


def _consume_override_pass(ban_name, action):
	"""Mark one approved Override request for exactly this ban + action as Used. Keyed on the ban
	(not applicant + country) so a leftover pass from a since-lifted ban can't unlock a newer one.
	Runs inside the caller's transaction, so if the unlocked action itself then fails and the
	request rolls back, the pass is un-consumed with it and can be retried."""
	req = frappe.db.get_value(
		"Country Ban Request",
		{"ban": ban_name, "request_type": "Override", "action": action, "status": "Approved"},
		"name",
		order_by="decided_on asc",
		for_update=True,
	)
	if not req:
		return False
	frappe.db.set_value("Country Ban Request", req, {"status": "Used", "used_on": frappe.utils.now_datetime()})
	return True


def _notify_management_of_ban_event(applicant_name, country, ban_name, event, reason):
	from agency_tracking.notification_engine import notify

	# Routed to Manager only -- Admin has full access but is deliberately not push-notified for
	# routine events (2026-09-05: "admin shouldn't be bothered by every notification").
	for user in frappe.get_all("Has Role", filters={"role": "Manager"}, pluck="parent"):
		notify(
			user,
			"country_ban_" + event,
			{"applicant": applicant_name, "country": country, "ban": ban_name, "reason": reason},
		)


@frappe.whitelist()
def update_applicant(applicant_name=None, override_ban=False, override_reason=None, **data):
	"""Edit an Applicant still at Draft or Registered. Does not change status — use
	register_applicant for that transition.
	"""
	if not applicant_name:
		frappe.throw("applicant_name is required.", frappe.ValidationError)
	doc = frappe.get_doc("Applicant", applicant_name)
	if not doc.has_permission("write"):
		frappe.throw("Not permitted.", frappe.PermissionError)
	data = {k: v for k, v in data.items() if k not in APPLICANT_SYSTEM_FIELDS and k not in ("doctype", "cmd")}

	new_country = data.get("destination_country")
	if new_country and new_country != doc.destination_country:
		# Only on an actual change: re-saving an unchanged (banned) country exposes the applicant
		# to nothing new -- register_applicant, generate_cv, restart_applicant, select_candidate and
		# the portal listing each enforce the ban themselves -- and checking it here would block
		# every routine edit of a banned applicant (and notify every Manager each time).
		_check_country_ban_or_throw(applicant_name, new_country, override_ban, override_reason, action="Change Destination")

	doc.update(data)
	if "entry_track" in data and data["entry_track"] != doc.entry_track and doc.status in CYCLE_REGRESSION_STATUSES:
		transition(doc, "Draft")
	else:
		doc.save()
	return doc.as_dict()


@frappe.whitelist()
def log_applicant_fee(applicant_name=None):
	"""Manual 'Log Fee' button path. Just flips fee_status to Paid and saves -- the actual
	ledger-entry creation lives in Applicant.maybe_log_fee_transaction (before_save), so a
	direct Desk edit that sets fee_status=Paid gets identical behavior without going through
	this endpoint at all. Kept as its own whitelisted call (rather than folding into
	update_applicant) so the button can carry its own explicit permission + friendly
	already-logged error, matching the other single-purpose action endpoints in this module."""
	from agency_tracking.roles import INTERNAL_STAFF_ROLES

	if not applicant_name:
		frappe.throw("applicant_name is required.", frappe.ValidationError)
	if not (INTERNAL_STAFF_ROLES & set(frappe.get_roles())):
		frappe.throw("Not permitted.", frappe.PermissionError)

	doc = frappe.get_doc("Applicant", applicant_name)
	if not doc.fee_required or not doc.registration_fee_amount:
		frappe.throw("Set Fee Required and an amount before logging a fee.", frappe.ValidationError)
	if doc.fee_transaction:
		frappe.throw(f"This fee was already logged as {doc.fee_transaction}.", frappe.ValidationError)

	doc.fee_status = "Paid"
	doc.save(ignore_permissions=True)
	return doc.as_dict()


LMIS_EDITABLE_FIELDS = (
	"exam_date",
	"coc_status",
	"labor_id",
	"national_id",
	"emergency_contact_name",
	"emergency_contact_phone",
	"emergency_contact_address",
)


@frappe.whitelist()
def update_applicant_for_lmis(applicant_name=None, **data):
	"""Narrow LMIS-stage edit surface (2026-08-29 correction, Part 5): national_id, labor_id,
	and emergency_contact_* are deliberately NOT part of the Registered field floor -- they're
	captured here, once the candidate is actually at the LMIS clearance step, not guessed at
	registration time. Restricted to the two LMIS roles (plus Manager/Admin, same fallback
	pattern as everywhere else) rather than general update_applicant."""
	if not applicant_name:
		frappe.throw("applicant_name is required.", frappe.ValidationError)
	if not ({"Saudi LMIS", "Kuwait LMIS", "Manager", "Admin"} & set(frappe.get_roles())):
		frappe.throw("Not permitted.", frappe.PermissionError)

	doc = frappe.get_doc("Applicant", applicant_name)
	updates = {k: v for k, v in data.items() if k in LMIS_EDITABLE_FIELDS}
	doc.update(updates)
	doc.save(ignore_permissions=True)
	return doc.as_dict()


@frappe.whitelist()
def cancel_applicant(applicant_name=None, reason=None, **kwargs):
	"""Global 'Cancelled' escape hatch (2026-08-29 lifecycle spec): only from Registered/CV
	Generated (never Draft -- nothing committed yet to cancel). If there's an active Placement,
	freeze it and its Clearance Steps first (marked Cancelled, left as permanent history) and
	clear active_placement -- all before the Applicant itself moves to Cancelled, so
	Placement.validate()'s own checks (still-matching active_placement/status) pass cleanly.
	Landing on Cancelled never bumps cycle_number by itself; only a later restart does.
	"""
	if not applicant_name:
		frappe.throw("applicant_name is required.", frappe.ValidationError)
	doc = frappe.get_doc("Applicant", applicant_name)
	if not doc.has_permission("write"):
		frappe.throw("Not permitted.", frappe.PermissionError)
	return cancel_applicant_cascade(doc, reason)


def cancel_applicant_cascade(doc, reason):
	"""The cancel cascade itself, with no permission check of its own: the caller has already
	decided this user may cancel -- cancel_applicant by Applicant write, the medical UNFIT
	endpoints by MEDICAL_RECORD_ROLES. Medical Officer has no write on Applicant or Placement, so
	routing UNFIT through cancel_applicant refused it and the result was never saved (QA P4-05)."""
	if doc.status not in CYCLE_REGRESSION_STATUSES:
		frappe.throw(
			f"Only Registered or CV Generated applicants can be cancelled (currently '{doc.status}').",
			frappe.ValidationError,
		)
	if not reason:
		frappe.throw("A written reason is required to cancel an applicant.", frappe.ValidationError)

	if doc.active_placement:
		placement = frappe.get_doc("Placement", doc.active_placement)
		transition(placement, "Cancelled", remarks=reason, ignore_permissions=True)
		frappe.db.set_value(
			"Clearance Step", {"placement": placement.name}, "status", "Cancelled"
		)
		doc.active_placement = None

	transition(doc, "Cancelled", remarks=reason, ignore_permissions=True)
	return doc.as_dict()


@frappe.whitelist()
def restart_applicant(applicant_name=None, target_status=None, override_ban=False, override_reason=None, **kwargs):
	"""Cancelled -> Draft or Registered. cycle_number bumps automatically (lands on
	Draft/Registered coming from Cancelled -- state_machine.bump_cycle_number). Restarting
	straight to Registered fails naturally via the normal ValidationError from
	Applicant.validate() if the field floor isn't actually satisfied by existing data --
	retry with target_status="Draft" instead, no special-casing needed here.

	Checked against the country ban here regardless of destination_country having "changed" --
	this is the one path that clears active_placement and lets an applicant re-enter the
	pipeline, so it's the chokepoint for a ban set while they were cancelled/on a prior cycle
	(2026-09-22, closing the gap where a re-cycle never re-checked the ban at all).
	"""
	if not applicant_name:
		frappe.throw("applicant_name is required.", frappe.ValidationError)
	if target_status not in ("Draft", "Registered"):
		frappe.throw("target_status must be 'Draft' or 'Registered'.", frappe.ValidationError)
	doc = frappe.get_doc("Applicant", applicant_name)
	if not doc.has_permission("write"):
		frappe.throw("Not permitted.", frappe.PermissionError)
	if doc.status != "Cancelled":
		frappe.throw(f"Only a Cancelled applicant can be restarted (currently '{doc.status}').", frappe.ValidationError)
	_check_country_ban_or_throw(applicant_name, doc.destination_country, override_ban, override_reason, action="Restart")
	transition(doc, target_status)
	return doc.as_dict()


@frappe.whitelist()
def register_applicant(applicant_name=None, override_ban=False, override_reason=None, **kwargs):
	"""Move an Applicant from Draft to Registered via the sanctioned transition() path
	(Part A.2 Stage 2). Field-floor and medical-FIT checks run inside Applicant.validate(),
	triggered by transition()'s doc.save().

	Country-ban check added 2026-09-22: this is the earliest point an applicant heads toward the
	foreign-agency portal, so a banned destination_country is refused here rather than only on a
	later edit that happens to change it."""
	applicant_name = applicant_name or kwargs.get("name") or kwargs.get("applicant")
	if not applicant_name:
		frappe.throw("applicant_name is required.", frappe.ValidationError)
	doc = frappe.get_doc("Applicant", applicant_name)
	if not doc.has_permission("write"):
		frappe.throw("Not permitted.", frappe.PermissionError)
	if doc.status == "Registered":
		return doc.as_dict()
	if kwargs:
		# Same system-field filter as update_applicant: the form's fields (medical, COC, fee included)
		# go through, but cycle_number, status, active_placement etc. are never taken from the caller
		# (QA P7-01 -- cycle_number=9 used to be saved as sent).
		data = {
			k: v for k, v in kwargs.items()
			if k not in APPLICANT_SYSTEM_FIELDS and k not in ("cmd", "doctype", "applicant_name", "name", "applicant")
		}
		if data:
			doc.update(data)
	_check_country_ban_or_throw(applicant_name, doc.destination_country, override_ban, override_reason, action="Register")
	transition(doc, "Registered")
	frappe.db.commit()
	return doc.as_dict()


@frappe.whitelist()
def get_applicant(applicant_name=None, **kwargs):
	if not applicant_name:
		frappe.throw("applicant_name is required.", frappe.ValidationError)
	doc = frappe.get_doc("Applicant", applicant_name)
	if not doc.has_permission("read"):
		frappe.throw("Not permitted.", frappe.PermissionError)
	return doc.as_dict()


@frappe.whitelist()
def list_applicants(filters=None, limit_page_length=100, order_by="modified desc", limit_start=0, with_total=0):
	"""backend-issues #02: the whitelisted list surface Applicant never had -- callers used to
	fall back to raw /api/resource/Applicant, which only Registrar/Manager/Admin/System Manager
	could read (Applicant's doctype-level permissions), 403ing every other role that legitimately
	needs to resolve an applicant name reference (Finance Manager, Clearance Officer, Complaint
	Manager, Communication Manager, the six country+step roles -- all granted read-only access on
	the doctype itself, see applicant.json). frappe.get_list enforces those permissions the same
	way it would for any other doctype; no separate role check needed here."""
	if isinstance(filters, str):
		filters = frappe.parse_json(filters)
	start, length = page_args(limit_start, limit_page_length)
	rows = frappe.get_list(
		"Applicant",
		filters=filters,
		fields=["*"],
		limit_start=start,
		limit_page_length=length,
		order_by=order_by,
	)
	return paged_result(rows, with_total, lambda: count_rows("Applicant", filters))


@frappe.whitelist()
def set_country_ban(applicant_name=None, country=None, reason=None, **kwargs):
	"""Whitelisted create surface for Applicant Country Ban (backend-issues #08) -- the doctype
	previously had no whitelisted writer anywhere, so the only way to set a ban was the raw
	/api/resource/Applicant Country Ban endpoint, contradicting the "no raw /api/resource/*
	exposure" architecture rule. Doctype permissions already grant create to Registrar/
	Complaint Manager/Manager/Admin/System Manager, so this just wraps a normal insert() and
	lets Frappe's own permission check do the gating."""
	if not applicant_name:
		frappe.throw("applicant_name is required.", frappe.ValidationError)
	if not country:
		frappe.throw("country is required.", frappe.ValidationError)
	if not reason:
		frappe.throw("A written reason is required to set a country ban.", frappe.ValidationError)
	if frappe.db.exists("Applicant Country Ban", {"applicant": applicant_name, "country": country, "active": 1}):
		frappe.throw(f"{applicant_name} already has an active country ban on file for {country}.", frappe.ValidationError)

	ban = frappe.get_doc(
		{
			"doctype": "Applicant Country Ban",
			"applicant": applicant_name,
			"country": country,
			"active": 1,
			"set_by": frappe.session.user,
			"set_on": frappe.utils.now_datetime(),
			"reason": reason,
		}
	).insert()
	_notify_management_of_ban_event(applicant_name, country, ban.name, "set", reason)
	return ban.as_dict()


@frappe.whitelist()
def list_country_bans(applicant_name=None, active_only=True):
	filters = {"applicant": applicant_name} if applicant_name else {}
	if frappe.utils.cint(active_only):
		filters["active"] = 1
	return frappe.get_list(
		"Applicant Country Ban",
		filters=filters,
		fields=[
			"name", "applicant", "country", "active", "set_by", "set_on", "reason",
			"lifted_by", "lifted_on", "lift_reason",
		],
		order_by="creation desc",
	)


@frappe.whitelist()
def remove_country_ban(ban_name=None, applicant_name=None, country=None, lift_reason=None, **kwargs):
	"""Lifts (deactivates) a ban rather than deleting it -- the row, and who set/lifted it and
	why, stays on record. Write permission on Applicant Country Ban is Manager/Admin/System
	Manager only (per doctype permissions, delete removed 2026-09-22 now that lifting no longer
	needs it) -- Registrar/Complaint Manager can set a ban but not lift one."""
	ban_name = ban_name or kwargs.get("name")
	if not ban_name and applicant_name and country:
		ban_name = frappe.db.get_value(
			"Applicant Country Ban", {"applicant": applicant_name, "country": country, "active": 1}, "name"
		)
	if not ban_name:
		frappe.throw("ban_name or (applicant_name and country) is required.", frappe.ValidationError)
	ban = frappe.get_doc("Applicant Country Ban", ban_name) if frappe.db.exists("Applicant Country Ban", ban_name) else None
	if not ban:
		return {"lifted": ban_name, "status": "not_found"}
	if not ban.active:
		return {"lifted": ban_name, "status": "already_lifted"}
	if not ban.has_permission("write"):
		frappe.throw("Not permitted.", frappe.PermissionError)

	ban.active = 0
	ban.lifted_by = frappe.session.user
	ban.lifted_on = frappe.utils.now_datetime()
	ban.lift_reason = lift_reason
	ban.save()
	_notify_management_of_ban_event(ban.applicant, ban.country, ban.name, "removed", lift_reason or "No reason given.")
	return {"lifted": ban_name, "status": "success"}


# --- Country Ban Requests (2026-09-23) ---
# A staff member blocked by an active ban asks a Manager/Admin for either a one-time Override of
# one specific action, or a Lift of the ban. Managers get one notification per request (not per
# blocked attempt); the requester is notified of the decision.

COUNTRY_BAN_ACTIONS = ("Register", "Generate CV", "Restart", "Change Destination")
COUNTRY_BAN_DECIDER_ROLES = {"Manager", "Admin", "System Manager"}


def _is_ban_decider():
	return frappe.session.user == "Administrator" or bool(COUNTRY_BAN_DECIDER_ROLES & set(frappe.get_roles()))


@frappe.whitelist()
def request_country_ban_exception(applicant_name=None, request_type=None, reason=None, action=None, country=None, **kwargs):
	"""Ask a Manager/Admin to Override (one-time pass for `action`) or Lift an active country ban.
	`country` defaults to the applicant's destination_country; pass it explicitly for a
	"Change Destination" override (the country being changed TO). Open to anyone with write
	access to the Applicant -- i.e. whoever could have been blocked by the ban."""
	if not applicant_name:
		frappe.throw("applicant_name is required.", frappe.ValidationError)
	if request_type not in ("Override", "Lift"):
		frappe.throw("request_type must be 'Override' or 'Lift'.", frappe.ValidationError)
	if request_type == "Override" and action not in COUNTRY_BAN_ACTIONS:
		frappe.throw(f"action is required for an Override and must be one of: {', '.join(COUNTRY_BAN_ACTIONS)}.", frappe.ValidationError)
	if request_type == "Lift":
		action = None
	if not reason:
		frappe.throw("A written reason is required.", frappe.ValidationError)

	applicant = frappe.get_doc("Applicant", applicant_name)
	if not applicant.has_permission("write"):
		frappe.throw("Not permitted.", frappe.PermissionError)
	country = country or applicant.destination_country
	if not country:
		frappe.throw("country is required (the applicant has no destination_country).", frappe.ValidationError)

	ban = frappe.db.get_value("Applicant Country Ban", {"applicant": applicant_name, "country": country, "active": 1}, "name")
	if not ban:
		frappe.throw(f"{applicant_name} has no active country ban for {country}.", frappe.ValidationError)

	open_filters = {"applicant": applicant_name, "country": country, "request_type": request_type, "status": "Pending"}
	if action:
		open_filters["action"] = action
	existing = frappe.db.get_value("Country Ban Request", open_filters, "name")
	if existing:
		frappe.throw(f"A matching request is already pending ({existing}).", frappe.ValidationError)
	if action and frappe.db.exists("Country Ban Request", {**open_filters, "status": "Approved"}):
		frappe.throw("An approved override for this action is already waiting to be used -- just retry the action.", frappe.ValidationError)

	req = frappe.get_doc(
		{
			"doctype": "Country Ban Request",
			"applicant": applicant_name,
			"country": country,
			"ban": ban,
			"request_type": request_type,
			"action": action,
			"reason": reason,
			"status": "Pending",
			"requested_by": frappe.session.user,
			"requested_on": frappe.utils.now_datetime(),
		}
	).insert(ignore_permissions=True)

	from agency_tracking.notification_engine import notify

	for user in frappe.get_all("Has Role", filters={"role": "Manager", "parenttype": "User"}, pluck="parent", distinct=True):
		notify(
			user,
			"country_ban_request",
			{"request": req.name, "request_type": request_type, "action": action, "applicant": applicant_name,
			 "country": country, "requested_by": frappe.session.user, "reason": reason},
		)
	return req.as_dict()


@frappe.whitelist()
def list_country_ban_requests(status=None, applicant_name=None, **kwargs):
	"""Managers/Admins see every request; anyone else only their own."""
	filters = {}
	if status:
		filters["status"] = status
	if applicant_name:
		filters["applicant"] = applicant_name
	if not _is_ban_decider():
		if not frappe.has_permission("Country Ban Request", "read"):
			frappe.throw("Not permitted.", frappe.PermissionError)
		filters["requested_by"] = frappe.session.user
	return frappe.get_all(
		"Country Ban Request",
		filters=filters,
		fields=[
			"name", "applicant", "country", "ban", "request_type", "action", "status", "reason",
			"requested_by", "requested_on", "decided_by", "decided_on", "decision_note", "used_on",
		],
		order_by="creation desc",
	)


@frappe.whitelist()
def decide_country_ban_request(request_name=None, decision=None, note=None, **kwargs):
	"""Manager/Admin/System Manager. Approve an Override -> a one-time pass the requester's retry
	of that action consumes. Approve a Lift -> the ban is lifted now. Reject -> nothing changes."""
	if not _is_ban_decider():
		frappe.throw("Only a Manager or Admin can decide a country ban request.", frappe.PermissionError)
	if not request_name:
		frappe.throw("request_name is required.", frappe.ValidationError)
	if decision not in ("Approve", "Reject"):
		frappe.throw("decision must be 'Approve' or 'Reject'.", frappe.ValidationError)

	req = frappe.get_doc("Country Ban Request", request_name, for_update=True)
	if req.status != "Pending":
		frappe.throw(f"{request_name} was already decided ({req.status}).", frappe.ValidationError)

	if decision == "Approve" and not frappe.db.get_value("Applicant Country Ban", req.ban, "active"):
		# Ban lifted some other way since the request was raised -- nothing left to approve.
		frappe.throw(f"The ban {req.ban} is no longer active; this request is moot. Reject it instead.", frappe.ValidationError)

	req.status = "Approved" if decision == "Approve" else "Rejected"
	req.decided_by = frappe.session.user
	req.decided_on = frappe.utils.now_datetime()
	req.decision_note = note
	req.save(ignore_permissions=True)

	if decision == "Approve" and req.request_type == "Lift":
		lift_reason = f"Approved request {req.name}: {req.reason}" + (f" -- {note}" if note else "")
		remove_country_ban(ban_name=req.ban, lift_reason=lift_reason)

	from agency_tracking.notification_engine import notify

	notify(
		req.requested_by,
		"country_ban_request_decided",
		{"request": req.name, "request_type": req.request_type, "action": req.action, "applicant": req.applicant,
		 "country": req.country, "status": req.status, "note": note},
	)
	return req.as_dict()
