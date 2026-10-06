# Copyright (c) 2026, Agency and contributors
# License: MIT. See LICENSE
#
# Per-stage automatic fees (client item #12, 2026-09-23). Each Corridor Definition.known_fees row
# names the clearance step_type whose completion records it. When that step completes, the fee is
# recorded as its own auto-Approved Expense Applicant Transaction -- same auto-approval as the
# ticket-cost entry, since it's a system-computed known amount, not a discretionary staff entry.
#
# Replaces the 2026-09-19 behavior of summing every known fee into the ticketing entry, which
# recorded nothing until ticketing (a placement cancelled after LMIS/Taeshir never recorded those
# costs) and lumped every fee into one ledger line.
#
# Idempotency key per ledger row: (clearance_step, fee_type, injaz_attempt). Injaz is charged per
# paid attempt: a paid attempt forfeited by a missed appointment is charged when it's forfeited
# (clearance_api.forfeit_injaz_and_restart), the final attempt when the step completes; a
# reschedule keeps the same attempt, so it never adds a charge.

import frappe
from frappe.utils import flt

from agency_tracking.db_errors import reraise_if_db_abort
from agency_tracking.state_machine import CLEARANCE_STEP_DONE_STATUSES, sanctioned_write

INJAZ_FEE_TYPE = "Injaz Payment"
CLOSED_INJAZ_OUTCOMES = ("Forfeited", "Missed")


def _placement_for_fees(placement_name):
	"""The placement, or None when per-step fees must not be recorded for it: no placement, or
	its fees were already recorded in the old combined ticketing entry (legacy_lump_fees)."""
	if not placement_name:
		return None
	placement = frappe.db.get_value(
		"Placement", placement_name, ["name", "applicant", "destination_country", "legacy_lump_fees"], as_dict=True
	)
	if not placement or placement.legacy_lump_fees:
		return None
	return placement


def _fee_rows(destination_country, step_type, fee_type=None):
	corridor = frappe.db.get_value("Corridor Definition", {"destination_country": destination_country}, "name")
	if not corridor:
		return []
	filters = {"parent": corridor, "parenttype": "Corridor Definition", "step_type": step_type}
	if fee_type:
		filters["fee_type"] = fee_type
	return [
		row
		for row in frappe.get_all("Corridor Known Fee", filters=filters, fields=["fee_type", "amount", "currency"])
		if flt(row.amount)
	]


def final_injaz_attempt(step):
	"""The attempt the step was completed on: the last one not closed as Forfeited/Missed."""
	open_attempts = [a for a in (step.get("injaz_attempts") or []) if a.outcome not in CLOSED_INJAZ_OUTCOMES]
	return open_attempts[-1] if open_attempts else None


def _record_fee(step, placement, row, injaz_attempt=None):
	"""Insert one auto-Approved Expense for `row` unless this (step, fee, attempt) already has
	one -- in any status, so a row Finance deliberately voided is never silently re-created.
	(Only a row voided by the undo of the step itself is set aside, see below.)
	If the fee's currency has never had an FX rate, it's still recorded -- in its own currency,
	awaiting_fx_rate=1, amount_birr 0 -- and converted when a rate is recorded
	(finance_engine.convert_awaiting_fx). Any other failure is logged and rolled back to a
	savepoint, never blocking the step itself."""
	attempt_name = injaz_attempt.name if injaz_attempt else None
	key = {
		"clearance_step": step.name,
		"fee_type": row.fee_type,
		"injaz_attempt": attempt_name or ["is", "not set"],
	}
	# A fee voided by the undo of its step does not count: finishing the step again records it again.
	from agency_tracking.reversal import VOIDED_BY_UNDO

	existing = frappe.get_all("Applicant Transaction", filters=key, fields=["status", "system_note"])
	if any(not (r.status == "Voided" and (r.system_note or "").startswith(VOIDED_BY_UNDO)) for r in existing):
		return None

	from decimal import Decimal

	from agency_tracking.finance_engine import birr_fields

	# "sp_" prefix: a bare hex hash like "234e0744ce" parses as a number and MariaDB rejects it as a
	# SAVEPOINT name (found live 2026-09-19).
	save_point = f"sp_{frappe.generate_hash(length=10)}"
	try:
		frappe.db.savepoint(save_point)
		amount = Decimal(str(row.amount))
		attempt_note = f", Injaz attempt {injaz_attempt.injaz_application_id or attempt_name}" if injaz_attempt else ""
		with sanctioned_write():  # system-created Approved ledger row (QA A1)
			txn = frappe.get_doc(
				{
					"doctype": "Applicant Transaction",
					"applicant": placement.applicant,
					"placement": placement.name,
					"transaction_type": "Expense",
					"amount_original": amount,
					"currency_original": row.currency,
					**birr_fields(amount, row.currency, wait_for_rate=True),
					"description": f"{row.fee_type} -- {step.step_type} [{step.name}]{attempt_note} for {placement.name}",
					"stage_logged_at": step.step_type,
					"clearance_step": step.name,
					"fee_type": row.fee_type,
					"injaz_attempt": attempt_name,
					"logged_by": frappe.session.user,
					# Known, configured amount -- auto-Approved exactly like the ticket-cost entry.
					"status": "Approved",
				}
			).insert(ignore_permissions=True)
		return txn.name
	except Exception as exc:
		# Before the savepoint rollback: after a DB abort the savepoint no longer exists (QA B-b1).
		reraise_if_db_abort(exc)
		frappe.db.rollback(save_point=save_point)
		frappe.log_error(
			title="Stage fee recording failed",
			message=f"{step.name} / {row.fee_type}: {frappe.get_traceback()}",
		)
		return None


def post_step_fees(step):
	"""Record every configured fee for a completed step. No-op unless the step is done."""
	if step.status not in CLEARANCE_STEP_DONE_STATUSES:
		return []
	placement = _placement_for_fees(step.placement)
	if not placement:
		return []
	posted = []
	for row in _fee_rows(placement.destination_country, step.step_type):
		attempt = final_injaz_attempt(step) if row.fee_type == INJAZ_FEE_TYPE else None
		name = _record_fee(step, placement, row, injaz_attempt=attempt)
		if name:
			posted.append(name)
	return posted


def post_forfeited_injaz_fee(step, attempt):
	"""A paid Injaz attempt lost to a missed appointment: its fee was spent, record it now."""
	placement = _placement_for_fees(step.placement)
	if not placement:
		return []
	posted = []
	for row in _fee_rows(placement.destination_country, step.step_type, fee_type=INJAZ_FEE_TYPE):
		name = _record_fee(step, placement, row, injaz_attempt=attempt)
		if name:
			posted.append(name)
	return posted


def post_missing_stage_fees(placement_name):
	"""Record any fee still missing for this placement's completed steps, including forfeited
	paid Injaz attempts. Idempotent. Only used by the one-time deploy catch-up
	(patches/stage_fees_setup.py) -- deliberately NOT called at ticketing, where it would charge
	the current amount for a fee that was 0 when its step completed."""
	if not _placement_for_fees(placement_name):
		return []
	posted = []
	for step_name in frappe.get_all("Clearance Step", filters={"placement": placement_name}, pluck="name"):
		step = frappe.get_doc("Clearance Step", step_name)
		for attempt in step.get("injaz_attempts") or []:
			if attempt.outcome == "Forfeited":
				posted += post_forfeited_injaz_fee(step, attempt)
		posted += post_step_fees(step)
	return posted
