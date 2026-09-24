# Copyright (c) 2026, Agency and contributors
# License: MIT. See LICENSE
#
# Part F: module-scoped whitelisted functions, no raw /api/resource/* exposure.

import frappe
from frappe.utils import formatdate, today

from agency_tracking.clearance_engine import assign_clearance_step as _engine_assign_clearance_step
from agency_tracking.clearance_engine import _broadcast_todo_to_role_holders
from agency_tracking.agency_tracking.doctype.clearance_step.clearance_step import CLEARANCE_ROLE_BY_STEP_TYPE
from agency_tracking.pdf_utils import asset_datauri, code128_b_datauri, embed_image_datauri, render_pdf
from agency_tracking.roles import INTERNAL_STAFF_ROLES
from agency_tracking.state_machine import assert_clearance_step_not_terminal, auto_advance_placement_if_ready, log_action

INJAZ_TEMPLATE = "templates/injaz_document.html"
# The sending (Ethiopian) agency named at the top of the Injaz application header, and the contact
# email printed under the embassy block -- the same identity the Injaz parser recognises as
# origin_agency (ANWAR SULTAN FOREIGN EMPLOYMENT AGENT).
ORIGIN_AGENCY_FULL = "ANWAR SULTAN FOREIGN EMPLOYMENT AGENT"
ORIGIN_AGENCY_EMAIL = "rawnasultan03@gmail.com"


def get_agency_email():
	"""Agency contact email from Agency Tracking Settings, falling back to ORIGIN_AGENCY_EMAIL when
	the setting is empty (e.g. a site migrated before the field existed)."""
	return frappe.db.get_single_value("Agency Tracking Settings", "agency_email") or ORIGIN_AGENCY_EMAIL
# Applicant.religion -> the wording the Saudi consular form uses.
_RELIGION_MAP = {"Muslim": "Islam"}


def _assign_or_reassign(clearance_step_name, user):
	"""Shared implementation for assign_clearance_step/reassign_clearance_step (2026-09-12,
	consolidated after finding the two had silently drifted apart): they were two separate
	whitelisted endpoints doing the identical mutation (cancel any open ToDo, create a new one for
	`user`), but only reassign_clearance_step had the terminal-placement guard and an audit log --
	confirmed live that a Clearance-Officer-only account, correctly blocked by
	reassign_clearance_step on an already-Departed placement, could reassign that exact same step
	on that exact same placement anyway just by calling assign_clearance_step instead. Routing both
	through one function means their guards can no longer disagree, whichever name is called."""
	step = _load_actionable_step(clearance_step_name)
	_engine_assign_clearance_step(step.name, user)
	log_action("Clearance Step", step.name, f"[{step.title or step.name}] Assigned to {user}")
	return step


@frappe.whitelist()
def assign_clearance_step(clearance_step_name=None, user=None, step_name=None, assigned_to=None, **kwargs):
	"""Assign or reassign a clearance step to an officer."""
	if not ({"Manager", "Admin", "Clearance Officer", "System Manager"} & set(frappe.get_roles())):
		frappe.throw("Not permitted.", frappe.PermissionError)
	clearance_step_name = clearance_step_name or step_name or kwargs.get("name")
	user = user or assigned_to or kwargs.get("user")
	if not clearance_step_name or not user:
		frappe.throw("Both clearance_step_name and user are required.", frappe.ValidationError)
	step = _assign_or_reassign(clearance_step_name, user)
	return {"status": "success", "clearance_step": step.name, "assigned_to": user}


# LMIS (both countries) completes to "Issued" -- everything else that uses the plain
# complete_clearance_step() path (Taeshir, Telesign) uses the generic "Complete". Embassy
# (Saudi + Kuwait) does NOT go through this function at all -- its Pending -> Submitted ->
# Stamped/Rejected flow needs its own functions below (a remark is required for Rejected,
# and "Stamped" isn't just "the step finished", it's a specific outcome distinct from failure).
TERMINAL_STATUS_BY_STEP_TYPE = {
	"LMIS Clearance": "Issued",
	"Kuwait LMIS": "Issued",
}
DEFAULT_TERMINAL_STATUS = "Complete"


def _is_assigned_officer(clearance_step_name):
	return bool(
		frappe.db.exists(
			"ToDo",
			{
				"reference_type": "Clearance Step",
				"reference_name": clearance_step_name,
				"allocated_to": frappe.session.user,
				"status": "Open",
			},
		)
	)


def _can_act_on_step(step):
	"""Manager/Admin/System Manager always; the officer currently ToDo-assigned to this exact row;
	or anyone holding the role mapped to this step_type."""
	if {"Manager", "Admin", "System Manager"} & set(frappe.get_roles()):
		return True
	if _is_assigned_officer(step.name):
		return True
	required_role = CLEARANCE_ROLE_BY_STEP_TYPE.get(step.step_type)
	return bool(required_role and required_role in frappe.get_roles())


def _close_open_todos(clearance_step_name):
	open_todos = frappe.get_all(
		"ToDo",
		filters={"reference_type": "Clearance Step", "reference_name": clearance_step_name, "status": "Open"},
		pluck="name",
	)
	for todo_name in open_todos:
		frappe.db.set_value("ToDo", todo_name, "status", "Closed")


@frappe.whitelist()
def complete_clearance_step(
	clearance_step_name=None,
	step_name=None,
	name=None,
	reference_no=None,
	amount=None,
	date_completed=None,
	**kwargs,
):
	"""Mark a Clearance Step complete/Issued. Not for Embassy steps -- use
	submit_embassy_step/stamp_embassy_step/reject_embassy_step instead."""
	clearance_step_name = clearance_step_name or step_name or name or kwargs.get("clearance_step")
	if not clearance_step_name:
		frappe.throw("clearance_step_name is required.", frappe.ValidationError)

	step = frappe.get_doc("Clearance Step", clearance_step_name)
	if step.step_type in ("Embassy", "Kuwait Embassy"):
		frappe.throw(
			"Embassy steps use submit_embassy_step/stamp_embassy_step/reject_embassy_step, not complete_clearance_step.",
			frappe.ValidationError,
		)
	if not _can_act_on_step(step):
		frappe.throw("Not permitted.", frappe.PermissionError)
	assert_clearance_step_not_terminal(step)

	terminal_status = TERMINAL_STATUS_BY_STEP_TYPE.get(step.step_type, DEFAULT_TERMINAL_STATUS)
	# 2026-09-12: distinguishes a fresh completion from a correction re-save of an already-Issued/
	# Complete step (allowed since 34fdb49 relaxed the step-terminal lock for data corrections --
	# but that relaxation also removed the only guard stopping this from firing on any OTHER
	# status too). A fresh completion is only legal from Pending (an officer who does the work and
	# marks it done without separately clicking "Start" first) or In Progress; anything else is a
	# nonsensical call this step type should never actually be in.
	is_correction = step.status == terminal_status
	if not is_correction and step.status not in ("Pending", "In Progress"):
		frappe.throw(f"A '{step.status}' clearance step cannot be completed.", frappe.ValidationError)

	if not step.date_started:
		step.date_started = today()
	step.status = terminal_status
	if date_completed:
		step.date_completed = date_completed
	elif not is_correction:
		step.date_completed = today()
	# else: correcting other fields without an explicit date_completed -- leave the existing
	# completion date untouched rather than silently bumping it to today.
	step.completed_by = frappe.session.user
	if reference_no:
		step.reference_no = reference_no
	if amount is not None:
		step.amount = amount
		step.payment_status = "Paid"
	step.save(ignore_permissions=True)
	_close_open_todos(clearance_step_name)
	log_action(
		"Clearance Step",
		step.name,
		f"[{step.title or step.name}] {'Corrected' if is_correction else terminal_status}"
		f" (reference {step.reference_no or '-'}, date {step.date_completed or '-'})",
	)
	auto_advance_placement_if_ready(step.placement)
	return step.as_dict()


def _load_actionable_step(clearance_step_name, expected_types=None):
	"""Load the EXACT Clearance Step to act on -- never guess another placement's step. A missing or
	stale/invalid id is a hard error (2026-09-05 data-integrity fix): the previous behavior silently
	fell back to "the most recently created active step of this type" ACROSS ALL PLACEMENTS, so a
	stale id from the UI would land the action on a different worker's placement -- producing e.g. a
	Departed placement whose own LMIS step is still Pending. Also refuses to edit any step once its
	placement is Departed/Cancelled (that history is final)."""
	if not clearance_step_name:
		frappe.throw("clearance_step_name is required.", frappe.ValidationError)
	if not frappe.db.exists("Clearance Step", clearance_step_name):
		frappe.throw(f"Clearance Step {clearance_step_name} not found.", frappe.DoesNotExistError)
	step = frappe.get_doc("Clearance Step", clearance_step_name)
	if expected_types and step.step_type not in expected_types:
		frappe.throw(
			f"{clearance_step_name} is a '{step.step_type}' step, not one of {sorted(expected_types)}.",
			frappe.ValidationError,
		)
	placement_status = frappe.db.get_value("Placement", step.placement, "status")
	if placement_status in ("Departed", "Cancelled"):
		frappe.throw(
			f"{step.placement} is already {placement_status}; its clearance steps can no longer be edited.",
			frappe.ValidationError,
		)
	return step


@frappe.whitelist()
def start_clearance_step(clearance_step_name=None, step_name=None, name=None, **kwargs):
	clearance_step_name = clearance_step_name or step_name or name or kwargs.get("clearance_step")
	step = _load_actionable_step(clearance_step_name)
	if not _can_act_on_step(step):
		frappe.throw("Not permitted.", frappe.PermissionError)
	if step.status == "In Progress":
		return step.as_dict()
	# S-2: only a not-yet-started step can be started (never revert a Submitted/Complete/etc. step).
	if step.status != "Pending":
		frappe.throw(f"A '{step.status}' clearance step cannot be (re)started.", frappe.ValidationError)
	step.status = "In Progress"
	step.date_started = today()
	step.save(ignore_permissions=True)
	log_action("Clearance Step", step.name, f"[{step.title or step.name}] Started")
	return step.as_dict()


@frappe.whitelist()
def submit_embassy_step(clearance_step_name=None, override_reason=None, **kwargs):
	"""Documents submitted (Monday). Saudi/Kuwait Embassy only."""
	clearance_step_name = clearance_step_name or kwargs.get("name") or kwargs.get("clearance_step")
	step = _load_actionable_step(clearance_step_name, {"Embassy", "Kuwait Embassy", "Saudi Embassy"})
	if not _can_act_on_step(step):
		frappe.throw("Not permitted.", frappe.PermissionError)
	if step.status == "Submitted":
		return step.as_dict()
	# S-2: submit only from a pre-submit state (not from an already-Stamped/Rejected step).
	if step.status not in ("Pending", "In Progress"):
		frappe.throw(f"An embassy step that is '{step.status}' cannot be submitted.", frappe.ValidationError)
	# Wakala (Saudi corridor only -- Kuwait's own "Kuwait Embassy" step_type doesn't carry this
	# fee, see wakala_amount's depends_on) must be Paid before documents go out, per this field's
	# own documented rule. Previously undocumented in code -- nothing actually enforced it, so a
	# step could be Submitted regardless of Wakala status. Manager/Admin/System Manager can still
	# push through unpaid with a written reason, same override pattern used elsewhere (Part C).
	if step.step_type == "Embassy" and step.wakala_status != "Paid":
		is_management = bool({"Manager", "Admin", "System Manager"} & set(frappe.get_roles()))
		if not (is_management and override_reason):
			frappe.throw(
				"Wakala must be Paid before Embassy documents can be Submitted.",
				frappe.ValidationError,
			)
		log_action(
			"Clearance Step",
			step.name,
			f"[{step.title or step.name}] Submitted with Wakala unpaid (Manager override): {override_reason}",
		)
	step.status = "Submitted"
	step.date_started = today()
	step.save(ignore_permissions=True)
	return step.as_dict()


@frappe.whitelist()
def record_wakala_payment(clearance_step_name=None, wakala_status=None, wakala_amount=None, paid_date=None, reference_no=None, **kwargs):
	"""Record the Saudi Embassy step's Wakala fee payment (paid by the foreign agency, not
	internal staff -- this just records that it landed). Embassy-step-only: Kuwait's own
	"Kuwait Embassy" step_type doesn't carry a Wakala fee (see wakala_amount's depends_on).

	reference_no here is the Musaned authorization number, stored in its own
	wakala_reference_no field -- NOT the step's shared reference_no (2026-09-11 fix: an earlier
	version of this endpoint wrote it into reference_no, silently overwriting the same step's
	Embassy visa/stamp reference set by stamp_embassy_step -- a real, different number).

	Replaces the previous only-way-to-change-it: a raw desk-form field edit, which had no role
	gate beyond blanket Clearance Step write access (shared by every clearance-country role, not
	just Embassy), no validation, and no audit trail."""
	clearance_step_name = clearance_step_name or kwargs.get("name") or kwargs.get("clearance_step")
	step = _load_actionable_step(clearance_step_name, {"Embassy"})
	if not _can_act_on_step(step):
		frappe.throw("Not permitted.", frappe.PermissionError)
	if wakala_status and wakala_status not in ("Pending", "Paid"):
		frappe.throw(f"Invalid Wakala status '{wakala_status}'.", frappe.ValidationError)
	if wakala_amount is not None:
		step.wakala_amount = wakala_amount
	if wakala_status:
		step.wakala_status = wakala_status
		step.wakala_paid_date = (paid_date or today()) if wakala_status == "Paid" else None
	if reference_no is not None:
		step.wakala_reference_no = reference_no
	step.save(ignore_permissions=True)
	log_action(
		"Clearance Step",
		step.name,
		f"[{step.title or step.name}] Wakala {step.wakala_status}: {step.wakala_amount or '-'}",
	)
	return step.as_dict()


def _taeshir_status_for_placement(placement_name):
	return frappe.db.get_value(
		"Clearance Step",
		{"placement": placement_name, "step_type": "Taeshir"},
		"status",
	)


@frappe.whitelist()
def stamp_embassy_step(clearance_step_name=None, reference_no=None, override_reason=None, **kwargs):
	"""Documents returned stamped (Thursday) -- the success outcome."""
	clearance_step_name = clearance_step_name or kwargs.get("name") or kwargs.get("clearance_step")
	reference_no = reference_no or kwargs.get("visa_number") or kwargs.get("reference")
	step = _load_actionable_step(clearance_step_name, {"Embassy", "Kuwait Embassy", "Saudi Embassy"})
	if not _can_act_on_step(step):
		frappe.throw("Not permitted.", frappe.PermissionError)
	is_correction = step.status == "Stamped"
	# S-2: documents can only be Stamped after they were Submitted (the Mon->Thu cycle) --
	# except a correction re-save of an already-Stamped step (2026-09-11: data entered here,
	# e.g. the visa/stamp reference, must stay fixable after the fact -- previously this early-
	# returned silently without ever applying a corrected reference_no).
	if not is_correction and step.status != "Submitted":
		frappe.throw(f"Documents must be Submitted before they can be Stamped (this step is '{step.status}').", frappe.ValidationError)
	# Saudi corridor only: Taeshir runs in parallel with Embassy, but Embassy may not be
	# Stamped (the terminal, success outcome) until Taeshir is done -- Submitted stays
	# allowed regardless, so documents can still go out Monday even if Taeshir is lagging.
	# Taeshir has exactly one terminal status ("Complete" -- it's not in
	# TERMINAL_STATUS_BY_STEP_TYPE so complete_clearance_step() always falls through to
	# DEFAULT_TERMINAL_STATUS; nothing ever sets a Taeshir step to Issued/Stamped/Rejected).
	# Only checked on the real Submitted->Stamped transition, not on a data-only correction.
	if not is_correction and step.step_type == "Embassy":
		taeshir_status = _taeshir_status_for_placement(step.placement)
		if taeshir_status != "Complete":
			frappe.throw(
				f"Taeshir must be complete before the Embassy step can be Stamped (Taeshir is '{taeshir_status or 'not started'}').",
				frappe.ValidationError,
			)
	# Same Wakala gate as submit_embassy_step (Saudi corridor only), re-checked here because
	# a step can reach Stamped without ever re-validating Wakala otherwise: a Manager override
	# at Submit, or wakala_status being reverted via record_wakala_payment after Submit, would
	# both slip past unnoticed. Only on the real Submitted->Stamped transition, not a
	# data-only correction re-save.
	if not is_correction and step.step_type == "Embassy" and step.wakala_status != "Paid":
		is_management = bool({"Manager", "Admin", "System Manager"} & set(frappe.get_roles()))
		if not (is_management and override_reason):
			frappe.throw(
				"Wakala must be Paid before Embassy documents can be Stamped.",
				frappe.ValidationError,
			)
		log_action(
			"Clearance Step",
			step.name,
			f"[{step.title or step.name}] Stamped with Wakala unpaid (Manager override): {override_reason}",
		)
	step.status = "Stamped"
	step.date_completed = today()
	step.completed_by = frappe.session.user
	if reference_no:
		step.reference_no = reference_no
	step.save(ignore_permissions=True)
	_close_open_todos(clearance_step_name)
	log_action(
		"Clearance Step",
		step.name,
		f"[{step.title or step.name}] {'Corrected' if is_correction else 'Stamped'} (reference {step.reference_no or '-'})",
	)
	auto_advance_placement_if_ready(step.placement)
	return step.as_dict()


@frappe.whitelist()
def reject_embassy_step(clearance_step_name=None, rejection_remark=None, **kwargs):
	"""Documents returned rejected (Thursday) -- requires a written remark
	(Clearance Step.validate() also enforces this as a backstop)."""
	if not clearance_step_name:
		frappe.throw("clearance_step_name is required.", frappe.ValidationError)
	if not rejection_remark:
		frappe.throw("A rejection remark is required.", frappe.ValidationError)
	step = frappe.get_doc("Clearance Step", clearance_step_name)
	if step.step_type not in ("Embassy", "Kuwait Embassy"):
		frappe.throw("Only meaningful for an Embassy clearance step.", frappe.ValidationError)
	if not _can_act_on_step(step):
		frappe.throw("Not permitted.", frappe.PermissionError)
	assert_clearance_step_not_terminal(step)
	# S-2: a Rejected outcome only makes sense for documents that were actually Submitted --
	# except correcting the remark on an already-Rejected step (2026-09-11: same
	# forgiving-on-data, firm-on-status-direction policy as stamp_embassy_step).
	if step.status not in ("Submitted", "Rejected"):
		frappe.throw(f"Documents must be Submitted before they can be Rejected (this step is '{step.status}').", frappe.ValidationError)
	is_correction = step.status == "Rejected"
	step.status = "Rejected"
	step.rejection_remark = rejection_remark
	step.date_completed = today()
	step.completed_by = frappe.session.user
	step.save(ignore_permissions=True)
	_close_open_todos(clearance_step_name)
	log_action(
		"Clearance Step",
		step.name,
		f"[{step.title or step.name}] {'Rejection remark corrected' if is_correction else 'Rejected'}: {rejection_remark}",
	)
	return step.as_dict()


@frappe.whitelist()
def reassign_clearance_step(clearance_step_name=None, new_officer=None, **kwargs):
	"""Part A.2: "reassignable by a manager if needed" — the escape hatch for the default
	auto-chain."""
	if not clearance_step_name:
		frappe.throw("clearance_step_name is required.", frappe.ValidationError)
	if not new_officer:
		frappe.throw("new_officer is required.", frappe.ValidationError)
	if not ({"Manager", "Admin", "System Manager"} & set(frappe.get_roles())):
		frappe.throw("Not permitted.", frappe.PermissionError)
	step = _assign_or_reassign(clearance_step_name, new_officer)
	return {"clearance_step": step.name, "assigned_to": new_officer}


def _renotify_reopened_step(step, previous_completed_by):
	"""2026-09-12 (explicit product decision): recreate task visibility for whoever should pick a
	reopened step back up, rather than leaving it silently sitting there with no open ToDo (its
	original one was already closed by _close_open_todos at completion time, confirmed live).
	Mirrors exactly how the step was first assigned when it was created
	(clearance_engine.create_clearance_steps): whoever completed it originally gets their own ToDo
	back first -- they made the call, they're the one who needs to fix it -- then, for the six
	country+step roles, every current role holder also gets a fresh broadcast ToDo, same as a
	brand-new step would. Order matters: assigning completed_by first, then broadcasting
	(additive, never cancels existing ToDos) means completed_by's ToDo survives the broadcast
	step. Best-effort: never blocks the reopen itself, which has already committed by this point."""
	try:
		role = CLEARANCE_ROLE_BY_STEP_TYPE.get(step.step_type)
		completed_by_gets_broadcast = role and previous_completed_by and role in frappe.get_roles(previous_completed_by)
		if previous_completed_by and previous_completed_by != "Administrator" and not completed_by_gets_broadcast:
			_engine_assign_clearance_step(step.name, previous_completed_by)
		if role:
			_broadcast_todo_to_role_holders(step.name, role)
	except Exception:
		frappe.log_error(title="Reopen re-notify failed", message=f"{step.name}: {frappe.get_traceback()}")


@frappe.whitelist()
def reopen_clearance_step(clearance_step_name=None, reason=None, target_status=None, **kwargs):
	"""Manager/Admin/System Manager only. Reverses a step's own terminal OUTCOME (Issued/Complete/
	Stamped/Rejected) when the determination itself was wrong, not just its data -- e.g. an LMIS
	officer marked a step Issued by mistake and needs to genuinely undo that, not just correct a
	reference number (complete_clearance_step's own correction path already covers that case).

	Deliberately scoped to ONLY this step: does not touch, re-check, or cascade into anything
	downstream that may already have relied on the old (wrong) result -- e.g. Embassy work done on
	the assumption LMIS was genuinely Issued, or a Placement that already auto-advanced to Stamped.
	Automatically unwinding those is a much bigger, riskier feature than this one; instead,
	state_machine's Stamped->Ticketed gate re-checks that every mandatory step is still complete at
	that point, so reopening one here quietly blocks Ticketing until a human notices and
	re-completes it -- it doesn't need to reverse the Placement itself."""
	clearance_step_name = clearance_step_name or kwargs.get("name") or kwargs.get("clearance_step")
	if not clearance_step_name:
		frappe.throw("clearance_step_name is required.", frappe.ValidationError)
	if not reason:
		frappe.throw("A reason is required to reopen a clearance step.", frappe.ValidationError)
	if not ({"Manager", "Admin", "System Manager"} & set(frappe.get_roles())):
		frappe.throw("Not permitted.", frappe.PermissionError)

	step = _load_actionable_step(clearance_step_name)
	is_embassy = step.step_type in ("Embassy", "Kuwait Embassy")
	valid_targets = {"Pending", "In Progress", "Submitted"} if is_embassy else {"Pending", "In Progress"}
	target_status = target_status or "In Progress"
	if target_status not in valid_targets:
		frappe.throw(
			f"'{target_status}' isn't a valid reopen target for a '{step.step_type}' step "
			f"(expected one of {sorted(valid_targets)}).",
			frappe.ValidationError,
		)
	if step.status == target_status:
		frappe.throw(f"{step.name} is already '{target_status}'.", frappe.ValidationError)

	previous_status = step.status
	previous_completed_by = step.completed_by
	step.status = target_status
	step.date_completed = None
	step.completed_by = None
	if is_embassy and previous_status == "Rejected":
		step.rejection_remark = None
	step.save(ignore_permissions=True)
	_renotify_reopened_step(step, previous_completed_by)
	log_action(
		"Clearance Step",
		step.name,
		f"[{step.title or step.name}] Reopened: '{previous_status}' -> '{target_status}' ({reason})",
	)
	return step.as_dict()


@frappe.whitelist()
def record_police_ashara(
	clearance_step_name=None,
	police_ashara_status=None,
	police_ashara_payment_status=None,
	police_ashara_amount=None,
	police_ashara_appointment_date=None,
	police_ashara_remark=None,
	**kwargs,
):
	"""Record the Kuwait LMIS step's own Police Ashara sub-check (appointment/status/payment/
	remark) -- Kuwait LMIS-only, mirrors record_wakala_payment's Embassy-only Wakala fields.
	Replaces the previous only-way-to-change-it: a raw desk-form field edit with no role gate
	beyond blanket Clearance Step write access, no validation, and no audit trail."""
	clearance_step_name = clearance_step_name or kwargs.get("name") or kwargs.get("clearance_step")
	step = _load_actionable_step(clearance_step_name, {"Kuwait LMIS"})
	if not _can_act_on_step(step):
		frappe.throw("Not permitted.", frappe.PermissionError)
	if police_ashara_status and police_ashara_status not in ("Pending", "Scheduled", "Completed", "Failed"):
		frappe.throw(f"Invalid Police Ashara status '{police_ashara_status}'.", frappe.ValidationError)
	if police_ashara_payment_status and police_ashara_payment_status not in ("Not Applicable", "Pending", "Paid"):
		frappe.throw(f"Invalid Police Ashara payment status '{police_ashara_payment_status}'.", frappe.ValidationError)
	effective_remark = police_ashara_remark if police_ashara_remark is not None else step.police_ashara_remark
	if police_ashara_status == "Failed" and not effective_remark:
		frappe.throw("A remark is required when Police Ashara status is 'Failed'.", frappe.ValidationError)
	if police_ashara_status:
		step.police_ashara_status = police_ashara_status
	if police_ashara_payment_status:
		step.police_ashara_payment_status = police_ashara_payment_status
	if police_ashara_amount is not None:
		step.police_ashara_amount = police_ashara_amount
	if police_ashara_appointment_date:
		step.police_ashara_appointment_date = police_ashara_appointment_date
	if police_ashara_remark is not None:
		step.police_ashara_remark = police_ashara_remark
	step.save(ignore_permissions=True)
	log_action(
		"Clearance Step",
		step.name,
		f"[{step.title or step.name}] Police Ashara {step.police_ashara_status or '-'}: {step.police_ashara_amount or '-'} (payment {step.police_ashara_payment_status or '-'})",
	)
	return step.as_dict()


@frappe.whitelist()
def record_other_payment(
	clearance_step_name=None,
	payment_type=None,
	amount=None,
	currency=None,
	status=None,
	remark=None,
	receipt_url=None,
	payment_row_name=None,
	**kwargs,
):
	"""Add a new row, or correct an existing one (pass payment_row_name), in a Clearance Step's
	generic "Other Payments" table -- any miscellaneous fee not already covered by a dedicated
	field (Wakala, Police Ashara, Injaz), e.g. an insurance premium. Works for any step_type.
	Replaces the previous only-way-to-change-it: a raw desk-form child-table edit with no role
	gate beyond blanket Clearance Step write access, no validation, and no audit trail."""
	clearance_step_name = clearance_step_name or kwargs.get("name") or kwargs.get("clearance_step")
	step = _load_actionable_step(clearance_step_name)
	if not _can_act_on_step(step):
		frappe.throw("Not permitted.", frappe.PermissionError)

	if currency and currency not in ("SAR", "KWD", "USD", "ETB", "AED", "QAR"):
		frappe.throw(f"Invalid currency '{currency}'.", frappe.ValidationError)
	if status and status not in ("Pending", "Paid"):
		frappe.throw(f"Invalid payment status '{status}'.", frappe.ValidationError)

	is_correction = bool(payment_row_name)
	row = None
	if is_correction:
		row = next((r for r in (step.get("payments") or []) if r.name == payment_row_name), None)
		if row is None:
			frappe.throw(f"Payment row {payment_row_name} not found on {step.name}.", frappe.ValidationError)
	else:
		if not payment_type:
			frappe.throw("payment_type is required to record a new payment.", frappe.ValidationError)
		row = step.append("payments", {})

	if payment_type:
		row.payment_type = payment_type
	if amount is not None:
		row.amount = amount
	if currency:
		row.currency = currency
	if status:
		row.status = status
	if remark is not None:
		row.remark = remark
	if receipt_url is not None:
		row.receipt_url = receipt_url
	step.save(ignore_permissions=True)
	log_action(
		"Clearance Step",
		step.name,
		f"[{step.title or step.name}] {'Corrected' if is_correction else 'Recorded'} payment: "
		f"{row.payment_type or '-'} {row.amount or '-'} {row.currency or ''} ({row.status or '-'})",
	)
	return step.as_dict()


@frappe.whitelist()
def list_my_clearance_steps(placement=None):
	"""A Clearance Officer / Ticketer's queue, optionally filtered by placement."""
	filters = {}
	if placement:
		filters["placement"] = placement
	steps = frappe.get_list(
		"Clearance Step",
		filters=filters,
		fields=[
			"name",
			"placement",
			"step_type",
			"status",
			"sequence_order",
			"is_mandatory",
			"date_started",
			"date_completed",
			"completed_by",
			"reference_no",
			"amount",
			"payment_status",
			"wakala_amount",
			"wakala_status",
			"wakala_reference_no",
			"rejection_remark",
		],
		order_by="sequence_order asc",
	)
	for s in steps:
		if s.get("step_type") == "Taeshir":
			active_injaz = frappe.get_all(
				"Injaz Attempt",
				filters={"parent": s["name"]},
				fields=["injaz_application_id", "appointment_date", "outcome", "injaz_amount"],
				order_by="creation desc",
				limit_page_length=1,
			)
			if active_injaz:
				s["injaz_application_id"] = active_injaz[0].get("injaz_application_id")
				s["appointment_date"] = active_injaz[0].get("appointment_date")
				s["injaz_outcome"] = active_injaz[0].get("outcome")
	return steps


@frappe.whitelist()
def list_assigned_steps(placement=None):
	return list_my_clearance_steps(placement=placement)


@frappe.whitelist()
def list_my_todos(status="Open", **kwargs):
	"""The calling user's own To-Do queue -- both Clearance Step ToDos (per-role broadcasts for
	the six country+step roles, or an exclusive assignment for Clearance Officer/Ticketer) and
	Placement ToDos (ticketing/departure). Always `frappe.session.user`, same as
	notification_feed's endpoints -- never accepts a user param, so nobody can page through
	someone else's queue.

	`status` defaults to "Open" (the actual open task list); pass "" or None for every status
	(Open/Closed/Cancelled) e.g. for a "my task history" view. Filtering by allocated_to=self
	already scopes this to the caller's own rows, so frappe.get_all (no permission query) is
	used -- there is nothing here to leak beyond what the row itself already says."""
	filters = {"allocated_to": frappe.session.user}
	if status:
		filters["status"] = status
	rows = frappe.get_all(
		"ToDo",
		filters=filters,
		fields=["name", "reference_type", "reference_name", "description", "status", "creation"],
		order_by="creation desc",
	)
	step_names = [r.reference_name for r in rows if r.reference_type == "Clearance Step"]
	steps_by_name = {}
	if step_names:
		for s in frappe.get_all(
			"Clearance Step",
			filters={"name": ["in", step_names]},
			fields=["name", "step_type", "status", "placement", "is_mandatory"],
		):
			steps_by_name[s.name] = s

	for row in rows:
		if row.reference_type == "Clearance Step":
			step = steps_by_name.get(row.reference_name)
			row["placement"] = step.placement if step else None
			row["step_type"] = step.step_type if step else None
			row["step_status"] = step.status if step else None
			row["is_mandatory"] = step.is_mandatory if step else None
		else:
			row["placement"] = row.reference_name if row.reference_type == "Placement" else None
	return rows


# ── Taeshir / Injaz attempts ───────────────────────────────────────────────────
# Injaz is data captured inside the Taeshir Clearance Step, now as a table of attempts
# (Injaz Attempt child rows). At most one row is "Active" at a time -- the current appointment
# and its Injaz payment. Missing/forfeiting an appointment closes the current attempt and opens a
# fresh one (new Application ID + appointment), because the Injaz website issues a new number and
# the fee already paid is lost. Reminders (watchdogs.taeshir_injaz_reminder_watchdog) and the
# Injaz PDF read the latest Active attempt.


def _get_active_injaz_attempt(step):
	"""The step's current (Active) Injaz attempt, or None. Falls back to the last row so a step
	whose only attempt was already Completed still resolves for read-only consumers (the PDF)."""
	attempts = step.get("injaz_attempts") or []
	active = [a for a in attempts if a.outcome == "Active"]
	if active:
		return active[-1]
	return attempts[-1] if attempts else None


def _require_taeshir_step(clearance_step_name):
	"""Load a Taeshir step the caller may act on, guarding type + terminal state uniformly."""
	if not clearance_step_name:
		frappe.throw("clearance_step_name is required.", frappe.ValidationError)
	step = frappe.get_doc("Clearance Step", clearance_step_name)
	if step.step_type != "Taeshir":
		frappe.throw("Injaz/appointment actions apply only to a Taeshir clearance step.", frappe.ValidationError)
	if not _can_act_on_step(step):
		frappe.throw("Not permitted.", frappe.PermissionError)
	assert_clearance_step_not_terminal(step)
	return step


@frappe.whitelist()
def set_taeshir_appointment(clearance_step_name=None, appointment_date=None, injaz_application_id=None, **kwargs):
	"""Book (or set details on) the current Taeshir appointment. Creates the Active Injaz attempt
	if none exists yet, otherwise updates it -- so this is the "first booking" entry point.
	Rescheduling an existing booking uses reschedule_taeshir_appointment; a missed/forfeited one
	uses forfeit_injaz_and_restart."""
	clearance_step_name = clearance_step_name or kwargs.get("name") or kwargs.get("clearance_step")
	appointment_date = appointment_date or kwargs.get("date")
	step = _require_taeshir_step(clearance_step_name)

	attempt = _get_active_injaz_attempt(step)
	if attempt is None or attempt.outcome != "Active":
		attempt = step.append("injaz_attempts", {"outcome": "Active", "payment_status": "Unpaid"})
	if appointment_date:
		attempt.appointment_date = appointment_date
	if injaz_application_id:
		attempt.injaz_application_id = injaz_application_id
	step.save(ignore_permissions=True)
	log_action("Clearance Step", step.name, f"[{step.title or step.name}] Taeshir appointment set: {appointment_date or '-'} / Injaz {injaz_application_id or '-'}")
	return step.as_dict()


@frappe.whitelist()
def reschedule_taeshir_appointment(clearance_step_name=None, new_appointment_date=None, cause=None, **kwargs):
	"""Move the current (Active) Taeshir appointment to a new date WITHOUT re-paying Injaz --
	the "unpaid or not-yet-due, free reschedule" case. Same Injaz Application ID / payment carry
	over; only the date changes (with the reason recorded on the attempt)."""
	clearance_step_name = clearance_step_name or kwargs.get("name") or kwargs.get("clearance_step")
	new_appointment_date = new_appointment_date or kwargs.get("appointment_date") or kwargs.get("date")
	if not new_appointment_date:
		frappe.throw("new_appointment_date is required.", frappe.ValidationError)
	step = _require_taeshir_step(clearance_step_name)

	attempt = _get_active_injaz_attempt(step)
	if attempt is None or attempt.outcome != "Active":
		frappe.throw("No active Taeshir appointment to reschedule. Book one with set_taeshir_appointment first.", frappe.ValidationError)
	attempt.appointment_date = new_appointment_date
	if cause:
		attempt.remark = f"Rescheduled: {cause}"
	step.save(ignore_permissions=True)
	log_action("Clearance Step", step.name, f"[{step.title or step.name}] Taeshir appointment rescheduled to {new_appointment_date}" + (f": {cause}" if cause else ""))
	return step.as_dict()


@frappe.whitelist()
def record_injaz_payment(clearance_step_name=None, amount=None, currency=None, receipt_number=None, paid_date=None, payment_status=None, **kwargs):
	"""Mark the current (Active) Injaz attempt Paid, recording amount / currency / receipt. This is
	what clears the taeshir_injaz_payment_reminder for this attempt.

	Pass payment_status="Unpaid" to correct a mistaken Paid confirmation (2026-09-12) -- e.g.
	someone clicked "Paid" with nothing actually paid yet. Deliberately distinct from
	forfeit_injaz_and_restart: that treats the fee as genuinely lost and opens a brand new attempt;
	this is a plain data correction, same attempt, no money actually changed hands."""
	clearance_step_name = clearance_step_name or kwargs.get("name") or kwargs.get("clearance_step")
	step = _require_taeshir_step(clearance_step_name)
	if payment_status and payment_status not in ("Unpaid", "Paid"):
		frappe.throw(f"Invalid payment status '{payment_status}'.", frappe.ValidationError)

	attempt = _get_active_injaz_attempt(step)
	if attempt is None or attempt.outcome != "Active":
		attempt = step.append("injaz_attempts", {"outcome": "Active"})
	attempt.payment_status = payment_status or "Paid"
	attempt.paid_date = (paid_date or today()) if attempt.payment_status == "Paid" else None
	if amount is not None:
		attempt.injaz_amount = amount
	if currency:
		attempt.injaz_currency = currency
	if receipt_number:
		attempt.receipt_number = receipt_number
	step.save(ignore_permissions=True)
	log_action(
		"Clearance Step",
		step.name,
		f"[{step.title or step.name}] Injaz {attempt.payment_status}: {amount if amount is not None else (attempt.injaz_amount or '-')} {currency or attempt.injaz_currency or ''} (receipt {receipt_number or attempt.receipt_number or '-'})",
	)
	return step.as_dict()


@frappe.whitelist()
def forfeit_injaz_and_restart(clearance_step_name=None, reason=None, new_appointment_date=None, new_injaz_application_id=None, **kwargs):
	"""The missed-appointment path: close the current attempt as Forfeited (fee lost) and open a
	fresh Active attempt with a new appointment date + (new) Injaz Application ID, unpaid. If the
	appointment was simply missed without loss dispute, the closed attempt is still marked
	Forfeited -- the money's gone either way once a re-payment is needed."""
	clearance_step_name = clearance_step_name or kwargs.get("name") or kwargs.get("clearance_step")
	if not reason:
		frappe.throw("A reason is required to forfeit and restart an Injaz attempt.", frappe.ValidationError)
	step = _require_taeshir_step(clearance_step_name)

	current = _get_active_injaz_attempt(step)
	prior_outcome = None
	if current is not None and current.outcome == "Active":
		# Forfeited = a Paid attempt whose fee is now lost; Missed = an unpaid appointment that
		# simply lapsed (nothing forfeited). (audit N-6)
		prior_outcome = "Forfeited" if current.payment_status == "Paid" else "Missed"
		current.outcome = prior_outcome
		current.remark = (current.remark + " | " if current.remark else "") + f"{prior_outcome}: {reason}"
	new_attempt = step.append(
		"injaz_attempts",
		{"outcome": "Active", "payment_status": "Unpaid", "appointment_date": new_appointment_date,
		 "injaz_application_id": new_injaz_application_id},
	)
	step.save(ignore_permissions=True)
	if prior_outcome == "Forfeited":
		# The paid fee is lost and the next attempt is paid again -- record this one now (#12).
		from agency_tracking.stage_fees import post_forfeited_injaz_fee

		post_forfeited_injaz_fee(step, current)
	log_action("Clearance Step", step.name, f"[{step.title or step.name}] Injaz {prior_outcome or 'restarted'} ({reason}); new attempt {new_injaz_application_id or '-'} on {new_appointment_date or '-'}")
	return step.as_dict()


def _fmt_date(value):
	return formatdate(value, "dd/MM/yyyy") if value else ""


def _upper(value):
	return str(value).upper() if value else ""


def _injaz_context(step, placement, applicant):
	"""Flatten a Clearance Step (+ its Placement + Applicant) into the Saudi Embassy Consular
	Section (easyenjaz) Injaz visa-application form. Identity/passport come from the Applicant,
	sponsor/visa/employer/duration from the Placement, and the Injaz/Enjaz application numbers
	(the two barcodes) from the step/placement. destination is hardcoded ("Kingdom of Saudi
	Arabia") since render_injaz_pdf only ever runs for a Saudi placement to begin with. Fields the
	system genuinely doesn't hold (arrival/payment/dependents, etc.) still render blank, exactly
	like the real form before the consulate fills them in -- 2026-09-07: business_address,
	duration_of_stay, dealer_name and destination used to be in that blank list too, but the data
	was already sitting on Placement (employer_address/employment_site, contract_duration,
	saudi_agency_name) and just wasn't wired in."""
	nationality = applicant.nationality or "Ethiopia"
	# Left barcode = the visa number (derived at the Injaz/Taeshir stage from the Application ID).
	# Right barcode = the Application ID (E-number) captured on the Taeshir step itself.
	visa_number = placement.visa_number or ""
	active_attempt = _get_active_injaz_attempt(step)
	application_id = (active_attempt.injaz_application_id if active_attempt else "") or ""

	return {
		# ── header ──
		"left_barcode": code128_b_datauri(visa_number),
		"left_barcode_number": visa_number,
		"right_barcode": code128_b_datauri(application_id),
		"right_barcode_number": application_id,
		"sponsor_name": _upper(placement.sponsor_name),
		# Downsized to roughly print-quality for its ~118x138px displayed size (see
		# embed_image_datauri) -- source photos are routinely multi-MB phone-camera originals.
		"photo_src": embed_image_datauri(applicant.photograph, max_dimension=450),
		"emblem_src": asset_datauri("templates", "injaz_assets", "mofa_emblem.png"),
		"agency_full": ORIGIN_AGENCY_FULL,
		"agency_email": get_agency_email(),
		# ── applicant ──
		"full_name": _upper(applicant.full_name),
		"date_of_birth": _fmt_date(applicant.date_of_birth),
		"place_of_birth": _upper(applicant.place_of_birth or applicant.city),
		"past_nationality": nationality,
		"current_nationality": nationality,
		"sex": applicant.gender or "",
		"marital_status": applicant.marital_status or "",
		"sect": "",
		"religion": _RELIGION_MAP.get(applicant.religion, applicant.religion) or "",
		"qualification": _upper(applicant.education),
		"profession": _upper(applicant.target_job) or "HOUSE WORKER",
		"home_address": _upper(applicant.address),
		"business_address": _upper(placement.employer_address or placement.employment_site),
		# ── travel / passport ──
		"purpose": "Work",
		"place_of_issue": _upper(applicant.passport_issue_place) or "ADDIS ABABA",
		"date_of_issue": _fmt_date(applicant.passport_issue_date),
		"passport_no": applicant.passport_number or "",
		"date_of_expiry": _fmt_date(applicant.passport_expiry_date),
		"duration_of_stay": placement.contract_duration or "",
		"date_of_arrival": "",
		"date_of_departure": "",
		"mode_of_payment": "",
		"payment_no": "",
		"payment_date": "",
		"relationship": "",
		"destination": "Kingdom of Saudi Arabia",
		"dealer_name": _upper(placement.saudi_agency_name),
		# ── certification / footer ──
		"cert_date": _fmt_date(today()),
		"cert_name": _upper(applicant.full_name),
		"footer_date": formatdate(today(), "EEEE, MMMM d, yyyy"),
		"page_label": "Page 1 of 1",
	}


def _build_injaz_pdf(clearance_step_name):
	"""Load the step + its Placement/Applicant, validate the Saudi-only corridor, render, and
	log the Access event. Returns (pdf_bytes, filename). Shared by render_injaz_pdf (streams the
	result straight to the caller) and the background-job handler (attaches it as a File)."""
	step = frappe.get_doc("Clearance Step", clearance_step_name)

	placement = frappe.get_doc("Placement", step.placement)
	if placement.destination_country != "Saudi Arabia":
		frappe.throw("Injaz applies only to Saudi Arabia placements.", frappe.ValidationError)
	applicant = frappe.get_doc("Applicant", placement.applicant)

	pdf_bytes = render_pdf(INJAZ_TEMPLATE, _injaz_context(step, placement, applicant))
	log_action("Clearance Step", step.name, f"[{step.title or step.name}] Injaz PDF downloaded for {applicant.full_name}", event_type="Access")
	return pdf_bytes, f"Injaz_{placement.applicant}.pdf"


@frappe.whitelist()
def render_injaz_pdf(clearance_step_name=None, **kwargs):
	"""Generate the Embassy of Saudi Arabia Injaz application PDF for a Saudi clearance step.
	Open to any internal staff (Taeshir, Embassy, LMIS, management, etc.) -- not just the Taeshir
	officer -- so the Embassy desk and other staff can pull the paper. Foreign agencies cannot.
	The document is assembled fresh from the step's latest Injaz attempt plus the linked
	Placement/Applicant -- it is not stored, it streams straight back as a download."""
	clearance_step_name = clearance_step_name or kwargs.get("name") or kwargs.get("step_name")
	if not clearance_step_name:
		frappe.throw("clearance_step_name is required.", frappe.ValidationError)

	if frappe.session.user != "Administrator" and not (INTERNAL_STAFF_ROLES & set(frappe.get_roles())):
		frappe.throw("Not permitted.", frappe.PermissionError)

	pdf_bytes, filename = _build_injaz_pdf(clearance_step_name)
	frappe.response["filename"] = filename
	frappe.response["filecontent"] = pdf_bytes
	frappe.response["type"] = "download"


@frappe.whitelist()
def enqueue_render_injaz_pdf(clearance_step_name=None, **kwargs):
	"""Async twin of render_injaz_pdf -- same param resolution and permission gate, but returns
	a Background Job reference immediately instead of blocking on the render. Poll
	background_jobs.get_job_status(job) for the result."""
	clearance_step_name = clearance_step_name or kwargs.get("name") or kwargs.get("step_name")
	if not clearance_step_name:
		frappe.throw("clearance_step_name is required.", frappe.ValidationError)
	if frappe.session.user != "Administrator" and not (INTERNAL_STAFF_ROLES & set(frappe.get_roles())):
		frappe.throw("Not permitted.", frappe.PermissionError)
	if not frappe.db.exists("Clearance Step", clearance_step_name):
		frappe.throw(f"Clearance Step {clearance_step_name} not found.", frappe.DoesNotExistError)

	from agency_tracking.background_jobs import enqueue_job

	job = enqueue_job(
		"Render Injaz PDF",
		reference_doctype="Clearance Step",
		reference_name=clearance_step_name,
		clearance_step_name=clearance_step_name,
	)
	return {"job": job, "status": "Queued"}
