# Copyright (c) 2026, Agency and contributors
# License: MIT. See LICENSE
#
# Undo: the latest move of a record can be taken back, one step at a time (design qa/10, user
# decisions REV-1..7 of 2026-10-05).
#
# - What can be undone is the latest forward event (a Transition or an Override written by
#   state_machine.transition) that no Reversal points at yet. Undoing twice goes back two steps.
# - Who: the role responsible for that move, or a Manager / Admin. No time limit. A reason may be
#   given and is recorded; it is not required.
# - The move's effects are undone in the same savepoint as the status change: all of it or none.
# - Money is never edited or deleted. A row is voided with a note, and where the earlier state
#   needs the amount back it is entered again as a new Pending row for Finance to approve.
# - Every undo writes a "Reversal" Process Event that names the event it undoes. The original
#   event is never touched. Going forward again is an ordinary transition(): every gate runs again.
#
# transition() itself is not changed by any of this: an undo is its own, separate write.

import frappe

from agency_tracking.db_errors import reraise_if_db_abort
from agency_tracking.state_machine import lock_doc_row, sanctioned_write

FORWARD_EVENT_TYPES = ("Transition", "Override")
# Besides the role responsible for a move, these may undo every move (REV-7).
OVERSIGHT_ROLES = {"Manager", "Admin", "System Manager"}


class Undo:
	"""How one forward move (doctype, from_status, to_status) is taken back.

	roles    -- who may undo it besides OVERSIGHT_ROLES.
	check    -- check(doc, event): raise ValidationError, saying what to do first, when the undo
	            must not happen now. Changes nothing.
	prepare  -- prepare(doc, event) -> str or None: change fields on doc before it is saved at its
	            earlier status; the returned text goes onto the Reversal event.
	effect   -- effect(doc, event): undo what the forward move caused (runs after the save).
	apply    -- apply(doc, event, reason) -> (from_status, to_status, note): replaces the plain
	            "set the earlier status and save" for moves that are not undone by a status
	            change (ledger rows are voided and re-entered, never set back).
	"""

	def __init__(self, roles, check=None, prepare=None, effect=None, apply=None):
		self.roles = set(roles)
		self.check = check
		self.prepare = prepare
		self.effect = effect
		self.apply = apply


# (doctype, from_status, to_status) of the forward move -> Undo. Filled in below, per record type.
UNDO = {}


def _doctypes():
	return {doctype for doctype, _, _ in UNDO}


def latest_forward_event(doctype, name):
	"""The newest Transition / Override of this record that no Reversal has undone, or None."""
	undone = frappe.get_all(
		"Process Event",
		filters={"reference_doctype": doctype, "reference_name": name, "event_type": "Reversal"},
		pluck="reverses_event",
	)
	events = frappe.get_all(
		"Process Event",
		filters={
			"reference_doctype": doctype,
			"reference_name": name,
			"event_type": ["in", FORWARD_EVENT_TYPES],
			"name": ["not in", undone or [""]],
		},
		fields=["name", "event_type", "from_status", "to_status", "actor", "creation"],
		order_by="creation desc, name desc",
		limit=1,
	)
	return events[0] if events else None


def _may_undo(rule):
	roles = set(frappe.get_roles())
	return frappe.session.user == "Administrator" or bool((rule.roles | OVERSIGHT_ROLES) & roles)


def _write_reversal_event(doc, event, from_status, to_status, reason, note):
	remarks = " | ".join(part for part in (reason, note) if part) or None
	frappe.get_doc(
		{
			"doctype": "Process Event",
			"reference_doctype": doc.doctype,
			"reference_name": doc.name,
			"event_type": "Reversal",
			"from_status": from_status,
			"to_status": to_status,
			"reverses_event": event.name,
			"actor": frappe.session.user,
			"remarks": remarks,
		}
	).insert(ignore_permissions=True)


@frappe.whitelist()
def reverse_last_step(doctype=None, name=None, reason=None, **kwargs):
	"""Undo the latest move of one record. Returns {"doctype", "name", "status"}."""
	# Someone who can undo nothing at all is refused before anything else is looked at.
	if frappe.session.user != "Administrator" and not (_all_undo_roles() & set(frappe.get_roles())):
		frappe.throw("Not permitted.", frappe.PermissionError)
	name = name or kwargs.get("docname")
	if not doctype or not name:
		frappe.throw("doctype and name are required.", frappe.ValidationError)
	if doctype not in _doctypes():
		frappe.throw("Steps of this kind of record cannot be undone.", frappe.ValidationError)
	if not frappe.db.exists(doctype, name):
		frappe.throw("Record not found.", frappe.DoesNotExistError)

	lock_doc_row(doctype, name)  # two undos, or an undo and a forward move, go one after the other
	doc = frappe.get_doc(doctype, name)
	event = latest_forward_event(doctype, name)
	rule = UNDO.get((doctype, event.from_status, event.to_status)) if event else None
	# Permission before anything about the record's state is revealed.
	if not _may_undo(rule or Undo(roles=_roles_for(doctype))):
		who = ", ".join(sorted((rule.roles if rule else _roles_for(doctype)))) or "the responsible role"
		frappe.throw(f"Only {who} or a Manager can undo this step.", frappe.PermissionError)
	if not event:
		frappe.throw("Nothing to undo: this record has not moved yet.", frappe.ValidationError)
	if event.to_status != doc.status:
		frappe.throw(
			"This record's history and its status disagree, so the last step cannot be undone. Ask an Admin.",
			frappe.ValidationError,
		)
	if not rule:
		frappe.throw("This step cannot be undone.", frappe.ValidationError)
	if rule.check:
		rule.check(doc, event)

	save_point = f"sp_{frappe.generate_hash(length=10)}"
	with sanctioned_write():
		frappe.db.savepoint(save_point)
		try:
			if rule.apply:
				from_status, to_status, note = rule.apply(doc, event, reason)
			else:
				from_status, to_status = doc.status, event.from_status
				note = rule.prepare(doc, event) if rule.prepare else None
				doc.status = to_status
				doc.save(ignore_permissions=True)
			_write_reversal_event(doc, event, from_status, to_status, reason, note)
			if rule.effect:
				rule.effect(doc, event)
		except Exception as exc:
			# After a DB abort the savepoint no longer exists (QA B-b1).
			reraise_if_db_abort(exc)
			frappe.db.rollback(save_point=save_point)
			raise
	return {"doctype": doc.doctype, "name": doc.name, "status": frappe.db.get_value(doc.doctype, doc.name, "status")}


def _all_undo_roles():
	"""Every role that may undo anything."""
	return OVERSIGHT_ROLES.union(*(rule.roles for rule in UNDO.values()))


def _roles_for(doctype):
	"""Every role that may undo some move of this record type (for the refusal message and for
	refusing an outsider before saying anything about the record)."""
	roles = set()
	for (dt, _, _), rule in UNDO.items():
		if dt == doctype:
			roles |= rule.roles
	return roles


# --- Complaint -------------------------------------------------------------------------------

COMPLAINT_ROLES = {"Complaint Manager"}
COMPLAINT_OUTCOME_FIELDS = ("resolution_notes", "resolved_by", "resolved_on")


def _clear_complaint_outcome(complaint, event):
	"""The outcome's details leave the complaint and are kept on the Reversal event."""
	was = ", ".join(f"{f}: {complaint.get(f)}" for f in COMPLAINT_OUTCOME_FIELDS if complaint.get(f))
	for field in COMPLAINT_OUTCOME_FIELDS:
		complaint.set(field, None)
	return f"outcome undone ({was})" if was else None


def _no_replacement_selected(complaint, event):
	replacement = frappe.db.exists(
		"Placement", {"free_replacement_for_complaint": complaint.name, "status": ["!=", "Cancelled"]}
	)
	if replacement:
		frappe.throw(
			"A free replacement has already been selected for this complaint. Undo that selection first.",
			frappe.ValidationError,
		)


UNDO[("Complaint", "New", "Unresolved")] = Undo(COMPLAINT_ROLES)
for _outcome in ("Resolved", "Escalated", "Dismissed"):
	UNDO[("Complaint", "Unresolved", _outcome)] = Undo(COMPLAINT_ROLES, prepare=_clear_complaint_outcome)
UNDO[("Complaint", "Unresolved", "Returned - Free Replacement Required")] = Undo(
	COMPLAINT_ROLES, check=_no_replacement_selected, prepare=_clear_complaint_outcome
)


# --- Applicant Transaction (ledger rows) -----------------------------------------------------
# A ledger row is never set back. Undoing an approval voids the row; undoing an approval, a
# rejection or a void enters the same amount again as a new Pending row that Finance approves
# like any other (REV-5). The copy keeps the original's exchange-rate fields, so an undo never
# re-prices money.

LEDGER_ROLES = {"Finance Manager"}
LEDGER_COPIED_FIELDS = (
	"applicant", "placement", "cycle_number", "transaction_type", "stage_logged_at", "logged_by",
	"amount_original", "currency_original", "fx_rate", "fx_rate_date", "amount_birr", "awaiting_fx_rate",
	"description", "clearance_step", "fee_type", "injaz_attempt", "receipt_image",
)


def _not_on_an_invoice(txn, event):
	if frappe.db.exists("Commission Batch Item", {"transaction": txn.name, "status": ["in", ["Pending", "Paid"]]}):
		frappe.throw("This row is on an invoice. Release it from the invoice first.", frappe.ValidationError)


def _not_a_write_off(txn, event):
	if frappe.db.exists("Commission Batch Write Off", {"transaction": txn.name}):
		frappe.throw(
			"This row is an invoice write-off. Record the write-off again on the invoice instead.",
			frappe.ValidationError,
		)


def _enter_again(txn, why):
	"""A new Pending row with the same figures as `txn`, noted as re-entered by an undo."""
	copy = frappe.get_doc(
		{
			"doctype": "Applicant Transaction",
			"status": "Pending",
			"system_note": f"Entered again by the undo of {why} (original row kept, see its history).",
			**{field: txn.get(field) for field in LEDGER_COPIED_FIELDS},
		}
	).insert(ignore_permissions=True)
	return copy.name


def _undo_approval(txn, event, reason):
	_enter_again(txn, "an approval")
	txn.status = "Voided"
	txn.system_note = "Voided by the undo of its approval; entered again as a new Pending row."
	txn.save(ignore_permissions=True)
	return "Approved", "Voided", "approval undone: row voided and entered again as Pending"


def _undo_rejection(txn, event, reason):
	_enter_again(txn, "a rejection")
	return "Rejected", "Rejected", "rejection undone: entered again as a new Pending row"


def _undo_void(txn, event, reason):
	_enter_again(txn, "a void")
	return "Voided", "Voided", "void undone: entered again as a new Pending row"


UNDO[("Applicant Transaction", "Pending", "Approved")] = Undo(LEDGER_ROLES, check=_not_on_an_invoice, apply=_undo_approval)
UNDO[("Applicant Transaction", "Pending", "Rejected")] = Undo(LEDGER_ROLES, apply=_undo_rejection)
UNDO[("Applicant Transaction", "Approved", "Voided")] = Undo(LEDGER_ROLES, check=_not_a_write_off, apply=_undo_void)
