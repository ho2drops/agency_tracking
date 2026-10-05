# Copyright (c) 2026, Agency and contributors
# License: MIT. See LICENSE
#
# Live, permission-scoped in-app notification feed. Backs notification_api.get_live_alerts.
#
# Replaces the frontend's getComplianceNotificationsV2(), which recomputed everything
# client-side from listApplicantsV2()/listPlacementsV2() -- both default to
# limit_page_length=100, silently dropping any alert for a record outside the 100
# most-recently-modified rows -- and referenced a nonexistent `passport_expiry` field (the real
# field is `passport_expiry_date`), so that watchdog never actually fired.
#
# Each builder below queries via frappe.get_list (permission-checked, unlike frappe.get_all),
# so the existing get_permission_query_conditions scoping on Applicant/Placement/Clearance Step
# applies automatically -- a clearance-country-role user sees only their own cases, exactly as
# they already do in every other list view, with no separate visibility logic needed here.
#
# Keys always bucket by tier/band where the underlying condition escalates over time (medical/
# passport expiry, contract age, Taeshir/Injaz countdown) -- a dismissal at one tier must not
# silence a later, more urgent tier for the same record. This intentionally differs from the
# frontend's old IDs (e.g. `med-expiry-watchdog-${app.name}`, no tier), which would have let an
# early dismissal suppress a later escalation once dismissal state was actually persisted.
#
# Medical expiry deliberately excludes Departed placements (same business rule already coded in
# watchdogs.medical_expiry_watchdog: an expiring medical no longer matters once the worker has
# flown) even though the old frontend derivation didn't apply that filter -- a stale medical
# alert for someone already departed is noise, not signal.

import frappe
from frappe.utils import getdate, today

from agency_tracking.labels import candidate_labels, format_candidate

MEDICAL_EXPIRY_TIER_DAYS = [14, 10, 7, 3, 1]
PASSPORT_EXPIRY_WINDOW_DAYS = 30
CONTRACT_AGE_APPROACHING_DAYS = 25
CONTRACT_AGE_CRITICAL_DAYS = 30
TAESHIR_INJAZ_TIER_DAYS = [3, 2, 1]


def _tier_for(diff_days, tiers):
	"""Smallest configured tier >= diff_days -- which escalation bucket a countdown is
	currently in (e.g. day 9 of a [14,10,7,3,1] schedule buckets into the 10-day tier, not the
	14-day one). Order-independent -- sorts ascending itself."""
	for tier in sorted(tiers):
		if diff_days <= tier:
			return tier
	return None


def _applicant_url(applicant_name):
	return f"/applicants/{applicant_name}"


def medical_expiry_alerts():
	applicants = frappe.get_list(
		"Applicant",
		filters={"medical_expiry_date": ["is", "set"], "active_placement": ["is", "set"]},
		fields=["name", "full_name", "passport_number", "medical_expiry_date", "active_placement"],
	)
	alerts = []
	departed = set(
		frappe.get_all(
			"Placement",
			filters={"name": ["in", [a.active_placement for a in applicants] or [""]], "status": "Departed"},
			pluck="name",
		)
	)
	for app in applicants:
		who = format_candidate(app.full_name, app.passport_number)
		diff_days = (getdate(app.medical_expiry_date) - getdate(today())).days
		if diff_days < 0:
			alerts.append(
				{
					"key": f"med-expired:{app.name}",
					"title": f"Medical Expired: {who}",
					"body": f"Medical clearance for {who} expired on {app.medical_expiry_date}. "
					f"Processing is halted until renewed.",
					"category": "compliance",
					"severity": "urgent",
					"reference_doctype": "Applicant",
					"reference_name": app.name,
					"action_url": _applicant_url(app.name),
				}
			)
			continue
		if diff_days > MEDICAL_EXPIRY_TIER_DAYS[0]:
			continue
		if app.active_placement in departed:
			continue
		tier = _tier_for(diff_days, MEDICAL_EXPIRY_TIER_DAYS)
		alerts.append(
			{
				"key": f"med-expiry:{app.name}:{tier}",
				"title": f"Medical Expiry Watchdog: {who} ({diff_days}d remaining)",
				"body": f"Medical clearance for {who} expires on {app.medical_expiry_date} "
				f"({diff_days} days remaining). LMIS re-examination or clearance renewal required.",
				"category": "compliance",
				"severity": "urgent" if diff_days <= 3 else "warning",
				"reference_doctype": "Applicant",
				"reference_name": app.name,
				"action_url": _applicant_url(app.name),
			}
		)
	return alerts


def passport_expiry_alerts():
	applicants = frappe.get_list(
		"Applicant",
		filters={"passport_expiry_date": ["is", "set"]},
		fields=["name", "full_name", "passport_number", "passport_expiry_date"],
	)
	alerts = []
	for app in applicants:
		who = format_candidate(app.full_name, app.passport_number)
		diff_days = (getdate(app.passport_expiry_date) - getdate(today())).days
		if diff_days < 0:
			alerts.append(
				{
					"key": f"passport-expired:{app.name}",
					"title": f"Expired Passport: {who}",
					"body": f"Passport for {who} expired on {app.passport_expiry_date}. "
					f"Processing is halted until renewed.",
					"category": "compliance",
					"severity": "urgent",
					"reference_doctype": "Applicant",
					"reference_name": app.name,
					"action_url": _applicant_url(app.name),
				}
			)
		elif diff_days <= PASSPORT_EXPIRY_WINDOW_DAYS:
			alerts.append(
				{
					"key": f"passport-expiry:{app.name}:{diff_days}",
					"title": f"Passport Expiring Soon: {who} ({diff_days}d)",
					"body": f"{who}'s passport expires on {app.passport_expiry_date}. "
					f"Immediate renewal required before embassy visa stamping.",
					"category": "compliance",
					"severity": "urgent",
					"reference_doctype": "Applicant",
					"reference_name": app.name,
					"action_url": _applicant_url(app.name),
				}
			)
	return alerts


def contract_age_alerts():
	placements = frappe.get_list(
		"Placement",
		filters={"status": ["not in", ["Departed", "Cancelled"]], "contract_signed_date": ["is", "set"]},
		fields=["name", "applicant", "contract_signed_date", "status"],
	)
	alerts = []
	label_of, _ = candidate_labels(applicants=[p.applicant for p in placements])
	for plc in placements:
		age_days = (getdate(today()) - getdate(plc.contract_signed_date)).days
		who = label_of(applicant=plc.applicant)
		if age_days >= CONTRACT_AGE_CRITICAL_DAYS:
			alerts.append(
				{
					"key": f"contract-age:{plc.name}:critical",
					"title": f"Critical Contract Age: {who} ({age_days}d)",
					"body": f"Placement for {who} has reached {age_days} days since contract "
					f"signing and is still not Departed (cutoff: {CONTRACT_AGE_CRITICAL_DAYS}d). "
					f"Priority clearance and ticketing required.",
					"category": "compliance",
					"severity": "urgent",
					"reference_doctype": "Placement",
					"reference_name": plc.name,
					"action_url": _applicant_url(plc.applicant),
				}
			)
		elif age_days >= CONTRACT_AGE_APPROACHING_DAYS:
			alerts.append(
				{
					"key": f"contract-age:{plc.name}:approaching",
					"title": f"Approaching Ticket Deadline: {who} ({age_days}d)",
					"body": f"Placement for {who} is at {age_days} days since signing "
					f"(critical cutoff approaching at {CONTRACT_AGE_CRITICAL_DAYS} days). Ensure "
					f"ticketing clearance is expedited.",
					"category": "workflow",
					"severity": "warning",
					"reference_doctype": "Placement",
					"reference_name": plc.name,
					"action_url": _applicant_url(plc.applicant),
				}
			)
		if plc.status == "Ticketed":
			alerts.append(
				{
					"key": f"plc-med2:{plc.name}",
					"title": f"Pre-Departure Medical 2 Due: {who}",
					"body": "Candidate is Ticketed for flight departure. Pre-departure medical fitness "
					"verification required before airport departure clearance.",
					"category": "workflow",
					"severity": "warning",
					"reference_doctype": "Placement",
					"reference_name": plc.name,
					"action_url": _applicant_url(plc.applicant),
				}
			)
	return alerts


def wakala_alerts():
	"""Embassy step, Wakala unpaid, not yet Stamped/Cancelled -- same condition as
	watchdogs.wakala_reminder_watchdog, just read-scoped instead of recipient-targeted."""
	steps = frappe.get_list(
		"Clearance Step",
		filters={"step_type": "Embassy", "wakala_status": ["!=", "Paid"], "status": ["not in", ["Stamped", "Cancelled"]]},
		fields=["name", "placement"],
	)
	alerts = []
	label_of, applicant_of = candidate_labels(placements=[s.placement for s in steps])
	for step in steps:
		applicant = applicant_of(step.placement)
		who = label_of(placement=step.placement)
		alerts.append(
			{
				"key": f"wakala:{step.name}",
				"title": f"Wakala Reminder: {who}",
				"body": f"Wakala authorization and fee payment pending for {who}. "
				f"Must be completed prior to the Monday Embassy cutoff.",
				"category": "compliance",
				"severity": "urgent",
				"reference_doctype": "Clearance Step",
				"reference_name": step.name,
				"action_url": _applicant_url(applicant) if applicant else "/applicants",
			}
		)
	return alerts


def taeshir_injaz_alerts():
	"""Taeshir step whose active Injaz Attempt appointment is 3/2/1 days out and still unpaid --
	same condition as watchdogs.taeshir_injaz_reminder_watchdog."""
	steps = frappe.get_list(
		"Clearance Step",
		filters={"step_type": "Taeshir", "status": ["not in", ["Issued", "Complete", "Cancelled"]]},
		fields=["name", "placement"],
	)
	alerts = []
	attempts_of = {}
	for attempt in frappe.get_all(
		"Injaz Attempt",
		filters={
			"parent": ["in", [s.name for s in steps] or [""]],
			"parenttype": "Clearance Step",
			"outcome": "Active",
			"payment_status": ["!=", "Paid"],
		},
		fields=["parent", "appointment_date"],
	):
		attempts_of.setdefault(attempt.parent, []).append(attempt)
	label_of, applicant_of = candidate_labels(placements=[s.placement for s in steps if s.name in attempts_of])
	for step in steps:
		applicant = applicant_of(step.placement)
		for attempt in attempts_of.get(step.name, []):
			if not attempt.appointment_date:
				continue
			diff_days = (getdate(attempt.appointment_date) - getdate(today())).days
			if diff_days < 0 or diff_days > TAESHIR_INJAZ_TIER_DAYS[0]:
				continue
			tier = _tier_for(diff_days, TAESHIR_INJAZ_TIER_DAYS)
			who = label_of(placement=step.placement)
			alerts.append(
				{
					"key": f"taeshir-injaz:{step.name}:{tier}",
					"title": f"Taeshir / Injaz Reminder: {who} ({diff_days}d)",
					"body": f"Injaz payment for {who} "
					f"is still unpaid with the Taeshir appointment {diff_days} day(s) out. Arriving "
					f"unpaid forfeits the appointment fee.",
					"category": "workflow",
					"severity": "warning",
					"reference_doctype": "Clearance Step",
					"reference_name": step.name,
					"action_url": _applicant_url(applicant) if applicant else "/applicants",
				}
			)
	return alerts


def complaint_alerts():
	"""Delegates the visibility gate to the existing endpoint (Complaint Manager/Admin/Manager/
	System Manager) rather than duplicating it -- Complaint has no get_permission_query_conditions
	hook of its own, so this role check IS the scoping."""
	from agency_tracking.complaint_api import list_unresolved_complaints

	try:
		complaints = list_unresolved_complaints()
	except frappe.PermissionError:
		return []

	alerts = []
	label_of, _ = candidate_labels(placements=[c.get("placement") for c in complaints])
	for comp in complaints:
		label = label_of(placement=comp.get("placement"))
		alerts.append(
			{
				"key": f"complaint:{comp['name']}",
				"title": f"Active Complaint: {label}",
				"body": f'Dispute ticket for {label}: '
				f'"{comp.get("description") or "Active complaint"}"',
				"category": "complaints",
				"severity": "urgent",
				"reference_doctype": "Complaint",
				"reference_name": comp["name"],
				"action_url": "/complaints",
			}
		)
	return alerts


ALERT_BUILDERS = [
	medical_expiry_alerts,
	passport_expiry_alerts,
	contract_age_alerts,
	wakala_alerts,
	taeshir_injaz_alerts,
	complaint_alerts,
]


def get_all_alerts():
	"""Runs every builder for the calling user (each queries via frappe.get_list, so
	permission scoping is automatic) and returns the combined, unordered alert list. Any single
	builder failing must not take down the whole feed."""
	alerts = []
	for builder in ALERT_BUILDERS:
		try:
			alerts.extend(builder())
		except Exception:
			frappe.log_error(title=f"notification_feed builder failed: {builder.__name__}")
	return alerts
