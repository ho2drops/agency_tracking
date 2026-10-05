# Copyright (c) 2026, Agency and contributors
# License: MIT. See LICENSE
#
# Part E: "Watchdogs sharing this pipeline: medical expiry (14/10/7/3/1-day tiers), contract-age
# threshold (admin-configurable), Wakala payment reminders (twice-weekly + manual trigger)."
# Wired into hooks.py's scheduler — daily for medical/contract-age, a cron entry for Wakala's
# "twice a week" cadence.

import frappe
from frappe.utils import add_days, getdate, today

from agency_tracking.clearance_engine import get_lmis_officer
from agency_tracking.notification_engine import notify, whatsapp_configured

MEDICAL_EXPIRY_TIERS_DAYS = [14, 10, 7, 3, 1]


def _sent_today(template):
	"""{(recipient, placement)} already notified today with this template."""
	sent = set()
	for row in frappe.get_all(
		"Comms Log",
		filters={"template": template, "creation": [">=", today()]},
		fields=["recipient", "context"],
	):
		ctx = frappe.parse_json(row.context) if row.context else {}
		sent.add((row.recipient, ctx.get("placement")))
	return sent


def _daily_notifier(template):
	"""Returns a `send(recipient, context)` that de-duplicates within a day (audit N-4): the same
	recipient is never pinged twice for the same (template, placement) on the same date -- guarding
	scheduler re-runs, overlapping tiers, and repeated sweeps. One Comms Log query per watchdog run
	(not per notification), so it stays O(1) in queries."""
	sent = _sent_today(template)

	def send(recipient, context):
		key = (recipient, context.get("placement"))
		if not recipient or key in sent:
			return
		sent.add(key)
		notify(recipient, template, context)

	return send


def _recipient_for_placement(placement_name):
	"""No single "owner" field exists on Placement — the natural recipient is whoever's
	currently doing the LMIS-family step (falls back to nobody, silently, if unassigned; a
	watchdog isn't the place to invent a recipient that doesn't reflect real assignment)."""
	placement = frappe.get_doc("Placement", placement_name)
	return get_lmis_officer(placement)


def _management_recipients():
	"""All Manager + Admin users (for the medical-expiry escalation). Admin is intentionally
	included here even though routine alerts skip Admin -- a medical about to expire mid-process is
	a management-visibility case the business asked to surface (2026-09-05)."""
	return set(frappe.get_all("Has Role", filters={"role": ["in", ["Manager", "Admin"]]}, pluck="parent"))


def medical_expiry_watchdog():
	"""Applicant.medical_expiry_date within one of the 14/10/7/3/1-day tiers. Only meaningful for
	applicants who've been selected (active_placement set) and NOT yet Departed -- an expiring
	medical no longer matters once the worker has flown. Goes to the LMIS officer plus Manager +
	Admin for management visibility (2026-09-05)."""
	send = _daily_notifier("medical_expiry_warning")
	for tier_days in MEDICAL_EXPIRY_TIERS_DAYS:
		target_date = add_days(today(), tier_days)
		applicants = frappe.get_all(
			"Applicant",
			filters={"medical_expiry_date": target_date, "active_placement": ["is", "set"]},
			fields=["name", "active_placement", "full_name"],
		)
		for applicant in applicants:
			if frappe.db.get_value("Placement", applicant.active_placement, "status") == "Departed":
				continue
			recipients = set(_management_recipients())
			officer = _recipient_for_placement(applicant.active_placement)
			if officer:
				recipients.add(officer)
			context = {
				"applicant": applicant.name,
				"full_name": applicant.full_name,
				"days_remaining": tier_days,
				"placement": applicant.active_placement,
			}
			for recipient in recipients:
				send(recipient, context)


def contract_age_watchdog():
	"""Part A.4: contract-age clock from Placement.contract_signed_date, admin-configurable
	threshold, alerts only while still active (not yet Departed). Goes to the LMIS officer plus
	Manager + Admin for management visibility of stale cases (2026-09-05)."""
	threshold_days = frappe.get_single("Notification Config").contract_age_threshold_days or 30
	cutoff_date = add_days(today(), -threshold_days)

	# A list of conditions, not a dict: the date needs two. "<= cutoff" alone also matches a
	# placement with no contract date (an empty date compares as very old), and a contract that
	# was never signed has no age to report.
	placements = frappe.get_all(
		"Placement",
		filters=[
			["status", "!=", "Departed"],
			["contract_signed_date", "is", "set"],
			["contract_signed_date", "<=", cutoff_date],
		],
		fields=["name", "contract_signed_date"],
	)
	management = _management_recipients()
	send = _daily_notifier("contract_age_alert")
	for placement in placements:
		recipients = set(management)
		officer = _recipient_for_placement(placement.name)
		if officer:
			recipients.add(officer)
		age_days = (getdate(today()) - getdate(placement.contract_signed_date)).days
		context = {"placement": placement.name, "age_days": age_days, "threshold_days": threshold_days}
		for recipient in recipients:
			send(recipient, context)


def wakala_reminder_watchdog():
	"""2026-08-29 fix: the Wakala fee is paid by the *foreign agency* (Contractor), not internal
	staff — this watchdog previously (wrongly) notified the LMIS officer instead. Also moved
	from Mon/Thu to Fri/Sat/Sun (business ask: remind before the Monday document-submission
	deadline, not after). Wakala now lives as fields on the "Embassy" step (was a standalone
	"Embassy/Wakala" step_type) — see clearance_step.json's wakala_status field."""
	unpaid_steps = frappe.get_all(
		"Clearance Step",
		filters={"step_type": "Embassy", "wakala_status": ["!=", "Paid"], "status": ["not in", ["Stamped", "Cancelled"]]},
		fields=["name", "placement"],
	)
	# One reminder per step per day, however often the sweep runs. A placement has one Embassy step,
	# so "this placement was reminded today" identifies the step. The manual trigger
	# (notification_api.trigger_wakala_reminder) is a person's decision and is not limited here.
	reminded = {placement for _recipient, placement in _sent_today("wakala_payment_reminder")}
	for step in unpaid_steps:
		if step.placement not in reminded:
			send_wakala_reminder(step.name, step.placement)


def send_wakala_reminder(clearance_step_name, placement_name):
	"""Recipient is the paying Contractor's linked User — never internal staff (that was the
	bug). WhatsApp delivery needs a `phone` key in context (pulled from the Contractor's User's
	mobile_no); previously nothing supplied one, so WhatsApp silently failed every time."""
	contractor_name = frappe.db.get_value("Placement", placement_name, "contractor")
	if not contractor_name:
		return
	recipient = frappe.db.get_value("Contractor", contractor_name, "user")
	if not recipient:
		return
	phone = frappe.db.get_value("User", recipient, "mobile_no")
	notify(
		recipient,
		"wakala_payment_reminder",
		{"clearance_step": clearance_step_name, "placement": placement_name},
	)
	# WhatsApp reminder too, per the spec's explicit "WhatsApp + portal notification" pairing, once
	# WhatsApp is set up; until then nothing is attempted or logged. Delivery falls back gracefully
	# (attempt_push_delivery never raises) if the contractor's phone isn't set.
	if not whatsapp_configured():
		return
	notify(
		recipient,
		"wakala_payment_reminder",
		{
			"clearance_step": clearance_step_name,
			"placement": placement_name,
			"phone": phone,
			"message": f"Wakala payment reminder for Clearance Step {clearance_step_name}.",
		},
		channel="WhatsApp",
	)


TAESHIR_INJAZ_REMINDER_TIERS_DAYS = [3, 2, 1]


def taeshir_injaz_reminder_watchdog():
	"""New (2026-08-29): reminds whoever holds the Saudi Taeshir role when a Taeshir
	appointment is 3/2/1 days out and Injaz still hasn't been paid — arriving at the
	appointment unpaid forfeits the (separate) appointment fee. Push only, deliberately no
	WhatsApp (that channel is reserved for reaching the external foreign agency via Wakala;
	Taeshir/Injaz reminders go to internal staff already using the system)."""
	send = _daily_notifier("taeshir_injaz_payment_reminder")
	taeshir_users = frappe.get_all("Has Role", filters={"role": "Saudi Taeshir"}, pluck="parent")
	for tier_days in TAESHIR_INJAZ_REMINDER_TIERS_DAYS:
		target_date = add_days(today(), tier_days)
		# Appointment + Injaz payment now live on the step's Injaz Attempt rows; remind on the
		# current (Active) attempt whose appointment is in this tier and whose Injaz is still unpaid.
		due_attempts = frappe.get_all(
			"Injaz Attempt",
			filters={"outcome": "Active", "payment_status": ["!=", "Paid"], "appointment_date": target_date},
			fields=["parent"],
		)
		for attempt in due_attempts:
			step = frappe.db.get_value(
				"Clearance Step", attempt.parent, ["name", "placement", "step_type", "status"], as_dict=True
			)
			if not step or step.step_type != "Taeshir" or step.status in ("Issued", "Complete", "Cancelled"):
				continue
			context = {
				"clearance_step": step.name,
				"placement": step.placement,
				"days_remaining": tier_days,
			}
			for recipient in taeshir_users:
				send(recipient, context)


def departure_due_watchdog():
	"""The flight/departure date has arrived (or passed) but the placement is still Ticketed --
	nobody has confirmed the worker actually departed. Remind the Ticketer(s) on the flight date
	and every day after while it stays Ticketed (overdue), stopping once it reaches Departed."""
	due_placements = frappe.get_all(
		"Placement",
		filters={"status": "Ticketed", "flight_date": ["<=", today()]},
		fields=["name", "flight_date"],
	)
	ticketers = frappe.get_all("Has Role", filters={"role": "Ticketer"}, pluck="parent")
	send = _daily_notifier("departure_due_reminder")
	for placement in due_placements:
		days_overdue = (getdate(today()) - getdate(placement.flight_date)).days
		context = {
			"placement": placement.name,
			"flight_date": str(placement.flight_date),
			"days_overdue": days_overdue,
		}
		for recipient in ticketers:
			send(recipient, context)
