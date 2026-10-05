# Copyright (c) 2026, Agency and contributors
# License: MIT. See LICENSE
#
# How a person is named in any text a user reads (push notifications, the in-app feed, task
# descriptions): a candidate is "Full Name (passport)", a staff member is their full name. Record
# IDs (PLM-, APP-, CLR-, ...) and e-mail addresses never appear in such text.
#
# Kept free of imports from the engines so that notification_engine, notification_feed and
# clearance_engine can all use it (clearance_engine imports notification_engine).

import re

import frappe

UNKNOWN_CANDIDATE = "a candidate"

# " [PLM-00012]" as older task descriptions carried it.
_BRACKETED_RECORD_ID = re.compile(r"\s*\[[A-Z]{3}-[^\]]*\]")


def candidate_label(applicant=None, placement=None, clearance_step=None):
	"""'Full Name (passport)' for the candidate behind an applicant, a placement or a clearance
	step -- whichever is given, most specific first."""
	if clearance_step and not placement:
		placement = frappe.db.get_value("Clearance Step", clearance_step, "placement")
	if placement:
		applicant = frappe.db.get_value("Placement", placement, "applicant") or applicant
	if not applicant:
		return UNKNOWN_CANDIDATE
	return format_candidate(*(frappe.db.get_value("Applicant", applicant, ["full_name", "passport_number"]) or (None, None)))


def format_candidate(full_name, passport_number):
	if not full_name:
		return UNKNOWN_CANDIDATE
	return f"{full_name} ({passport_number})" if passport_number else full_name


def step_label(clearance_step):
	"""'Embassy step for Full Name (passport)'."""
	step_type = frappe.db.get_value("Clearance Step", clearance_step, "step_type")
	who = candidate_label(clearance_step=clearance_step)
	return f"{step_type} step for {who}" if step_type else f"Clearance step for {who}"


def user_label(user):
	"""A staff member's full name; never their e-mail address."""
	return (frappe.db.get_value("User", user, "full_name") if user else None) or "A colleague"


def without_record_ids(text, placement=None):
	"""Task text written before candidates were named by passport ('Book ticket for Full Name
	[PLM-00012]') read the current way. Text already in the current form comes back unchanged."""
	text = _BRACKETED_RECORD_ID.sub("", text or "")
	if not placement:
		return text
	applicant = frappe.db.get_value("Placement", placement, "applicant")
	full_name, passport_number = (
		frappe.db.get_value("Applicant", applicant, ["full_name", "passport_number"]) if applicant else None
	) or (None, None)
	label = format_candidate(full_name, passport_number)
	if full_name and label not in text:
		text = text.replace(full_name, label, 1)
	return text
