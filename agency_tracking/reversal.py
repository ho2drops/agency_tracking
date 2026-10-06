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
		# ... except an agency, which may undo its own selection (checked against the placement below).
		if not (doctype == "Placement" and frappe.db.exists("Contractor", {"user": frappe.session.user})):
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
	if doctype == "Placement" and not event and doc.status == "Selected":
		return _run_unselect(doc, reason)
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
	return OVERSIGHT_ROLES.union({CONTRACT_PARSER, REGISTRAR}, *(rule.roles for rule in UNDO.values()))


def _run_unselect(placement, reason):
	if not _may_unselect(placement):
		frappe.throw("Only the agency that selected this candidate or a Manager can undo the selection.", frappe.PermissionError)
	save_point = f"sp_{frappe.generate_hash(length=10)}"
	with sanctioned_write():
		frappe.db.savepoint(save_point)
		try:
			_unselect(placement, reason)
		except Exception as exc:
			reraise_if_db_abort(exc)
			frappe.db.rollback(save_point=save_point)
			raise
	return {"doctype": "Placement", "name": placement.name, "status": "Cancelled"}


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


# --- Shared: taking money off the books ------------------------------------------------------


def _void_rows(filters, why):
	"""Void every Approved ledger row matching `filters`, each with a system note and its own
	"Voided" event. Nothing is edited or deleted. Returns the voided names."""
	names = frappe.get_all("Applicant Transaction", filters={**filters, "status": "Approved"}, pluck="name")
	for name in names:
		txn = frappe.get_doc("Applicant Transaction", name)
		txn.status = "Voided"
		txn.system_note = f"Voided by the undo of {why}."
		txn.save(ignore_permissions=True)
		frappe.get_doc(
			{
				"doctype": "Process Event",
				"reference_doctype": "Applicant Transaction",
				"reference_name": name,
				"event_type": "Voided",
				"from_status": "Approved",
				"to_status": "Voided",
				"actor": frappe.session.user,
				"remarks": f"Voided by the undo of {why}.",
			}
		).insert(ignore_permissions=True)
	return names


# --- Placement -------------------------------------------------------------------------------
# Selected -> Processing is undone by Manager / Admin (they assign the case). Processing ->
# Stamped by the clearance roles. Stamped -> Ticketed and Ticketed -> Departed by the Ticketer
# (REV-3). A selection itself is undone by "unselect", further down.

from agency_tracking.roles import CLEARANCE_COUNTRY_ROLES, CLEARANCE_OFFICER, CONTRACT_PARSER, REGISTRAR, TICKETER  # noqa: E402

CLEARANCE_ROLES = CLEARANCE_COUNTRY_ROLES | {CLEARANCE_OFFICER}


def _live_steps(placement_name):
	return frappe.get_all(
		"Clearance Step", {"placement": placement_name, "status": ["!=", "Cancelled"]}, ["name", "status", "wakala_status"]
	)


def _no_wakala_paid(placement, event):
	if any(step.wakala_status == "Paid" for step in _live_steps(placement.name)):
		frappe.throw(
			"A Wakala has already been paid on this case, so the move to Processing cannot be undone.",
			frappe.ValidationError,
		)


def _undo_processing(placement, event):
	"""The steps created for Processing are cancelled (kept as history), their tasks closed and
	any fee already recorded on them voided. Moving to Processing again creates fresh steps."""
	from agency_tracking.clearance_engine import close_open_todos
	from agency_tracking.state_machine import log_action

	steps = _live_steps(placement.name)
	names = [step.name for step in steps]
	for step in steps:
		frappe.db.set_value("Clearance Step", step.name, "status", "Cancelled")
		log_action("Clearance Step", step.name, f"Cancelled by the undo of the move to Processing (was {step.status})")
	close_open_todos("Clearance Step", names)
	if names:
		_void_rows({"clearance_step": ["in", names]}, "the move to Processing")


def _undo_stamped(placement, event):
	from agency_tracking.clearance_engine import _close_placement_todos

	_close_placement_todos(placement.name)  # "Book ticket ..."


TICKET_FIELDS = ("ticket_number", "flight_date", "ticket_cost", "is_rescheduled", "reschedule_date", "reschedule_cause", "reschedule_cost")


def _clear_ticket(placement, event):
	"""REV-6: undoing Ticketed takes the ticket with it. The details leave the placement and are
	kept on the Reversal event; the cost is voided in _undo_ticketed."""
	was = ", ".join(f"{f}: {placement.get(f)}" for f in TICKET_FIELDS if placement.get(f))
	for field in TICKET_FIELDS:
		if placement.meta.has_field(field):
			placement.set(field, None)
	placement.corridor_fees_logged = 0  # so booking again records a new cost
	return f"ticket undone ({was})" if was else None


def _undo_ticketed(placement, event):
	from agency_tracking.clearance_engine import _close_placement_todos, notify_ticketing_due

	_void_rows({"placement": placement.name, "fee_type": ["like", "Ticket%"]}, "the move to Ticketed")
	_close_placement_todos(placement.name)  # "Confirm departure ..."
	notify_ticketing_due(placement)  # "Book ticket ..." is open again


def _commission_rows(placement_name):
	return frappe.get_all(
		"Applicant Transaction",
		{"placement": placement_name, "transaction_type": "Commission", "status": "Approved"},
		pluck="name",
	)


def _departure_can_be_undone(placement, event):
	if frappe.db.exists("Complaint", {"placement": placement.name}):
		frappe.throw(
			"A complaint has been filed about this placement, so its departure cannot be undone.",
			frappe.ValidationError,
		)
	items = frappe.get_all(
		"Commission Batch Item",
		{"transaction": ["in", _commission_rows(placement.name) or [""]], "status": ["in", ["Pending", "Paid"]]},
		pluck="status",
	)
	if "Paid" in items:
		frappe.throw(
			"The commission for this placement is already paid. Unmark the payment first.", frappe.ValidationError
		)
	if items:
		frappe.throw(
			"The commission for this placement is on an invoice. Release it from the invoice first.",
			frappe.ValidationError,
		)


def _clear_departure(placement, event):
	was = placement.departed_on
	placement.departed_on = None
	return f"departure undone (was recorded {was})" if was else None


def _undo_departed(placement, event):
	from agency_tracking.clearance_engine import notify_departure_due

	_void_rows({"placement": placement.name, "transaction_type": "Commission"}, "the departure")
	# Closes whatever is open on the placement (a "no commission" task for Finance included) and
	# opens "Confirm departure ..." again.
	notify_departure_due(placement)


UNDO[("Placement", "Selected", "Processing")] = Undo(set(), check=_no_wakala_paid, effect=_undo_processing)
UNDO[("Placement", "Processing", "Stamped")] = Undo(CLEARANCE_ROLES, effect=_undo_stamped)
UNDO[("Placement", "Stamped", "Ticketed")] = Undo({TICKETER}, prepare=_clear_ticket, effect=_undo_ticketed)
UNDO[("Placement", "Ticketed", "Departed")] = Undo(
	{TICKETER}, check=_departure_can_be_undone, prepare=_clear_departure, effect=_undo_departed
)


# --- Unselect (REV-2) ------------------------------------------------------------------------
# Selecting a candidate is not a move: it creates the placement at Selected, so there is no
# event to undo. While the placement is still at Selected (never moved on, or moved back), the
# selection itself can be undone: the placement is closed and the candidate is free again. The
# applicant is not cancelled, unlike Cancel Applicant.


def _own_agency_placement(placement):
	contractor = frappe.db.get_value("Contractor", {"user": frappe.session.user}, "name")
	return bool(contractor) and contractor == placement.contractor


def _may_unselect(placement):
	roles = set(frappe.get_roles())
	if frappe.session.user == "Administrator" or OVERSIGHT_ROLES & roles:
		return True
	if _own_agency_placement(placement):
		return True
	track = frappe.db.get_value("Applicant", placement.applicant, "entry_track")
	return track == "Muayena" and bool({CONTRACT_PARSER, REGISTRAR} & roles)


def _unselect(placement, reason):
	from agency_tracking.clearance_engine import close_open_todos

	placement.status = "Cancelled"
	placement.save(ignore_permissions=True)
	frappe.get_doc(
		{
			"doctype": "Process Event",
			"reference_doctype": "Placement",
			"reference_name": placement.name,
			"event_type": "Reversal",
			"from_status": "Selected",
			"to_status": "Cancelled",
			"actor": frappe.session.user,
			"remarks": " | ".join(part for part in (reason, "selection undone: the candidate is available again") if part),
		}
	).insert(ignore_permissions=True)
	close_open_todos("Placement", placement.name)
	if frappe.db.get_value("Applicant", placement.applicant, "active_placement") == placement.name:
		frappe.db.set_value("Applicant", placement.applicant, "active_placement", None)


# --- Applicant -------------------------------------------------------------------------------
# Draft -> Registered and Registered -> CV Generated are undone by the Registrar (and, for the
# CV, the CV role). A cancel is undone by the Registrar too (REV-4) and brings the case back with
# it. A restart is undone back to Cancelled. A track change (-> Draft) is not undone here: the
# event does not keep the old track, and putting only the status back would leave the two at odds.

from agency_tracking.roles import CV  # noqa: E402

APPLICANT_ROLES = {REGISTRAR}


def _not_selected(applicant, event):
	if applicant.active_placement:
		frappe.throw(
			"This applicant has been selected by an agency. Undo the selection first.", frappe.ValidationError
		)


def _supersede_cv(applicant, event):
	"""The CV record of the undone step is kept, marked superseded. Generating again makes a new one."""
	record = frappe.db.get_value(
		"CV Record", {"applicant": applicant.name, "docstatus": 1, "superseded": 0}, "name", order_by="creation desc"
	)
	if record:
		frappe.db.set_value("CV Record", record, "superseded", 1)


def _nothing_on_this_cycle(applicant, event):
	for doctype, what in (("Placement", "a selection"), ("CV Record", "a CV")):
		filters = {"applicant": applicant.name, "cycle_number": applicant.cycle_number}
		if doctype == "CV Record":
			filters["superseded"] = 0
		else:
			filters["status"] = ["!=", "Cancelled"]
		if frappe.db.exists(doctype, filters):
			frappe.throw(f"The restarted applicant already has {what}. Undo that first.", frappe.ValidationError)


def _step_back_a_cycle(applicant, event):
	if (applicant.cycle_number or 1) > 1:
		applicant.cycle_number = applicant.cycle_number - 1
		return f"cycle number back to {applicant.cycle_number}"


def _placement_cancelled_with(applicant, event):
	"""The placement the cancel cascade closed together with this applicant, with the event of
	that closing, or (None, None). Same cycle only; a placement closed by an unselect has no such
	event and is never picked."""
	for name in frappe.get_all(
		"Placement",
		{"applicant": applicant.name, "status": "Cancelled", "cycle_number": applicant.cycle_number},
		pluck="name",
		order_by="modified desc",
	):
		closing = latest_forward_event("Placement", name)
		if closing and closing.to_status == "Cancelled" and closing.creation <= event.creation:
			return frappe.get_doc("Placement", name), closing
	return None, None


def _status_before_the_cascade(step_name):
	"""A step's status before it was cancelled with its case, or None when its latest history
	line is something else (e.g. it was cancelled by an undone move to Processing)."""
	import re

	from agency_tracking.applicant_api import STEP_CANCELLED_WITH_CASE

	last = frappe.get_all(
		"Process Event",
		{"reference_doctype": "Clearance Step", "reference_name": step_name},
		["from_status", "remarks"],
		order_by="creation desc, name desc",
		limit=1,
	)
	if not last or not (last[0].remarks or "").startswith(STEP_CANCELLED_WITH_CASE):
		return None
	if last[0].from_status:
		return last[0].from_status
	match = re.search(r"\(was ([^)]+)\)", last[0].remarks)  # cancels recorded before from_status was kept
	return match.group(1) if match else None


def _undo_cancel(applicant, event, reason):
	"""REV-4: the applicant goes back to where it was, and the case closed with it comes back as
	it was: the placement at its stage, every step at its status, tasks open again."""
	from agency_tracking.clearance_engine import _notify_step_officers, notify_departure_due, notify_ticketing_due
	from agency_tracking.state_machine import CLEARANCE_STEP_DONE_STATUSES, log_action

	placement, closing = _placement_cancelled_with(applicant, event)
	note = "cancel undone"
	# The applicant first: a placement only saves while its applicant is at the right stage.
	applicant.status = event.from_status
	if placement:
		applicant.active_placement = placement.name
		note = "cancel undone: the case came back with it"
	applicant.save(ignore_permissions=True)
	if placement:
		placement.status = closing.from_status
		placement.save(ignore_permissions=True)
		_write_reversal_event(placement, closing, "Cancelled", closing.from_status, reason, "restored with its applicant")
		for step in frappe.get_all("Clearance Step", {"placement": placement.name, "status": "Cancelled"}, pluck="name"):
			before = _status_before_the_cascade(step)
			if not before:
				continue
			frappe.db.set_value("Clearance Step", step, "status", before)
			log_action("Clearance Step", step, f"Restored with the case (back to {before})", from_status="Cancelled", to_status=before)
			if before not in CLEARANCE_STEP_DONE_STATUSES:
				_notify_step_officers(frappe.get_doc("Clearance Step", step))
		if placement.status == "Stamped":
			notify_ticketing_due(placement)
		elif placement.status == "Ticketed":
			notify_departure_due(placement)
	return "Cancelled", event.from_status, note


UNDO[("Applicant", "Draft", "Registered")] = Undo(APPLICANT_ROLES, check=_not_selected)
UNDO[("Applicant", "Registered", "CV Generated")] = Undo(APPLICANT_ROLES | {CV}, check=_not_selected, effect=_supersede_cv)
for _before in ("Registered", "CV Generated"):
	UNDO[("Applicant", _before, "Cancelled")] = Undo(APPLICANT_ROLES, apply=_undo_cancel)
for _target in ("Draft", "Registered"):
	UNDO[("Applicant", "Cancelled", _target)] = Undo(APPLICANT_ROLES, check=_nothing_on_this_cycle, prepare=_step_back_a_cycle)


# --- What the screens ask -------------------------------------------------------------------


@frappe.whitelist()
def get_undoable_step(doctype=None, name=None, **kwargs):
	"""What "Undo last step" would do on this record for the caller, without doing it:
	{"available", "from_status", "to_status", "blocked"}.

	available -- there is a step to undo and the caller may undo it.
	from_status / to_status -- where the record is and where the undo would put it.
	blocked -- when the undo is refused right now: the message that says what to do first."""
	name = name or kwargs.get("docname")
	nothing = {"available": False, "from_status": None, "to_status": None, "blocked": None}
	if not doctype or not name or doctype not in _doctypes() or not frappe.db.exists(doctype, name):
		return nothing
	doc = frappe.get_doc(doctype, name)
	if not doc.has_permission("read") and not (doctype == "Placement" and _own_agency_placement(doc)):
		return nothing
	event = latest_forward_event(doctype, name)
	if doctype == "Placement" and not event and doc.status == "Selected":
		return {**nothing, "available": _may_unselect(doc), "from_status": "Selected", "to_status": "Cancelled"}
	rule = UNDO.get((doctype, event.from_status, event.to_status)) if event else None
	if not rule or event.to_status != doc.status or not _may_undo(rule):
		return nothing
	to_status = {"Applicant Transaction": {"Approved": "Voided"}}.get(doctype, {}).get(doc.status, event.from_status)
	if doctype == "Applicant Transaction" and doc.status in ("Rejected", "Voided"):
		to_status = doc.status  # the row stays; the amount is entered again as Pending
	blocked = None
	if rule.check:
		try:
			rule.check(doc, event)
		except frappe.ValidationError as exc:
			frappe.clear_last_message()
			blocked = str(exc)
	return {"available": True, "from_status": doc.status, "to_status": to_status, "blocked": blocked}
