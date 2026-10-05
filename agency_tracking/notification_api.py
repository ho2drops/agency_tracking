# Copyright (c) 2026, Agency and contributors
# License: MIT. See LICENSE
#
# Part F: module-scoped whitelisted functions, no raw /api/resource/* exposure.

import frappe

from agency_tracking.notification_engine import (
	ensure_vapid_keys,
	generate_vapid_keys as _generate_vapid_keys,
	_render_notification,
	register_push_subscription as _register_push_subscription,
	remove_push_subscription as _remove_push_subscription,
	notify as _notify,
)
from agency_tracking.notification_feed import get_all_alerts
from agency_tracking.watchdogs import send_wakala_reminder


@frappe.whitelist()
def subscribe_to_push(endpoint=None, p256dh=None, auth=None, **kwargs):
	"""A user subscribing their own browser — never on behalf of anyone else."""
	if not (endpoint and p256dh and auth):
		frappe.throw("endpoint, p256dh, and auth are all required.", frappe.ValidationError)
	_register_push_subscription(frappe.session.user, endpoint, p256dh, auth)
	return {"status": "subscribed"}


@frappe.whitelist()
def unsubscribe_from_push(endpoint=None, **kwargs):
	"""The caller's own browser stops receiving their notifications -- called on sign-out and when
	push is turned off. Only ever removes the calling user's row for that endpoint."""
	if not endpoint:
		frappe.throw("endpoint is required.", frappe.ValidationError)
	_remove_push_subscription(frappe.session.user, endpoint)
	return {"status": "unsubscribed"}


@frappe.whitelist()
def get_vapid_public_key():
	"""Frontend fetches the VAPID application server key to pass to PushManager.subscribe().
	Auto-provisions the keypair on first call so push works out of the box (see
	notification_engine.ensure_vapid_keys). Safe for any authenticated user -- the public key
	is, by design, public."""
	config = ensure_vapid_keys()
	return {"vapid_public_key": config.vapid_public_key}


@frappe.whitelist()
def regenerate_vapid_keys():
	"""Admin-only: force a brand-new VAPID keypair (e.g. after a suspected key leak). NOTE: this
	invalidates every existing Push Subscription -- browsers subscribed under the old key will
	stop receiving pushes until they re-subscribe with the new applicationServerKey. Returns the
	new public key."""
	frappe.only_for(("System Manager", "Administrator"), message=True)  # message: QA P7-03
	public_key, private_pem = _generate_vapid_keys()
	config = frappe.get_single("Notification Config")
	config.vapid_public_key = public_key
	config.vapid_private_key = private_pem
	if not config.vapid_claims_email:
		site = frappe.local.site if getattr(frappe.local, "site", None) else "localhost"
		config.vapid_claims_email = f"admin@{site}"
	config.save(ignore_permissions=True)
	return {"vapid_public_key": public_key, "message": "VAPID keys regenerated. Existing subscriptions must re-subscribe."}


@frappe.whitelist()
def trigger_wakala_reminder(clearance_step_name=None, **kwargs):
	"""business-workflow-srs.md: "plus staff can trigger a reminder manually any time" — the
	escape hatch alongside the automatic Fri/Sat/Sun watchdog."""
	clearance_step_name = clearance_step_name or kwargs.get("name") or kwargs.get("clearance_step")
	if not clearance_step_name:
		frappe.throw("clearance_step_name is required.", frappe.ValidationError)
	if not frappe.db.exists("Clearance Step", clearance_step_name):
		frappe.throw(f"Clearance Step {clearance_step_name} not found.", frappe.DoesNotExistError)
	step = frappe.get_doc("Clearance Step", clearance_step_name)
	if step.step_type not in ("Embassy", "Kuwait Embassy"):
		frappe.throw("This is only meaningful for an Embassy clearance step.", frappe.ValidationError)
	if not step.has_permission("read"):
		frappe.throw("Not permitted.", frappe.PermissionError)
	send_wakala_reminder(clearance_step_name, step.placement)
	return {"status": "reminder sent"}


@frappe.whitelist()
def send_test_push():
	"""Sends a real push notification to the current user through the actual delivery pipeline
	(Comms Log -> pywebpush), not a client-side-only Notification() call -- this is what lets
	the "Test Alert" button genuinely prove whether server-to-device push works, instead of
	passing even when the real pipeline is broken. Returns the resulting delivery status/error
	so the frontend can show real feedback."""
	log = _notify(frappe.session.user, "test_notification", {}, immediate=True)
	return {"status": log.status, "error": log.error}


@frappe.whitelist(allow_guest=False)
def get_push_subscription_status():
	"""Read-only -- tells the frontend whether the current user already has an active Push
	Subscription, so it knows whether to show the manual \"enable notifications\" fallback
	button (needed when the browser's own permission prompt is auto-dismissed or the user
	cancels it without realizing)."""
	return {
		"subscribed": bool(
			frappe.db.exists("Push Subscription", {"user": frappe.session.user})
		)
	}


def _upsert_read_state(user, alert_key, state):
	"""Check-then-insert, same pattern as notification_engine.register_push_subscription --
	dedupe_key's DB-level unique constraint (notification_read_state.json) is what makes this
	safe against a race between the check and the insert, not the check itself."""
	from frappe.utils import now_datetime

	dedupe_key = f"{user}|{alert_key}"
	existing = frappe.db.get_value("Notification Read State", {"dedupe_key": dedupe_key}, "name")
	if existing:
		frappe.db.set_value(
			"Notification Read State", existing, {"state": state, "updated_at": now_datetime()}
		)
		return
	try:
		frappe.get_doc(
			{
				"doctype": "Notification Read State",
				"user": user,
				"alert_key": alert_key,
				"dedupe_key": dedupe_key,
				"state": state,
				"updated_at": now_datetime(),
			}
		).insert(ignore_permissions=True)
	except frappe.DuplicateEntryError:
		# Lost a race against a concurrent insert for the same (user, alert_key) -- the row
		# that won is functionally equivalent (upsert intent, not append), nothing to fix up.
		frappe.db.rollback()
		frappe.db.set_value(
			"Notification Read State",
			frappe.db.get_value("Notification Read State", {"dedupe_key": dedupe_key}, "name"),
			{"state": state, "updated_at": now_datetime()},
		)


@frappe.whitelist()
def get_live_alerts():
	"""Server-derived, permission-scoped in-app notification feed (see notification_feed.py),
	stamped with the calling user's own read/dismissed state. A brand-new device sees exactly
	the same read/dismissed state as any other device, because it's looked up by user in
	Notification Read State, not anything device- or session-local (sessionStorage/React
	state) -- this is the actual fix for "a new device shows every notification at once"."""
	alerts = get_all_alerts()
	keys = [a["key"] for a in alerts]
	states = {}
	if keys:
		# frappe.get_all, not get_list: Notification Read State's own DocType permissions only
		# grant System Manager/Admin, so a get_list here would silently return nothing for
		# every other role. Safe to bypass -- the filter below is hardcoded to the session
		# user, never attacker-controlled.
		rows = frappe.get_all(
			"Notification Read State",
			filters={"user": frappe.session.user, "alert_key": ["in", keys]},
			fields=["alert_key", "state"],
		)
		states = {row.alert_key: row.state for row in rows}

	unread_count = 0
	for alert in alerts:
		state = states.get(alert["key"])
		alert["read"] = state is not None
		alert["dismissed"] = state == "Dismissed"
		if state is None:
			unread_count += 1
	return {"alerts": alerts, "unread_count": unread_count}


SENT_HISTORY_DAYS = 30
SENT_HISTORY_PAGE_SIZE = 50


@frappe.whitelist()
def get_sent_notifications(page=1, page_size=None, **kwargs):
	""""Sent to you": every push the calling user was sent in the last SENT_HISTORY_DAYS days,
	newest first, whether or not it reached a browser (`delivered`). The text is rendered the same
	way the push itself was. Always the session user's own -- there is no user parameter."""
	from frappe.utils import add_days, cint, now_datetime

	page = max(cint(page), 1)
	size = cint(page_size)
	size = SENT_HISTORY_PAGE_SIZE if size <= 0 else min(size, SENT_HISTORY_PAGE_SIZE)
	filters = {
		"recipient": frappe.session.user,
		"channel": "Push",
		"creation": [">=", add_days(now_datetime(), -SENT_HISTORY_DAYS)],
	}
	# frappe.get_all: Comms Log is not readable by most roles; the recipient filter is the session
	# user, never caller-supplied.
	rows = frappe.get_all(
		"Comms Log",
		filters=filters,
		fields=["name", "template", "context", "status", "creation"],
		order_by="creation desc",
		limit_start=(page - 1) * size,
		limit_page_length=size,
	)
	notifications = []
	for row in rows:
		title, body = _render_notification(row.template, frappe.parse_json(row.context) if row.context else {})
		notifications.append(
			{"name": row.name, "title": title, "body": body, "sent_at": row.creation, "delivered": row.status == "Sent"}
		)
	return {
		"notifications": notifications,
		"total_count": frappe.db.count("Comms Log", filters),
		"page": page,
		"page_size": size,
	}


@frappe.whitelist()
def mark_alerts_read(alert_keys=None, **kwargs):
	"""Bulk -- upserts every key to Read for the calling user in one round trip, so "mark all
	read" (or the frontend watchdog dispatcher persisting "already surfaced this one") isn't N
	separate requests. Always writes for frappe.session.user only -- never accepts a user
	parameter, so one user can never mark another user's alerts."""
	alert_keys = alert_keys or kwargs.get("keys")
	if isinstance(alert_keys, str):
		alert_keys = frappe.parse_json(alert_keys)
	if not alert_keys:
		return {"status": "ok", "updated": 0}
	user = frappe.session.user
	for key in alert_keys:
		_upsert_read_state(user, key, "Read")
	return {"status": "ok", "updated": len(alert_keys)}


@frappe.whitelist()
def set_alert_state(alert_key=None, state=None, **kwargs):
	"""Dismiss (state="Dismissed") or restore (state="Read") a single alert for the calling
	user. Always writes for frappe.session.user only -- same reasoning as mark_alerts_read."""
	alert_key = alert_key or kwargs.get("key")
	if not alert_key:
		frappe.throw("alert_key is required.", frappe.ValidationError)
	if state not in ("Read", "Dismissed"):
		frappe.throw("state must be 'Read' or 'Dismissed'.", frappe.ValidationError)
	_upsert_read_state(frappe.session.user, alert_key, state)
	return {"status": "ok"}
