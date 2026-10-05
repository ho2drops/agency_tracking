# Copyright (c) 2026, Agency and contributors
# License: MIT. See LICENSE
#
# Part E: "One delivery pipeline serves three features (assignment alerts, chat, watchdog
# alerts)." notify() is transcribed directly from Part E's pseudocode. Delivery itself
# (_deliver_push / _deliver_whatsapp) calls real external services (pywebpush, WhatsApp Cloud
# API) but — same honesty standard as fetch_daily_fx_rates in Step 8 — neither has been
# exercised against live credentials/a real subscribed browser in this build. What's fully
# verified is the part that's actually load-bearing for correctness: the queue never loses a
# notification, retries pick up exactly the Pending/Failed rows, and delivery failures never
# raise into the caller.
#
# notify() is deliberately NOT whitelisted — it's an internal building block called by
# clearance_engine (assignment alerts), watchdogs.py, and (Step 12) chat, never directly by a
# client. Whitelisting it would let any authenticated user spam-notify any other user by name;
# the only client-facing surface for this pipeline is notification_api.py's
# register_push_subscription (a user subscribing their own browser) and the manual watchdog
# triggers.

import frappe
from frappe.utils import add_to_date, now_datetime

from agency_tracking.labels import candidate_label, step_label, user_label, without_record_ids

# How long a push service should hold an undelivered notification for an offline recipient
# before giving up, per RFC 8030. pywebpush defaults to 0 ("now or never") if not overridden.
PUSH_TTL_SECONDS = 24 * 60 * 60
NOTIFICATIONS_PAGE = "/notifications"


def notify(user, template, context, channel="Push", immediate=False):
	"""Records the notification now and hands the sending to the background worker, so whoever
	triggered it (an assignment to a whole role, a ban event to every Manager) does not wait ~1.5 s
	per push. `immediate=True` sends before returning -- only for a caller that reports the result
	(send_test_push). A row the worker never reaches stays Pending and is picked up by
	retry_pending_notifications."""
	log = frappe.get_doc(
		{
			"doctype": "Comms Log",
			"recipient": user,
			"channel": channel,
			"template": template,
			"context": frappe.as_json(context) if not isinstance(context, str) else context,
			"status": "Pending",
		}
	).insert(ignore_permissions=True)
	if immediate:
		attempt_push_delivery(log)
	else:
		frappe.enqueue(
			"agency_tracking.notification_engine.deliver",
			queue="short",
			enqueue_after_commit=True,
			comms_log=log.name,
		)
	return log


def deliver(comms_log):
	"""Background job for one notification. The row is gone if the action that raised it was
	rolled back, and already Sent if a retry got there first."""
	if frappe.db.get_value("Comms Log", comms_log, "status") in (None, "Sent"):
		return
	attempt_push_delivery(frappe.get_doc("Comms Log", comms_log))


def attempt_push_delivery(log):
	"""Best-effort, never raises — delivery failures are retried on next login and on new
	Push Subscription registration (Part E), not by crashing whatever triggered the notify()
	call (an assignment, a watchdog sweep, a chat message)."""
	try:
		if log.channel == "Push":
			_deliver_push(log)
		elif log.channel == "WhatsApp":
			_deliver_whatsapp(log)
		log.status = "Sent"
		log.error = None
	except Exception as e:
		log.status = "Failed"
		log.attempts = (log.attempts or 0) + 1
		log.error = str(e)[:500]
	log.last_attempt_at = now_datetime()
	log.save(ignore_permissions=True)


def generate_vapid_keys():
	"""Generate a fresh VAPID (Web Push) P-256 keypair. Pure -- no side effects, no DB writes.

	Returns (application_server_key, private_pem):
	  application_server_key -- base64url (no padding) of the 65-byte uncompressed public point,
	                            i.e. exactly the string a browser passes as `applicationServerKey`
	                            to PushManager.subscribe().
	  private_pem            -- PKCS8 PEM string, fed to py_vapid for signing (see _deliver_push).
	"""
	import base64
	from cryptography.hazmat.primitives.asymmetric import ec
	from cryptography.hazmat.primitives import serialization

	pk = ec.generate_private_key(ec.SECP256R1())
	private_pem = pk.private_bytes(
		serialization.Encoding.PEM,
		serialization.PrivateFormat.PKCS8,
		serialization.NoEncryption(),
	).decode()
	pub_point = pk.public_key().public_bytes(
		serialization.Encoding.X962,
		serialization.PublicFormat.UncompressedPoint,
	)
	application_server_key = base64.urlsafe_b64encode(pub_point).rstrip(b"=").decode()
	return application_server_key, private_pem


def ensure_vapid_keys(config=None):
	"""Return a Notification Config that definitely has VAPID keys, generating + persisting a
	keypair on first use if the admin hasn't set them. This is what makes Web Push "work out of
	the box": the very first notify() that needs to sign a push provisions the keys instead of
	failing. A default vapid_claims_email is filled in too (VAPID requires a `sub` claim)."""
	config = config or frappe.get_single("Notification Config")
	if config.vapid_public_key and config.get_password("vapid_private_key", raise_exception=False):
		return config

	public_key, private_pem = generate_vapid_keys()
	config.vapid_public_key = public_key
	config.vapid_private_key = private_pem
	if not config.vapid_claims_email:
		# `sub` must be a mailto: or https: URL identifying the sender; fall back to a site-derived
		# address so signing never fails purely for a missing contact email.
		site = frappe.local.site if getattr(frappe.local, "site", None) else "localhost"
		config.vapid_claims_email = f"admin@{site}"
	config.save(ignore_permissions=True)
	frappe.logger().info("Auto-generated VAPID keypair for Web Push (Notification Config).")
	return config


def _get_vapid_config():
	# Auto-provision on first use rather than raising -- see ensure_vapid_keys.
	return ensure_vapid_keys()


def _render_notification(template, context):
	"""Turn a (template, context) pair into human-readable (title, body) text. The Web Push
	payload has to carry real text -- a browser's service worker has no idea what a
	"clearance_step_assigned" template key means -- so every template notify() is ever called
	with (watchdogs.py, clearance_engine.py, chat_engine.py, background_jobs.py,
	applicant_api.py, placement_api.py) needs a case here, including the four passed as a
	variable through watchdogs.py's _daily_notifier(template) rather than as a literal at the
	notify() call site (medical_expiry_warning, contract_age_alert,
	taeshir_injaz_payment_reminder, departure_due_reminder). An unrecognized template still
	renders something reasonable rather than failing delivery.

	People are named, records are not: a candidate is "Full Name (passport)" (labels.py), looked up
	here from the IDs the context carries, so what is stored does not change."""
	context = context or {}
	who = candidate_label(context.get("applicant"), context.get("placement"), context.get("clearance_step"))
	if template == "medical_expiry_warning":
		return "Medical Expiry Warning", f"{who}: medical clearance expires in {context.get('days_remaining')} day(s)."
	if template == "contract_age_alert":
		return (
			"Contract Age Alert",
			f"{who}: contract signed {context.get('age_days')} days ago, not yet departed "
			f"(limit {context.get('threshold_days')} days).",
		)
	if template == "taeshir_injaz_payment_reminder":
		return (
			"Taeshir/Injaz Payment Reminder",
			f"{who}: Injaz payment due, appointment in {context.get('days_remaining')} day(s).",
		)
	if template == "departure_due_reminder":
		return (
			"Departure Confirmation Overdue",
			f"{who}: flight date {context.get('flight_date')} has passed "
			f"({context.get('days_overdue')} day(s) overdue). Confirm departure.",
		)
	if template == "clearance_step_assigned":
		return "New Clearance Step Assigned", f"You've been assigned the {step_label(context.get('clearance_step'))}."
	if template == "placement_todo_assigned":
		description = without_record_ids(context.get("description"), context.get("placement"))
		return "New Task Assigned", description or f"New task for {who}."
	if template == "wakala_payment_reminder":
		return "Wakala Payment Reminder", context.get("message") or f"Wakala payment due for {who}."
	if template == "chat_message":
		return "New Message", f"New message from {user_label(context.get('sender'))}."
	if template == "background_job_completed":
		job_type = context.get("job_type") or "Background job"
		status = context.get("status") or "Finished"
		about = ""
		if context.get("reference_doctype") == "Applicant" and context.get("reference_name"):
			about = f" for {candidate_label(applicant=context.get('reference_name'))}"
		return f"{job_type} {status}", f"Your {job_type} job{about} is {status.lower()}."
	if template == "country_ban_request":
		action = str(context.get("action") or "").replace("_", " ")
		what = f"a one-time override ({action})" if context.get("request_type") == "Override" else "lifting the ban"
		return (
			"Country Ban Request",
			f"{user_label(context.get('requested_by'))} requests {what} for {who} on "
			f"{context.get('country')}: {context.get('reason')}",
		)
	if template == "country_ban_request_decided":
		outcome = context.get("status")
		extra = " Retry the action now; it will go through once." if outcome == "Approved" and context.get("request_type") == "Override" else ""
		return (
			f"Country Ban Request {outcome}",
			f"Your {str(context.get('request_type')).lower()} request for {who} on {context.get('country')} "
			f"was {str(outcome).lower()}.{extra}" + (f" Note: {context.get('note')}" if context.get("note") else ""),
		)
	if template and template.startswith("country_ban_"):
		event = template[len("country_ban_") :].replace("_", " ").title()
		return (
			f"Country Ban {event}",
			f"{who}: ban on {context.get('country')}." + (f" Reason: {context.get('reason')}" if context.get("reason") else ""),
		)
	if template == "kuwait_visa_agency_mismatch":
		return (
			"Visa Agency Mismatch",
			f"{who}: visa agency '{context.get('visa_agency_name')}' doesn't match the agency "
			f"'{context.get('contractor_name')}'.",
		)
	if template == "test_notification":
		return "Test Notification", "This is a test push notification -- if you can see this, push delivery is working."
	return "Travel Agency Workflow Alert", f"You have a new update ({template})."


def _deliver_push(log):
	subscriptions = frappe.get_all(
		"Push Subscription", filters={"user": log.recipient}, fields=["endpoint", "p256dh", "auth"]
	)
	if not subscriptions:
		raise Exception("No Push Subscription registered for this user yet.")

	from pywebpush import webpush
	from py_vapid import Vapid01

	config = _get_vapid_config()
	# Build the signer once from the stored PEM and hand webpush the Vapid instance directly
	# (version-robust across pywebpush releases whose string-key handling differs). py_vapid
	# fills the `aud` claim per endpoint and an `exp` claim automatically; we supply `sub`.
	vapid = Vapid01.from_pem(config.get_password("vapid_private_key").encode())
	vapid_claims = {"sub": f"mailto:{config.vapid_claims_email}"}
	context = frappe.parse_json(log.context) if log.context else {}
	title, body = _render_notification(log.template, context)
	# `url` is what the service worker opens on click: the Notifications page, where every push
	# the user was sent is listed.
	payload = frappe.as_json({"title": title, "body": body, "url": NOTIFICATIONS_PAGE})

	errors = []
	for sub in subscriptions:
		try:
			webpush(
				subscription_info={
					"endpoint": sub.endpoint,
					"keys": {"p256dh": sub.p256dh, "auth": sub.auth},
				},
				data=payload,
				vapid_private_key=vapid,
				vapid_claims=dict(vapid_claims),
				# pywebpush defaults ttl=0 ("deliver now or drop it" per the Web Push spec) --
				# an offline recipient would never get this even after reconnecting, since the
				# push service doesn't queue it. A day is long enough to survive a normal
				# offline stretch without holding genuinely stale notifications forever.
				ttl=PUSH_TTL_SECONDS,
			)
		except Exception as e:
			errors.append(str(e))
			# 404 / 410 is the push service's final answer that this browser's subscription no
			# longer exists (push turned off, permission withdrawn, browser data cleared). Any other
			# failure may be temporary and keeps the row.
			if getattr(getattr(e, "response", None), "status_code", None) in (404, 410):
				frappe.db.delete("Push Subscription", {"user": log.recipient, "endpoint": sub.endpoint})
	if errors and len(errors) == len(subscriptions):
		raise Exception("; ".join(errors))


def whatsapp_configured(config=None):
	"""True once Notification Config holds both WhatsApp Cloud API values. Callers that pair a push
	with a WhatsApp message ask this first, so nothing is attempted or logged before it is set up."""
	config = config or frappe.get_single("Notification Config")
	return bool(config.get_password("whatsapp_access_token", raise_exception=False) and config.whatsapp_phone_number_id)


def _deliver_whatsapp(log):
	import requests

	config = frappe.get_single("Notification Config")
	if not whatsapp_configured(config):
		raise Exception("WhatsApp Cloud API not configured (Notification Config).")
	token = config.get_password("whatsapp_access_token")

	context = frappe.parse_json(log.context) if log.context else {}
	phone = context.get("phone")
	if not phone:
		raise Exception("No phone number in notification context.")

	response = requests.post(
		f"https://graph.facebook.com/v19.0/{config.whatsapp_phone_number_id}/messages",
		headers={"Authorization": f"Bearer {token}"},
		json={
			"messaging_product": "whatsapp",
			"to": phone,
			"type": "text",
			"text": {"body": context.get("message", log.template)},
		},
		timeout=10,
	)
	response.raise_for_status()


def register_push_subscription(user, endpoint, p256dh, auth):
	"""Records a browser's push subscription. Whitelisted wrapper lives in
	notification_api.py — this is the underlying logic, callable from tests without going
	through a whitelisted-function permission context."""
	# An endpoint is one physical browser, and it belongs to whoever enabled push on it last:
	# without this, the previous user's notifications keep appearing on a shared computer.
	frappe.db.delete("Push Subscription", {"endpoint": endpoint, "user": ["!=", user]})
	existing = frappe.db.get_value("Push Subscription", {"user": user, "endpoint": endpoint}, "name")
	if not existing:
		frappe.get_doc(
			{
				"doctype": "Push Subscription",
				"user": user,
				"endpoint": endpoint,
				"p256dh": p256dh,
				"auth": auth,
			}
		).insert(ignore_permissions=True)
	retry_pending_notifications_later(user)


def remove_push_subscription(user, endpoint):
	"""Forget one browser of one user (sign-out, or push turned off in the UI)."""
	frappe.db.delete("Push Subscription", {"user": user, "endpoint": endpoint})


def retry_pending_notifications(user):
	"""Part E: "retried on next login and on new Push Subscription registration" — covers
	'notify even if offline, deliver once back online'. Only what is younger than PUSH_TTL_SECONDS:
	the same window the push service itself holds a message for. Older rows stay as history and are
	never pushed, so enabling push does not replay months of backlog at once."""
	pending = frappe.get_all(
		"Comms Log",
		filters={
			"recipient": user,
			"channel": "Push",
			"status": ["in", ["Pending", "Failed"]],
			"creation": [">=", add_to_date(now_datetime(), seconds=-PUSH_TTL_SECONDS)],
		},
		order_by="creation asc",
	)
	for row in pending:
		attempt_push_delivery(frappe.get_doc("Comms Log", row.name))


def retry_pending_notifications_later(user):
	"""The retry as a background job: sign-in and subscribe return without waiting for it."""
	frappe.enqueue(
		"agency_tracking.notification_engine.retry_pending_notifications",
		queue="short",
		enqueue_after_commit=True,
		user=user,
	)


def retry_pending_notifications_on_login(login_manager):
	"""hooks.py on_login — Frappe passes the LoginManager, whose .user is the logged-in user."""
	retry_pending_notifications_later(login_manager.user)
