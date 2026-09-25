# Copyright (c) 2026, Agency and contributors
# License: MIT. See LICENSE
#
# Part F: module-scoped whitelisted functions, no raw /api/resource/* exposure.

import frappe

from agency_tracking.pagination import count_rows, page_args, paged_result
from agency_tracking.contract_parser import _parse_contract, _parse_visa
from agency_tracking.state_machine import (
	assert_placement_not_terminal,
	lock_applicant_row,
	log_action,
	strip_lifecycle_fields,
	transition,
)


# The staff who own parsed contract/visa data and may correct it by hand when a parse misses or
# mis-reads a field (Contract Parser is the dedicated role; the usual management fallback too).
PARSE_EDIT_ROLES = {"Contract Parser", "Manager", "Admin", "System Manager"}

# Who may record a medical result (post-Selected and pre-departure): the dedicated Medical Officer
# role plus the existing Placement writers (management + back-office). 2026-09-05.
MEDICAL_RECORD_ROLES = {"Medical Officer", "Manager", "Admin", "System Manager", "Contract Parser", "Ticketer"}

# Only the parsed contract/visa identifier fields are hand-editable here — never lifecycle/status
# fields (those move via transition() only), never the applicant/contractor links. visa_number is
# the one the Injaz paper's left barcode is built from; the rest travel with it.
PARSED_EDITABLE_FIELDS = (
	"visa_number",
	"visa_type",
	"visa_issue_date",
	"visa_expiry_date",
	"visa_reference_number",
	"contract_number",
	"contract_signed_date",
	"employer_name",
	"employer_national_id",
	"employer_address",
	"saudi_agency_name",
	"saudi_agency_license",
	"kuwait_agency_name",
	"kuwait_agency_license",
	"employment_site",
	"contract_duration",
	"contract_salary_amount",
	"contract_salary_currency",
	"sponsor_name",
	"sponsor_civil_id",
)


def _linked_contractor_or_staff_write(placement):
	"""Keyed off an actual linked Contractor record, not role membership — the special
	Administrator user carries every role in the system, so a role-membership check alone
	can't tell "logged in as an agency" from "logged in as staff". Contract Parser is the
	dedicated staff role for this (2026-08-29) -- has_permission already covers it via the
	doctype-level write grant added to Placement."""
	linked_contractor = frappe.db.get_value("Contractor", {"user": frappe.session.user}, "name")
	if linked_contractor:
		if linked_contractor != placement.contractor:
			frappe.throw("Not permitted.", frappe.PermissionError)
	elif not placement.has_permission("write"):
		frappe.throw("Not permitted.", frappe.PermissionError)


@frappe.whitelist()
def upload_contract(placement_name=None, file_url=None, **kwargs):
	"""Standard track (Part I Step 4): attach the signed contract to an already-selected
	Placement (created by portal_api.select_candidate in Step 3) and extract contract_signed_date
	plus, per destination_country, the structured fields contract_parser.parse_contract_file
	knows how to pull out (Saudi: contract#/visa#/employer/agency; Kuwait: employer/site/
	duration/salary only -- its template carries far less). Either the contractor who made the
	selection, or internal staff (Contract Parser and the general fallback roles), may upload."""
	if not placement_name:
		frappe.throw("placement_name is required.", frappe.ValidationError)
	if not file_url:
		frappe.throw("file_url is required.", frappe.ValidationError)
	placement = frappe.get_doc("Placement", placement_name)
	_linked_contractor_or_staff_write(placement)

	# Parsing is informational only: attach the file + fill parsed data fields, but never let
	# it move the Placement's stage (strip_lifecycle_fields). Stage moves go through
	# advance_placement()/transition() only. See state_machine.LIFECYCLE_FIELDS.
	extracted = strip_lifecycle_fields(_parse_contract(file_url, placement.destination_country))
	placement.contract_file = file_url
	placement.update(extracted)
	placement.save(ignore_permissions=True)
	return placement.as_dict()


@frappe.whitelist()
def upload_visa(placement_name=None, file_url=None, **kwargs):
	"""Kuwait only: a separate document from the contract, uploaded alongside it. Carries
	visa_number/type/dates plus the agency name/license the Kuwait contract itself never has.
	Cross-checks the parsed agency identity against this Placement's actual Contractor and
	flags a mismatch (notify, never auto-reassigns)."""
	if not placement_name:
		frappe.throw("placement_name is required.", frappe.ValidationError)
	if not file_url:
		frappe.throw("file_url is required.", frappe.ValidationError)
	placement = frappe.get_doc("Placement", placement_name)
	if placement.destination_country != "Kuwait":
		frappe.throw("Visa upload is only applicable to Kuwait placements.", frappe.ValidationError)
	_linked_contractor_or_staff_write(placement)

	# Informational-only, same as upload_contract: never let parsed data advance the stage.
	extracted = strip_lifecycle_fields(_parse_visa(file_url))
	placement.visa_file = file_url
	placement.update(extracted)
	placement.save(ignore_permissions=True)

	parsed_agency_name = extracted.get("kuwait_agency_name")
	if parsed_agency_name:
		actual_agency_name = frappe.db.get_value("Contractor", placement.contractor, "contractor_name")
		if actual_agency_name and parsed_agency_name.strip().lower() != actual_agency_name.strip().lower():
			from agency_tracking.notification_engine import notify

			# Manager only -- Admin isn't push-notified for routine alerts (2026-09-05).
			for user in frappe.get_all("Has Role", filters={"role": "Manager"}, pluck="parent"):
				notify(
					user,
					"kuwait_visa_agency_mismatch",
					{
						"placement": placement_name,
						"visa_agency_name": parsed_agency_name,
						"contractor_name": actual_agency_name,
					},
				)

	return placement.as_dict()


@frappe.whitelist()
def create_muayena_placement(applicant_name=None, contractor_name=None, file_url=None, **kwargs):
	"""Muayena track (Part A.1 / Part I Step 4): "enters directly at Selected with contract in
	hand" — no portal, no CV. Internal staff (Registrar/Manager/Admin/Contract Parser) only —
	a Muayena candidate is matched to an agency directly, not through the public portal.

	2026-08-29 correction: destination_country is no longer a parameter here. It used to be
	set as a side effect of this call (the old assumption was that Muayena's destination
	becomes known only once a contract names it); that was wrong — it's selected during
	Draft/Registered same as Standard, and is now part of MUAYENA_REGISTERED_REQUIRED_FIELDS.
	The contractor is always picked manually for Muayena (both countries) — Saudi's contract
	*can* carry a labeled agency name/license for cross-checking, but auto-assignment isn't
	attempted at creation time either way; Kuwait's contract never carries one at all.
	"""
	if not applicant_name:
		frappe.throw("applicant_name is required.", frappe.ValidationError)
	if not contractor_name:
		frappe.throw("contractor_name is required.", frappe.ValidationError)
	applicant = frappe.get_doc("Applicant", applicant_name)
	if not applicant.has_permission("write"):
		frappe.throw("Not permitted.", frappe.PermissionError)

	if applicant.entry_track != "Muayena":
		frappe.throw(
			"Only Muayena-track candidates enter directly via contract upload; "
			"Standard-track candidates go through the portal (Step 3).",
			frappe.ValidationError,
		)
	if applicant.status != "Registered":
		frappe.throw(
			f"{applicant_name} must be Registered before a Placement can be created "
			f"(currently '{applicant.status}').",
			frappe.ValidationError,
		)
	if not applicant.destination_country:
		frappe.throw(f"{applicant_name} has no destination_country set.", frappe.ValidationError)

	# Parse BEFORE taking the row lock, not after (2026-09-11 fix): real contract text
	# extraction can be slow, and it never touches active_placement, so it must not hold this
	# lock -- confirmed live that a held SELECT ... FOR UPDATE blocks even a plain, unrelated
	# doc.save() on the same Applicant row until the holder's transaction commits. A slow parse
	# here was silently turning into "editing THIS ONE applicant hangs/fails" for anyone else,
	# while every other applicant worked fine. select_candidate (portal_api.py) already gets
	# this right -- lock held only across the fast check-and-insert, nothing slow in between.
	#
	# Commit here (nothing but reads happened above -- harmless) to close out this transaction
	# before the slow parse, so the read above can never pin a stale REPEATABLE READ snapshot
	# across it. Without this, a concurrent request that commits an active_placement change
	# during the parse window causes the *locking* re-read below to hit a hard MySQL error
	# (ER_CHECKREAD, "Record has changed since last read") instead of the clean "already has an
	# active Placement" rejection it's supposed to fail with -- confirmed live.
	frappe.db.commit()
	extracted = _parse_contract(file_url, applicant.destination_country) if file_url else {}

	current_lock = lock_applicant_row(applicant_name)
	if current_lock:
		frappe.throw(f"{applicant_name} already has an active Placement.", frappe.ValidationError)

	placement = frappe.get_doc(
		{
			"doctype": "Placement",
			"applicant": applicant_name,
			"contractor": contractor_name,
			"destination_country": applicant.destination_country,
			"status": "Selected",
			"contract_file": file_url,
			**extracted,
		}
	).insert(ignore_permissions=True)

	frappe.db.set_value("Applicant", applicant_name, "active_placement", placement.name)
	return placement.as_dict()


@frappe.whitelist()
def record_selected_medical_result(placement_name=None, status=None, examination_date=None, expiry_date=None, **kwargs):
	"""New post-contract medical checkpoint (2026-08-29): gates Selected -> Processing (see
	state_machine.medical_selected_gate). FIT just records the result; UNFIT cancels the whole
	Applicant + Placement via the same cascade as applicant_api.cancel_applicant, uniformly
	for every track/country -- nothing forward from here."""
	if not placement_name:
		frappe.throw("placement_name is required.", frappe.ValidationError)
	if status not in ("FIT", "UNFIT"):
		frappe.throw("status must be 'FIT' or 'UNFIT'.", frappe.ValidationError)
	placement = frappe.get_doc("Placement", placement_name)
	if not (MEDICAL_RECORD_ROLES & set(frappe.get_roles())):
		frappe.throw("Not permitted.", frappe.PermissionError)
	assert_placement_not_terminal(placement)

	placement.medical_selected_status = status
	placement.medical_selected_examination_date = examination_date
	placement.medical_selected_expiry_date = expiry_date
	placement.save(ignore_permissions=True)

	if status == "UNFIT":
		from agency_tracking.applicant_api import cancel_applicant

		cancel_applicant(placement.applicant, "Medical (Selected stage) result: UNFIT.")

	return placement.as_dict()


@frappe.whitelist()
def record_predeparture_medical_result(placement_name=None, status=None, examination_date=None, **kwargs):
	"""Pre-departure medical checkpoint (~72h before flight, Part A.2 Stage 8 / Step 6): gates
	Ticketed -> Departed (see state_machine.medical_2_gate). Mirrors
	record_selected_medical_result's shape -- FIT just records the result and lets
	advance_placement(new_status="Departed") pass the gate; UNFIT cancels the whole Applicant +
	Placement via the same cascade, since a failed pre-departure medical this late (ticket
	already purchased) has no forward path either, same as the earlier Selected-stage check."""
	if not placement_name:
		frappe.throw("placement_name is required.", frappe.ValidationError)
	if status not in ("FIT", "UNFIT"):
		frappe.throw("status must be 'FIT' or 'UNFIT'.", frappe.ValidationError)
	placement = frappe.get_doc("Placement", placement_name)
	if not (MEDICAL_RECORD_ROLES & set(frappe.get_roles())):
		frappe.throw("Not permitted.", frappe.PermissionError)
	assert_placement_not_terminal(placement)

	placement.medical_2_status = status
	placement.medical_2_examination_date = examination_date
	placement.save(ignore_permissions=True)

	if status == "UNFIT":
		from agency_tracking.applicant_api import cancel_applicant

		cancel_applicant(placement.applicant, "Medical (pre-departure) result: UNFIT.")

	return placement.as_dict()


@frappe.whitelist()
def advance_placement(placement_name=None, new_status=None, override_reason=None, **kwargs):
	"""Move a Placement forward through its lifecycle via the sanctioned transition() path
	(Part C). Passing override_reason attempts a Manager Override if the move is gate-blocked
	(business-workflow-srs.md: "always with a written reason") — transition() itself enforces
	the Manager/Admin role check and that the reason is non-empty.

	This is the direct/manual path -- Processing -> Stamped can also happen on its own via
	state_machine.auto_advance_placement_if_ready() once every mandatory Clearance Step is
	done, so a caller here may find the placement already at new_status by the time they ask
	(idempotent no-op below), not just already-in-that-state from a repeated click.
	"""
	placement_name = placement_name or kwargs.get("placement") or kwargs.get("name")
	new_status = new_status or kwargs.get("status") or kwargs.get("target_status")
	if not placement_name or not new_status:
		frappe.throw("Both placement_name and new_status are required.", frappe.ValidationError)

	if not frappe.db.exists("Placement", placement_name):
		frappe.throw(f"Placement {placement_name} not found.", frappe.DoesNotExistError)
	placement = frappe.get_doc("Placement", placement_name)
	# S-1 (2026-09-05): lifecycle advance is management, OR the officer actually assigned to THIS
	# placement's current stage (holds an open ToDo on it) -- not every role that happens to have
	# Placement write (which included Ticketer/Contract Parser, letting them advance any placement's
	# whole lifecycle). The gates still enforce the stage conditions on top of this.
	from agency_tracking.finance_engine import is_assigned_to_placement

	if not (
		frappe.session.user == "Administrator"
		or ({"Manager", "Admin", "System Manager"} & set(frappe.get_roles()))
		or is_assigned_to_placement(frappe.session.user, placement_name)
	):
		frappe.throw("Not permitted.", frappe.PermissionError)

	if placement.status == new_status:
		return placement.as_dict()

	return transition(
		placement, new_status, override=bool(override_reason), override_reason=override_reason
	).as_dict()


TICKET_FEE_TYPE = "Ticket"
RESCHEDULE_TICKET_FEE_TYPE = "Ticket (Internal Reschedule)"


def _require_etb(currency, what):
	"""Ticket and reschedule costs are always paid in Birr (2026-09-23 product decision)."""
	if currency and currency != "ETB":
		frappe.throw(f"{what} is always in ETB (got {currency}).", frappe.ValidationError)


def _record_ticket_expense(placement, amount, fee_type, description):
	"""One auto-Approved ETB Expense for a ticket we paid for. System-recorded from the ticketer's
	own figure, not a discretionary staff entry -- skips Finance review (same precedent as
	finance_engine.accrue_commission's Commission transaction)."""
	from decimal import Decimal

	from frappe.utils import today

	return frappe.get_doc(
		{
			"doctype": "Applicant Transaction",
			"applicant": placement.applicant,
			"placement": placement.name,
			"transaction_type": "Expense",
			"amount_original": Decimal(str(amount)),
			"currency_original": "ETB",
			"fx_rate": Decimal("1"),
			"fx_rate_date": today(),
			"description": description,
			"stage_logged_at": "Ticketing",
			"fee_type": fee_type,
			"logged_by": frappe.session.user,
			"status": "Approved",
		}
	).insert(ignore_permissions=True).name


def _correct_ticket_expense(txn_name, amount):
	"""Edit a ticket/reschedule expense's amount in place (a price correction is the same ticket,
	never a second ledger row). Returns a warning string instead when the row can't be edited."""
	from frappe.utils import flt

	txn = frappe.get_doc("Applicant Transaction", txn_name)
	if txn.status in ("Voided", "Rejected"):
		return f"{txn.name} is {txn.status}, so its amount wasn't changed -- ask Finance to record the corrected cost."
	if flt(amount) <= 0:
		return f"A ticket cost can't be corrected to 0 -- ask Finance to void {txn.name} instead."
	if flt(txn.amount_original) == flt(amount):
		return None
	old = txn.amount_original
	txn.amount_original = amount
	txn.save(ignore_permissions=True)
	log_action("Applicant Transaction", txn.name, f"{txn.fee_type} cost corrected {old} -> {amount} ETB")
	return None


@frappe.whitelist()
def record_ticket_details(placement_name=None, ticket_number=None, flight_date=None, ticket_cost=None, currency=None, **kwargs):
	"""Ticketer role. ticket_cost (always ETB -- any other currency is rejected) is recorded as ONE
	auto-Approved Applicant Transaction expense (fee_type "Ticket") the first time it's non-zero.
	Calling again with a different ticket_cost corrects that same row's amount (a price
	correction, not a new ticket); correcting only the ticket number/flight date touches no money.
	A new ticket bought because WE rescheduled is record_reschedule(cause="Internal") -- its own row.

	2026-09-23 (client item #12): the corridor's known fees are no longer summed in here -- each
	is recorded on its own, only when its clearance step completes (stage_fees.py). Ticketing
	deliberately doesn't re-check them: it couldn't tell a fee that was 0 at the time from one
	that failed, and would charge the later amount for the former."""
	if not placement_name:
		frappe.throw("placement_name is required.", frappe.ValidationError)
	if not ticket_number:
		frappe.throw("ticket_number is required.", frappe.ValidationError)
	if not flight_date:
		frappe.throw("flight_date is required.", frappe.ValidationError)
	_require_etb(currency, "Ticket cost")
	placement = frappe.get_doc("Placement", placement_name)
	if not placement.has_permission("write"):
		frappe.throw("Not permitted.", frappe.PermissionError)
	assert_placement_not_terminal(placement)

	from frappe.utils import flt

	previous_cost = placement.ticket_cost
	placement.ticket_number = ticket_number
	placement.flight_date = flight_date
	placement.ticket_cost = ticket_cost
	placement.save(ignore_permissions=True)

	result = placement.as_dict()
	ticket_amount = flt(ticket_cost)
	if not placement.corridor_fees_logged:
		if ticket_amount > 0:
			_record_ticket_expense(placement, ticket_amount, TICKET_FEE_TYPE, f"Ticket cost ({ticket_amount} ETB) for {placement_name}")
			placement.db_set("corridor_fees_logged", 1, update_modified=False)
			result["corridor_fees_logged"] = 1
		return result

	if flt(previous_cost) == ticket_amount:
		return result
	txn_name = frappe.db.get_value(
		"Applicant Transaction", {"placement": placement_name, "fee_type": TICKET_FEE_TYPE}, "name", order_by="creation asc"
	)
	if txn_name:
		warning = _correct_ticket_expense(txn_name, ticket_amount)
	else:
		# Logged before 2026-09-23 as the combined ticket + corridor-fees row -- no clean ticket
		# amount to edit.
		warning = "This ticket's cost was logged in the old combined entry, so it wasn't changed -- ask Finance to adjust it."
	if warning:
		result["warning"] = warning
	return result


@frappe.whitelist()
def record_reschedule(
	placement_name=None,
	reschedule_date=None,
	reschedule_cause=None,
	reschedule_cost=None,
	currency=None,
	ticket_number=None,
	transaction=None,
	**kwargs,
):
	"""Ticketer role. reschedule_date is the new flight date (flight_date moves with it).

	- Airport: the airline moved the flight; no new ticket is paid, nothing is recorded.
	- Internal: we rescheduled, so we buy a new ticket -- reschedule_cost (ETB, required) is
	  recorded as its OWN auto-Approved expense (fee_type "Ticket (Internal Reschedule)"), one per
	  reschedule. ticket_number, if given, replaces the placement's ticket number.
	- Correcting an Internal reschedule's price: pass transaction=<that expense's name> with the
	  corrected reschedule_cost -- the same row is edited, nothing new is recorded.
	The recorded expense's name is returned as reschedule_transaction."""
	if not placement_name:
		frappe.throw("placement_name is required.", frappe.ValidationError)
	_require_etb(currency, "Reschedule cost")
	placement = frappe.get_doc("Placement", placement_name)
	if not placement.has_permission("write"):
		frappe.throw("Not permitted.", frappe.PermissionError)
	assert_placement_not_terminal(placement)

	from frappe.utils import flt

	if transaction:
		if frappe.db.get_value("Applicant Transaction", transaction, ["placement", "fee_type"]) != (
			placement_name,
			RESCHEDULE_TICKET_FEE_TYPE,
		):
			frappe.throw(f"{transaction} isn't an internal-reschedule ticket cost of {placement_name}.", frappe.ValidationError)
		warning = _correct_ticket_expense(transaction, reschedule_cost)
		latest = frappe.db.get_value(
			"Applicant Transaction",
			{"placement": placement_name, "fee_type": RESCHEDULE_TICKET_FEE_TYPE},
			"name",
			order_by="creation desc",
		)
		if not warning and latest == transaction:
			placement.db_set("reschedule_cost", reschedule_cost)
		result = frappe.get_doc("Placement", placement_name).as_dict()
		result["reschedule_transaction"] = transaction
		if warning:
			result["warning"] = warning
		return result

	if not reschedule_date:
		frappe.throw("reschedule_date is required.", frappe.ValidationError)
	if reschedule_cause not in ("Internal", "Airport"):
		frappe.throw("reschedule_cause must be 'Internal' or 'Airport'.", frappe.ValidationError)
	if reschedule_cause == "Internal" and flt(reschedule_cost) <= 0:
		frappe.throw("reschedule_cost is required for an Internal reschedule -- we pay for the new ticket.", frappe.ValidationError)

	placement.is_rescheduled = 1
	placement.reschedule_date = reschedule_date
	placement.flight_date = reschedule_date
	placement.reschedule_cause = reschedule_cause
	placement.reschedule_cost = reschedule_cost if reschedule_cause == "Internal" else None
	if reschedule_cause == "Internal" and ticket_number:
		placement.ticket_number = ticket_number
	placement.save(ignore_permissions=True)

	result = placement.as_dict()
	if reschedule_cause == "Internal":
		result["reschedule_transaction"] = _record_ticket_expense(
			placement,
			flt(reschedule_cost),
			RESCHEDULE_TICKET_FEE_TYPE,
			f"New ticket after internal reschedule to {reschedule_date} ({flt(reschedule_cost)} ETB) for {placement_name}",
		)
	return result


#: Applicant fields joined onto every list_placements row (2026-09-19) -- fixed set of
#: read-only demographic/identity fields a placements workspace needs to render a row without
#: a second round-trip per row. Deliberately NOT the same policy as portal_api.PORTAL_FIELDS:
#: this endpoint is internal-staff-only (Placement's own doctype permissions already gate who
#: can call it at all -- see the docstring below), so there's no third-party PII exposure
#: concern the way there is for the Foreign Agency portal's candidate-browsing endpoints.
_PLACEMENT_LIST_APPLICANT_FIELDS = [
	"full_name",
	"passport_number",
	"photograph",
	"photo_full_body",
	"target_job",
	"nationality",
	"gender",
	"religion",
	"age",
	"date_of_birth",
]


@frappe.whitelist()
def list_placements(filters=None, limit_page_length=100, order_by="modified desc", limit_start=0, with_total=0):
	"""backend-issues #02: the whitelisted list surface Placement never had -- callers used to
	fall back to raw /api/resource/Placement, which only Manager/Admin/System Manager/Contract
	Parser/Ticketer could read (Placement's doctype-level permissions), 403ing every other role
	that legitimately needs to resolve a placement reference (Finance Manager, Clearance Officer,
	Complaint Manager, Communication Manager, the six country+step roles -- all granted read-only
	access on the doctype itself, see placement.json). frappe.get_list enforces those permissions
	the same way it would for any other doctype; no separate role check needed here.

	2026-09-19: also joins _PLACEMENT_LIST_APPLICANT_FIELDS onto each row (one batched
	frappe.get_all, not one query per placement) -- found live: the frontend was enriching each
	row with its own frappe.client.get call PER placement to fill these in, an N+1 pattern that
	also bypassed this app's own permission model (frappe.client.get only checks Applicant's
	default doctype permission, not any of this app's own role logic). destination_country is
	deliberately NOT re-added here even though Applicant has it too -- Placement already carries
	its own destination_country, and Placement.validate() guarantees it always equals the
	applicant's, so joining it again would just be a same-value overwrite."""
	if isinstance(filters, str):
		filters = frappe.parse_json(filters)
	start, length = page_args(limit_start, limit_page_length)
	placements = frappe.get_list(
		"Placement",
		filters=filters,
		fields=["*"],
		limit_start=start,
		limit_page_length=length,
		order_by=order_by,
	)

	applicant_names = list({p.applicant for p in placements if p.get("applicant")})
	applicant_by_name = {}
	if applicant_names:
		applicants = frappe.get_all(
			"Applicant",
			filters={"name": ["in", applicant_names]},
			fields=["name"] + _PLACEMENT_LIST_APPLICANT_FIELDS,
		)
		applicant_by_name = {a.name: a for a in applicants}

	for p in placements:
		applicant = applicant_by_name.get(p.get("applicant"))
		for field in _PLACEMENT_LIST_APPLICANT_FIELDS:
			p[field] = applicant.get(field) if applicant else None

	return paged_result(placements, with_total, lambda: count_rows("Placement", filters))


@frappe.whitelist()
def update_placement_parsed_fields(placement_name=None, **data):
	"""Hand-edit the parsed contract/visa identifiers on a Placement — primarily the visa number
	(the Injaz paper's left barcode) when parsing missed it or read it wrong.

	Restricted to the parsing staff (Contract Parser + the management fallback); Foreign Agency
	users can never reach it. Only PARSED_EDITABLE_FIELDS are accepted — status/applicant/contractor
	and every other lifecycle field are ignored, so this can neither move a placement's stage nor
	reassign it. In the Desk these fields are already editable directly by Contract Parser; this is
	the headless equivalent (Part F: no raw /api/resource writes)."""
	placement_name = placement_name or data.pop("name", None) or data.pop("placement", None)
	if not placement_name:
		frappe.throw("placement_name is required.", frappe.ValidationError)
	if frappe.session.user != "Administrator" and not (PARSE_EDIT_ROLES & set(frappe.get_roles())):
		frappe.throw("Not permitted.", frappe.PermissionError)

	placement = frappe.get_doc("Placement", placement_name)
	assert_placement_not_terminal(placement)

	updates = {k: v for k, v in data.items() if k in PARSED_EDITABLE_FIELDS}
	if not updates:
		frappe.throw(
			"No editable field supplied. Allowed: " + ", ".join(PARSED_EDITABLE_FIELDS),
			frappe.ValidationError,
		)
	placement.update(updates)
	placement.save(ignore_permissions=True)
	return placement.as_dict()


@frappe.whitelist()
def get_placement(placement_name=None, **kwargs):
	placement_name = placement_name or kwargs.get("name") or kwargs.get("placement")
	if not placement_name:
		frappe.throw("placement_name is required.", frappe.ValidationError)
	if not frappe.db.exists("Placement", placement_name):
		frappe.throw(f"Placement {placement_name} not found.", frappe.DoesNotExistError)
	doc = frappe.get_doc("Placement", placement_name)
	if not doc.has_permission("read"):
		frappe.throw("Not permitted.", frappe.PermissionError)
	return doc.as_dict()
