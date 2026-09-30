# Copyright (c) 2026, Agency and contributors
# License: MIT. See LICENSE
#
# Part C of master-build-specification.md: two enforcement points.
#   validate()   — is the data allowed to exist in this state? (lives on each doctype)
#   transition() — is this process move allowed right now? (lives here, shared)
#
# Built starting Step 1 (not deferred to Step 6 as Part I's sequence might suggest) because
# CLAUDE.md's rule is absolute: no doc.status = X; doc.save() anywhere, ever. Only the contents
# of ALLOWED_TRANSITIONS and STAGE_GATES grow as later build steps add doctypes/stages — the
# function itself never changes shape. See BUILD_LOG.md "Standing decisions".
import traceback
from contextlib import contextmanager

import frappe
from frappe.utils import get_datetime, getdate, today

from agency_tracking.db_errors import reraise_if_db_abort


def log_action(reference_doctype, reference_name, remarks, event_type="Action", actor=None):
	"""Append a Process Event for a non-transition action (write-off, Injaz payment, contractor
	edit, PDF download, ...) so the audit trail answers "who did what, when" for actions that don't
	go through transition(). event_type is "Action" for a state-affecting op or "Access" for a
	read/download. Best-effort: never raise into the caller (the action already happened)."""
	try:
		frappe.get_doc(
			{
				"doctype": "Process Event",
				"reference_doctype": reference_doctype,
				"reference_name": reference_name,
				"event_type": event_type,
				"actor": actor or frappe.session.user,
				"remarks": remarks,
			}
		).insert(ignore_permissions=True)
	except Exception as exc:
		reraise_if_db_abort(exc)
		frappe.log_error(title="log_action failed", message=f"{reference_doctype} {reference_name}: {remarks}")


def lock_applicant_row(applicant_name):
	"""Row-level lock (SELECT ... FOR UPDATE), held until the current request's transaction
	commits. Used anywhere two concurrent requests could both read active_placement as empty
	before either writes it — portal selection (Step 3) and Muayena direct-entry (Step 4).

	Returns the CURRENT active_placement value, read via this same locking query. Callers must
	use this return value for their check, not a separate plain frappe.db.get_value() afterward
	(2026-09-11 fix, found live): under MySQL's REPEATABLE READ, a locking read (FOR UPDATE)
	always sees the latest committed data, but a plain SELECT in an already-open transaction can
	still be bound to a snapshot fixed by an earlier plain read elsewhere in the same request
	(e.g. loading the Applicant doc before calling this) — so a plain re-check after the lock can
	silently return stale data even though the row itself is genuinely locked and fresh."""
	rows = frappe.db.sql(
		"SELECT `active_placement` FROM `tabApplicant` WHERE `name`=%s FOR UPDATE", applicant_name
	)
	return rows[0][0] if rows else None


def lock_doc_row(doctype, name):
	"""Generic row-level lock (SELECT ... FOR UPDATE), held until the current request's
	transaction commits -- same primitive as lock_applicant_row above, generalized (2026-09-12)
	for any doctype where two concurrent requests could both read-then-write the same row off a
	stale snapshot. First user: Commission Batch Request write-offs/batch creation, where an
	unlocked check-then-act could over-credit a batch or double-invoice the same commission into
	two batches (finance_engine.py). Call this BEFORE any plain read of the same row in the same
	request -- a locking read always sees latest-committed data and, as the first statement
	touching this row, fixes the transaction's snapshot for any plain read that follows it."""
	frappe.db.sql(f"SELECT `name` FROM `tab{doctype}` WHERE `name`=%s FOR UPDATE", name)


# Fields that describe *where a record sits in its lifecycle* or *which record it is* -- never
# something a document parser is allowed to write. Parsing (passport MRZ, contract, visa, injaz)
# is strictly informational: it may auto-fill data fields and attach the file, but the ONLY
# sanctioned way to move a record's stage is transition() (invoked from an explicit button/RPC
# like register_applicant / advance_placement). Consumers of parsed data
# (placement_api.upload_contract/upload_visa, Applicant.autofill_from_passport) run it through
# strip_lifecycle_fields() before applying, so even if a parser regex someday captured a stray
# "status"/"docstatus" token, it can never silently advance or mutate the lifecycle.
LIFECYCLE_FIELDS = frozenset({
	"status",
	"applicant_state",
	"docstatus",
	"name",
	"active_placement",
	"departed_on",
	"entry_track",
})


def strip_lifecycle_fields(data):
	"""Return a copy of a parsed-data dict with any lifecycle/identity keys removed. No-op for
	falsy input. Keeps document parsing informational-only (see LIFECYCLE_FIELDS)."""
	if not data:
		return data
	return {k: v for k, v in data.items() if k not in LIFECYCLE_FIELDS}


# --- Terminal-state guards (2026-08-31, cc2 QA pass findings NEW-1 / NEW-3) ---
# A class of endpoints record an outcome via a plain doc.save() rather than transition() (they
# don't change Placement.status themselves -- record_ticket_details, record_reschedule, the
# medical-result recorders, and every Clearance Step action below), so ALLOWED_TRANSITIONS/
# STAGE_GATES never see them and never got a chance to block a write once the parent record is
# already in a terminal state. Confirmed live: a Ticketer could silently rewrite ticket_number
# on an already-Departed Placement, and a Kuwait Embassy user could flip an already-Stamped step
# on an already-Departed Placement back to Rejected -- both with no audit trail (no Process
# Event, since neither goes through transition()). These two shared guards are the single place
# every such action now checks before writing, rather than patching each endpoint in isolation.

TERMINAL_PLACEMENT_STATUSES = {"Departed", "Cancelled"}


def assert_placement_not_terminal(placement):
	"""Guards every Placement-mutating action that records an outcome without itself being a
	transition() call (record_ticket_details, record_reschedule,
	record_selected_medical_result, record_predeparture_medical_result). Once Departed/Cancelled,
	ticketing/medical data is a historical record, not something a routine action should still
	be able to silently overwrite."""
	if placement.status in TERMINAL_PLACEMENT_STATUSES:
		frappe.throw(
			f"{placement.name} is already {placement.status} (terminal) -- this can no longer "
			"be edited through this action.",
			frappe.ValidationError,
		)


def assert_clearance_step_not_terminal(step):
	"""Guards every Clearance-Step-mutating action (start/complete_clearance_step,
	submit/stamp/reject_embassy_step, reassign_clearance_step) against editing a step whose
	parent Placement is already Departed/Cancelled -- that history is final.

	2026-09-11 (product decision): a step reaching its OWN terminal outcome (Issued/Complete/
	Stamped/Rejected) no longer blocks further edits through these same actions -- LMIS, Taeshir/
	Injaz, Embassy, and reassignment all need to stay correctable (fix a wrong reference number,
	amount, or officer) after the fact, without that correction reopening or reversing anything
	already downstream of it (auto-advance, etc. are idempotent and unaffected by a data-only
	correction). Each caller still guards its OWN nonsensical transitions where relevant (e.g.
	reject_embassy_step still refuses a step that was never Submitted)."""
	placement_status = frappe.db.get_value("Placement", step.placement, "status")
	if placement_status in TERMINAL_PLACEMENT_STATUSES:
		frappe.throw(
			f"{step.placement} is already {placement_status} -- its Clearance Steps can no "
			"longer be edited.",
			frappe.ValidationError,
		)


# doctype -> set of (from_status, to_status) edges that are legal to attempt.
# Extended as each build step introduces new statuses (Placement's Selected/Processing/
# Stamped/Ticketed/Departed land here from Step 3 onward).
ALLOWED_TRANSITIONS = {
	"Applicant": {
		("Draft", "Registered"),
		("Registered", "CV Generated"),
		("Registered", "Cancelled"),
		("CV Generated", "Cancelled"),
		# entry_track-forced regression (applicant_api.update_applicant) and Cancelled->restart
		# (applicant_api.restart_applicant) both land here -- Draft->Cancelled is deliberately
		# NOT an edge (Cancelled only applies once something is committed, Registered onward).
		("Registered", "Draft"),
		("CV Generated", "Draft"),
		("Cancelled", "Draft"),
		("Cancelled", "Registered"),
	},
	"Placement": {
		("Selected", "Processing"),
		("Processing", "Stamped"),
		("Stamped", "Ticketed"),
		("Ticketed", "Departed"),
		# Cancellable from any pre-Departed stage (applicant_api.cancel_applicant cascades
		# here) -- Departed stays terminal/uncancellable.
		("Selected", "Cancelled"),
		("Processing", "Cancelled"),
		("Stamped", "Cancelled"),
		("Ticketed", "Cancelled"),
	},
	"Applicant Transaction": {
		("Pending", "Approved"),
		("Pending", "Rejected"),
		("Approved", "Voided"),
	},
	"Complaint": {
		("New", "Unresolved"),
		("Unresolved", "Resolved"),
		("Unresolved", "Returned - Free Replacement Required"),
		("Unresolved", "Escalated"),
		("Unresolved", "Dismissed"),
	},
}

# (from_status, to_status) -> callable(doc) -> bool. Applicant's Draft->Registered move has no
# cross-doctype gate (just the field-floor/medical check already in Applicant.validate()).
# Registered->CV Generated is gated on cv_generation_gate (Standard track only, Step 2).
# Placement's Ticketed->Departed is gated on medical_2_gate (Step 6). Processing->Stamped is
# gated on all_mandatory_clearance_steps_complete (Step 7, below). Selected->Processing and
# Stamped->Ticketed have no gate — nothing to check against for either.
STAGE_GATES = {}


class NoOverride(str):
	"""A gate's refusal that even a Manager override can't force through (e.g. medically UNFIT
	-> CV Generated, user decision 2026-09-23). Return NoOverride("reason") instead of a plain str."""


# (doctype, to_status) -> callable(doc). Runs once, after a transition has already committed
# (doc.save() + Process Event logged) — orchestration, not validation; a side effect here
# can't block the move itself (that's what STAGE_GATES is for). Keeps transition() the single
# place that drives cross-doctype consequences (Part C: "triggers reopen_for_reprocessing()...
# triggers commission accrual on reaching Departed") instead of scattering them across callers.
TRANSITION_SIDE_EFFECTS = {}

# (doctype, to_status) whose side effect is part of the move itself, not best-effort: if it
# raises, the move is undone and the caller gets the error (QA P4-04 -- a Processing placement
# must never exist with only some of its clearance steps).
REQUIRED_SIDE_EFFECTS = set()


# --- Out-of-band write guard (QA A1: S-01, S-02, P4-02, P4-03, P5-07) ---
# Role permissions still grant write on these doctypes (unchanged on purpose), so generic REST /
# frappe.client.set_value / frappe.client.save / frappe.client.insert could set `status` directly,
# skipping transition()'s topology, gates, audit event and side effects -- or insert a ledger row
# already Approved, or edit an Approved amount in place. The doctypes' validate() now refuses
# those writes unless they come from the app's own guarded code, which marks itself with
# sanctioned_write(): transition() always, plus the few direct paths listed where they happen
# (clearance step actions, system-created Approved ledger rows, the ticket-cost correction).
# frappe.db.set_value never runs validate() and is unaffected.


@contextmanager
def sanctioned_write():
	"""Marks writes made inside as coming from a guarded app code path (see above)."""
	frappe.flags.sanctioned_write = (frappe.flags.sanctioned_write or 0) + 1
	try:
		yield
	finally:
		frappe.flags.sanctioned_write -= 1


def _write_is_sanctioned():
	f = frappe.flags
	return bool(f.sanctioned_write or f.in_install or f.in_migrate or f.in_patch or f.in_import or f.in_fixtures)


def guard_status_write(doc, initial_statuses):
	"""Call from validate(). Outside a sanctioned path, a new doc may only start at one of
	`initial_statuses`, and an existing doc's status may not change."""
	if _write_is_sanctioned():
		return
	if doc.is_new():
		if doc.status and doc.status not in initial_statuses:
			frappe.throw(
				f"A new {doc.doctype} starts at {' / '.join(initial_statuses)}; use the app's actions to move it on.",
				frappe.ValidationError,
			)
		return
	before = doc.get_doc_before_save()
	if before is not None and before.status != doc.status:
		frappe.throw(
			f"{doc.doctype} status can only be changed through the app's actions, not edited directly "
			f"({before.status} -> {doc.status}).",
			frappe.ValidationError,
		)


def guard_locked_fields(doc, locked_statuses, fieldnames):
	"""Call from validate(). Outside a sanctioned path, `fieldnames` may not change while the
	stored row is in one of `locked_statuses` (e.g. an Approved ledger amount)."""
	if _write_is_sanctioned() or doc.is_new():
		return
	before = doc.get_doc_before_save()
	if before is None or before.status not in locked_statuses:
		return
	# validate() runs before Frappe casts incoming values, so a REST save still carries e.g. a date as
	# "2026-09-25" -- compare both sides cast to the field's type, or an untouched field reads as changed.
	changed = [f for f in fieldnames if doc.cast(before.get(f), doc.meta.get_field(f)) != doc.cast(doc.get(f), doc.meta.get_field(f))]
	if changed:
		frappe.throw(
			f"An {before.status} {doc.doctype} can't be edited ({', '.join(changed)}); void it and record a new one.",
			frappe.ValidationError,
		)


def transition(doc, new_status, actor=None, override=False, override_reason=None, remarks=None, ignore_permissions=False):
	"""The only sanctioned status-change path -- see _transition. Everything it writes (the doc,
	its Process Event, its side effects) counts as a sanctioned write."""
	with sanctioned_write():
		return _transition(doc, new_status, actor=actor, override=override, override_reason=override_reason,
			remarks=remarks, ignore_permissions=ignore_permissions)


def _transition(doc, new_status, actor=None, override=False, override_reason=None, remarks=None, ignore_permissions=False):
	"""The only sanctioned status-change path. Validates the move is a legal edge for this
	doctype, runs any registered gate, commits the change (which re-triggers the doctype's
	own validate() against the new status), logs a Process Event, and returns the saved doc.

	Manager Override (business-workflow-srs.md: "can override a blocked step... always with a
	written reason"): if a gate blocks the move, override=True + a non-empty override_reason
	lets a Manager/Admin force it through anyway. Override only ever bypasses a *gate* — the
	ALLOWED_TRANSITIONS topology itself is never overridable; there's no business case in the
	spec for skipping an entire lifecycle stage, only for forcing past a blocked condition
	within an otherwise-legal move.

	remarks: recorded on the Process Event same as override_reason, but for plain (non-gated)
	transitions that still want a reason on the audit trail -- e.g. cancel_applicant's written
	cancellation reason, which isn't an override of anything.

	ignore_permissions: for a transition the *system* decides to make as a consequence of some
	other action (see auto_advance_placement_if_ready) rather than one the caller's own session
	is personally allowed to make on the doc directly -- e.g. a Taeshir officer completing their
	step shouldn't need Placement-write permission themselves just because that happened to be
	the last mandatory step. Every existing caller keeps the default (permission-checked) behavior.
	"""
	current_status = doc.status
	allowed = ALLOWED_TRANSITIONS.get(doc.doctype, set())
	if (current_status, new_status) not in allowed:
		frappe.throw(
			"Cannot move {0} {1} from '{2}' to '{3}'.".format(
				doc.doctype, doc.name, current_status, new_status
			),
			frappe.ValidationError,
		)

	gate = STAGE_GATES.get((current_status, new_status))
	gate_result = gate(doc) if gate else True
	gate_passed = gate_result is True
	is_override = bool(gate) and not gate_passed

	if is_override:
		if not override or isinstance(gate_result, NoOverride):
			# Gate functions may return a specific reason string instead of a bare False
			# (mirrors validate_field_floor's specific field list) -- fall back to the gate
			# function's own name/docstring when it doesn't, so the message is never fully
			# generic even for older gates that haven't been updated to return a reason.
			reason = gate_result if isinstance(gate_result, str) and gate_result else (
				gate.__doc__.strip().splitlines()[0] if gate.__doc__ else gate.__name__
			)
			frappe.throw(
				"'{0}' -> '{1}' is blocked: {2}".format(current_status, new_status, reason),
				frappe.ValidationError,
			)
		if not ({"Manager", "Admin"} & set(frappe.get_roles())):
			frappe.throw("Only Manager or Admin can override a blocked transition.", frappe.PermissionError)
		if not override_reason:
			frappe.throw("A written reason is required to override this gate.", frappe.ValidationError)

	actor = actor or frappe.session.user
	side_effect = TRANSITION_SIDE_EFFECTS.get((doc.doctype, new_status))
	required = side_effect if (doc.doctype, new_status) in REQUIRED_SIDE_EFFECTS else None
	# "sp_" prefix: MariaDB rejects a savepoint name that parses as a number (see stage_fees).
	save_point = f"sp_{frappe.generate_hash(length=10)}"
	frappe.db.savepoint(save_point)
	try:
		doc.status = new_status
		doc.save(ignore_permissions=ignore_permissions)

		frappe.get_doc(
			{
				"doctype": "Process Event",
				"reference_doctype": doc.doctype,
				"reference_name": doc.name,
				"event_type": "Override" if is_override else "Transition",
				"from_status": current_status,
				"to_status": new_status,
				"actor": actor,
				"remarks": override_reason if is_override else remarks,
			}
		).insert(ignore_permissions=True)
		if required:
			required(doc, current_status)
	except Exception as exc:
		# After a DB abort the savepoint no longer exists (QA B-b1).
		reraise_if_db_abort(exc)
		frappe.db.rollback(save_point=save_point)
		doc.status = current_status
		raise

	if side_effect and not required:
		try:
			side_effect(doc, current_status)
		except Exception as exc:
			# A DB abort is not a side-effect failure: the transition itself was rolled back
			# with it, so it must reach the caller (QA B-b1).
			reraise_if_db_abort(exc)
			# Side effects run after the transition has already committed (doc.save() +
			# Process Event, both above). Letting an exception here propagate would make
			# transition() look like it failed to the caller while the status change actually
			# went through — corrupting the "only sanctioned path" guarantee (a caller who
			# catches the exception and retries, or assumes nothing happened, would be wrong).
			# Side effects are best-effort automation layered on top of a transition that has
			# already legitimately happened; only STAGE_GATES may block a transition itself.
			# Real failures (e.g. commission accrual missing a configured rate) are logged for
			# staff to notice and resolve manually, not silently lost.
			frappe.logger().error(f"Transition side effect failed: {doc.doctype} {doc.name}: {current_status} -> {new_status}\n{traceback.format_exc()}")
			frappe.log_error(
				title="Transition side effect failed",
				message=f"{doc.doctype} {doc.name}: {current_status} -> {new_status}\n\n{traceback.format_exc()}",
			)

	return doc


def cv_generation_gate(applicant):
	"""Registered -> CV Generated (Part A.2 Stage 3): Standard track only.

	2026-08-29: the Musaned gate (blocking CV generation for Saudi-bound Standard candidates
	until musaned_status == ALTEYAZECHEM) and the musaned_status field itself have both been
	removed per direct instruction -- Musaned tracking is no longer part of this system at all.
	"""
	if applicant.entry_track != "Standard":
		return f"only Standard-track applicants generate a CV (this applicant is {applicant.entry_track})."
	# 2026-09-23: a medically UNFIT applicant must never reach the agency portal, and CV Generated
	# is what puts them there. cv_api.generate_cv refuses first with a clearer message; this is
	# the backstop for any other path into CV Generated.
	if applicant.medical_status == "UNFIT":
		return NoOverride("medically UNFIT applicants cannot have a CV generated.")
	return True


STAGE_GATES[("Registered", "CV Generated")] = cv_generation_gate


# --- Medical 2 gate (Part A.2 Stage 8 / Step 6) ---
# The pre-departure check (~72h before flight) is separate from the earlier registration-time
# FIT check — a candidate can pass the first medical, get all the way to Ticketed, and still
# fail this one. "If this fails, the flight is cancelled and departure is blocked."


def medical_2_gate(placement):
	if placement.medical_2_status == "FIT":
		return True
	return (
		f"pre-departure medical (Medical 2) status is '{placement.medical_2_status}', "
		"must be FIT. Record it via placement_api.record_predeparture_medical_result."
	)


def passport_valid_for_departure(placement):
	"""Audit G-016: a passport valid at registration can lapse before departure (only medical was
	re-checked this late). Block Departed if the applicant's passport has expired."""
	expiry = frappe.db.get_value("Applicant", placement.applicant, "passport_expiry_date")
	if expiry and get_datetime(expiry).date() < frappe.utils.getdate():
		return f"passport expired on {expiry}; it must be renewed before departure."
	return True


def departure_gate(placement):
	"""Combined Ticketed -> Departed gate: pre-departure medical FIT AND a non-expired passport."""
	medical = medical_2_gate(placement)
	if medical is not True:
		return medical
	return passport_valid_for_departure(placement)


STAGE_GATES[("Ticketed", "Departed")] = departure_gate


# --- Post-contract medical gate (2026-08-29, new) ---
# A fresh checkpoint right after contract upload/Placement creation, distinct from both the
# Applicant's earlier registration-time FIT check and the pre-departure Medical 2 check above.
# UNFIT here doesn't just block the gate -- it cancels the whole Applicant + Placement (see
# applicant_api.cancel_applicant, called by placement_api.record_selected_medical_result).
# Applies uniformly to Standard (Saudi/Kuwait) and Muayena.


def medical_selected_gate(placement):
	"""2026-09-23 product decision: a registration-time FIT (Applicant.medical_status) that hasn't
	expired yet (medical_expiry_date on or after today) counts for this check too -- a new exam
	is only needed when that one is missing, expired, undated or not FIT."""
	if placement.medical_selected_status == "FIT":
		return True
	applicant = frappe.db.get_value(
		"Applicant", placement.applicant, ["medical_status", "medical_expiry_date"], as_dict=True
	)
	if (
		applicant
		and applicant.medical_status == "FIT"
		and applicant.medical_expiry_date
		and getdate(applicant.medical_expiry_date) >= getdate(today())
	):
		return True
	return (
		f"medical (Selected stage) status is '{placement.medical_selected_status}', must be FIT, and the "
		"registration medical isn't a usable substitute (not FIT, no expiry date, or expired). "
		"Record it via placement_api.record_selected_medical_result."
	)


STAGE_GATES[("Selected", "Processing")] = medical_selected_gate


# --- All-mandatory-clearance-steps-complete gate (Part A.2 Stage 6 / Step 7) ---
# "Stamped — all mandatory corridor steps issued." Optional steps (is_mandatory=0) don't block.


CLEARANCE_STEP_DONE_STATUSES = {"Complete", "Issued", "Stamped"}


def all_mandatory_clearance_steps_complete(placement):
	"""Checked against the destination's corridor, not just the rows that exist: a mandatory
	corridor step with no Clearance Step row blocks too (QA P4-04)."""
	from agency_tracking.corridor_engine import get_corridor_steps

	steps = frappe.get_all(
		"Clearance Step",
		filters={"placement": placement.name},
		fields=["step_type", "status", "is_mandatory"],
	)
	mandatory = [s for s in steps if s.is_mandatory]
	if not mandatory:
		return "no mandatory Clearance Steps exist yet for this Placement."
	pending = [f"{s.step_type} ({s.status})" for s in mandatory if s.status not in CLEARANCE_STEP_DONE_STATUSES]
	existing = {s.step_type for s in steps}
	pending += [
		f"{c['step_type']} (missing)"
		for c in get_corridor_steps(placement.destination_country)
		if c["is_mandatory"] and c["step_type"] not in existing
	]
	if pending:
		return "mandatory Clearance Steps not yet complete: " + ", ".join(pending)
	return True


STAGE_GATES[("Processing", "Stamped")] = all_mandatory_clearance_steps_complete


# --- Auto-advance Processing -> Stamped (2026-09-10) ---
# Nothing used to notice when the *last* mandatory Clearance Step finished -- a Placement could
# have LMIS Issued, Taeshir Complete, and Embassy Stamped and still sit in Processing forever,
# because advancing it was a fully separate manual call nobody was prompted to make (the cause
# of the PLM-00016 confusion: Embassy alone reaching Stamped looked like progress, but the
# Placement's own status never moved). Call this after ANY mandatory Clearance Step reaches its
# own done status, regardless of which step type it is or what order they finish in -- it's a
# no-op unless this really was the last piece.
# Process Event.actor is a mandatory Link to User -- there's no "system user" record to point
# at, so "Administrator" (guaranteed to exist on every site) is the actor of record, with
# remarks distinguishing this from an actual Administrator click.
AUTO_ADVANCE_ACTOR = "Administrator"
AUTO_ADVANCE_REMARKS = "Auto-advanced: all mandatory Clearance Steps are complete."


def auto_advance_placement_if_ready(placement_name):
	"""Best-effort: never raises into the caller. The Clearance Step that triggered this call
	already saved successfully -- a failure here should be logged for a Manager to advance by
	hand, not unwind real work that already happened.

	If it doesn't advance now, it checks once more after this request commits: two last steps
	saved at the same moment each read the other as still open on their own snapshot, so
	neither advanced (QA test_44). The later commit's re-check sees both done."""
	if not _advance_if_ready(placement_name):
		frappe.db.after_commit.add(lambda: _recheck_after_commit(placement_name))


def _recheck_after_commit(placement_name):
	"""Runs in a fresh transaction. The Placement row lock comes first so two re-checks run one
	after the other and the second sees the first's Stamped (no double advance)."""
	try:
		frappe.db.get_value("Placement", placement_name, "status", for_update=True)
		_advance_if_ready(placement_name)
		frappe.db.commit()
	except Exception:
		frappe.db.rollback()
		frappe.log_error(
			title="Auto-advance re-check after commit failed",
			message=f"{placement_name}: {frappe.get_traceback()}",
		)
		frappe.db.commit()  # the request already committed; keep the log


def _advance_if_ready(placement_name):
	"""Processing -> Stamped when every mandatory step is done. True when the placement is past
	Processing (advanced now or already), False otherwise."""
	try:
		placement = frappe.get_doc("Placement", placement_name)
		if placement.status != "Processing":
			return True
		if all_mandatory_clearance_steps_complete(placement) is not True:
			return False
		# ignore_permissions=True: the officer who happened to complete the last mandatory step
		# (e.g. Taeshir) isn't necessarily who Placement-write permission would be checked
		# against -- this is a system-driven consequence of their action, not their own edit.
		transition(
			placement, "Stamped", actor=AUTO_ADVANCE_ACTOR, remarks=AUTO_ADVANCE_REMARKS, ignore_permissions=True
		)
		return True
	except Exception as exc:
		reraise_if_db_abort(exc)
		frappe.log_error(
			title="Auto-advance Processing->Stamped failed",
			message=f"{placement_name}: {frappe.get_traceback()}",
		)
		return True  # logged for a Manager to advance by hand; re-checking would only repeat it


# --- Ticket-recorded gate (2026-08-30, backend-issues #05) ---
# Nothing previously gated "Ticketed" on ticket data actually existing -- a placement could
# reach Ticketed with ticket_number/flight_date both still null. Require a ticket_number so
# "Ticketed" reliably means a ticket was recorded (placement_api.record_ticket_details).


def ticket_recorded_gate(placement):
	if placement.ticket_number:
		return True
	return "no ticket_number recorded yet. Call placement_api.record_ticket_details first."


# 2026-09-12: a mandatory Clearance Step can now be reopened after the fact
# (clearance_api.reopen_clearance_step) when its own terminal outcome was wrong, not just its
# data -- e.g. an LMIS officer un-Issuing a step they marked complete by mistake. Nothing
# previously re-checked clearance completeness once a Placement had already auto-advanced past
# Processing, so a reopened step (silently back to Pending/In Progress) would NOT stop Ticketing
# from proceeding on a corridor that's no longer actually fully cleared. Chained onto
# ticket_recorded_gate rather than replacing it, mirroring departure_gate's own two-checks
# pattern below.
def stamped_to_ticketed_gate(placement):
	ticket = ticket_recorded_gate(placement)
	if ticket is not True:
		return ticket
	return all_mandatory_clearance_steps_complete(placement)


STAGE_GATES[("Stamped", "Ticketed")] = stamped_to_ticketed_gate


# --- Free-replacement window gate (Part A.4 / Step 10) ---
# "A 3-month window from departure, during which a returned worker triggers a free replacement
# obligation." Measured from Placement.departed_on (stamped once, on first entry to Departed —
# see Placement.stamp_departed_on), not the complaint's own creation date.

FREE_REPLACEMENT_WINDOW_DAYS = 90


def within_free_replacement_window(complaint):
	placement = frappe.get_doc("Placement", complaint.placement)
	if not placement.departed_on:
		return f"{placement.name} has no departed_on date recorded (never reached Departed)."

	departed_on = get_datetime(placement.departed_on) if isinstance(placement.departed_on, str) else placement.departed_on
	days_since_departure = (frappe.utils.now_datetime() - departed_on).days
	if days_since_departure <= FREE_REPLACEMENT_WINDOW_DAYS:
		return True
	return (
		f"{days_since_departure} days have passed since departure, "
		f"outside the {FREE_REPLACEMENT_WINDOW_DAYS}-day free-replacement window."
	)


STAGE_GATES[("Unresolved", "Returned - Free Replacement Required")] = within_free_replacement_window


# --- Applicant cycle_number bump (2026-08-29 lifecycle spec) ---
# "Increments if and only if the status transition lands specifically on Draft or Registered,
# coming from an already-completed state (Registered, CV Generated, or Cancelled)." Covers both
# trigger paths uniformly -- entry_track-forced regression (applicant_api.update_applicant) and
# Cancelled->restart (applicant_api.restart_applicant) -- since both just call transition() and
# land here. A plain edit that never changes status never touches this at all.

CYCLE_BUMP_FROM_STATUSES = {"Registered", "CV Generated", "Cancelled"}


def bump_cycle_number(applicant, from_status):
	if from_status in CYCLE_BUMP_FROM_STATUSES:
		# Runs after transition()'s doc.save(), so also sync the in-memory doc -- callers return
		# applicant.as_dict() (restart_applicant, update_applicant), which otherwise reported the
		# pre-bump cycle_number. update_modified=False keeps doc.modified matching the row, so a
		# later save of this same doc object in the request doesn't hit a TimestampMismatchError.
		new_cycle = (applicant.cycle_number or 1) + 1
		frappe.db.set_value("Applicant", applicant.name, "cycle_number", new_cycle, update_modified=False)
		applicant.cycle_number = new_cycle


TRANSITION_SIDE_EFFECTS[("Applicant", "Draft")] = bump_cycle_number
TRANSITION_SIDE_EFFECTS[("Applicant", "Registered")] = bump_cycle_number
