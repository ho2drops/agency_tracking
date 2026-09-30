# Copyright (c) 2026, Agency and contributors
# License: MIT. See LICENSE
#
# Part F: module-scoped whitelisted functions, no raw /api/resource/* exposure.

import frappe
from frappe.utils import flt, now, today

from agency_tracking.finance_engine import (
	accrue_commission,
	apply_batch_write_off,
	create_batch_request,
	fetch_daily_fx_rates,
	get_fx_rate as _get_fx_rate,
	list_owed_commissions,
	list_owed_commissions_by_currency,
	mark_batch_items_paid,
	match_batch_payment_proof,
	record_fx_rate,
	release_unpaid_items as _release_unpaid_items,
	render_batch_invoice_pdf,
	settle_batch_request,
)
from agency_tracking.pagination import count_rows, page_args, paged_result
from agency_tracking.roles import require_internal_staff
from agency_tracking.state_machine import log_action, transition
from decimal import Decimal


def _positive_decimal(value, label):
	"""A money input as an exact Decimal, refused unless it is a number above zero. Sign comes from
	the transaction type, so amounts are always positive (QA P5-08); 0 or a missing value is a
	validation error, not a crash (QA P5-09)."""
	if value is None or value == "":
		frappe.throw(f"{label} is required.", frappe.ValidationError)
	try:
		value = Decimal(str(value).strip())
	except Exception:
		frappe.throw(f"{label} must be a number.", frappe.ValidationError)
	if not value.is_finite() or value <= 0:
		frappe.throw(f"{label} must be greater than zero.", frappe.ValidationError)
	return value


def _log_stage_transaction(
	transaction_type,
	amount,
	currency,
	description,
	placement_name=None,
	stage_logged_at=None,
	applicant=None,
	stage=None,
):
	"""2026-08-29: open to any internal staff role, no longer gated on being assigned to the
	placement's current stage — Finance Manager/Admin approval (approve_transaction/
	reject_transaction) is the real gate now, so the write side can be permissive."""
	require_internal_staff()
	amount = _positive_decimal(amount, "amount")

	placement = frappe.get_doc("Placement", placement_name) if placement_name else None
	if not applicant and placement:
		applicant = placement.applicant
	elif applicant and not placement:
		active_placement = frappe.db.get_value("Applicant", applicant, "active_placement")
		if active_placement:
			placement_name = active_placement
			placement = frappe.get_doc("Placement", placement_name)

	stage_value = stage or stage_logged_at
	if not stage_value:
		if placement:
			stage_value = placement.status
		elif applicant:
			stage_value = frappe.db.get_value("Applicant", applicant, "status")

	fx_rate, fx_rate_date = _get_fx_rate(currency)
	txn = frappe.get_doc(
		{
			"doctype": "Applicant Transaction",
			"applicant": applicant,
			"placement": placement_name,
			"transaction_type": transaction_type,
			"amount_original": amount,
			"currency_original": currency,
			"fx_rate": Decimal(str(fx_rate)),
			"fx_rate_date": fx_rate_date,
			"amount_birr": round(amount * Decimal(str(fx_rate)), 2),
			"description": description,
			"stage_logged_at": stage_value,
			"logged_by": frappe.session.user,
		}
	).insert(ignore_permissions=True)
	return txn.as_dict()


@frappe.whitelist()
def log_stage_expense(amount=None, currency=None, description=None, placement=None, applicant=None, stage=None, stage_logged_at=None, **kwargs):
	"""Ad-hoc expense logging, still the right endpoint for reschedule fees, general office
	expenses, or anything else not covered by a corridor's known fees.

	2026-09-19: do NOT use this with stage set to a clearance step type (LMIS Clearance/Kuwait
	LMIS/Telesign/Taeshir/Embassy/Kuwait Embassy) -- those costs are now known in advance
	(Corridor Definition.known_fees), and since 2026-09-23 each is recorded as its own already-
	Approved expense when its clearance step completes (stage_fees.py). A manual log here for
	one of those fees would double-count it."""
	if amount is None:
		amount = kwargs.get("amount_original", kwargs.get("amount_birr"))
	currency = currency or kwargs.get("currency_original") or "ETB"
	description = description or kwargs.get("reference_text") or kwargs.get("remarks") or "Expense"
	placement = placement or kwargs.get("placement_name")
	applicant = applicant or kwargs.get("applicant_name")
	return _log_stage_transaction(
		"Expense",
		amount,
		currency,
		description,
		placement_name=placement,
		stage_logged_at=stage_logged_at,
		applicant=applicant,
		stage=stage,
	)


@frappe.whitelist()
def log_stage_income(amount=None, currency=None, description=None, placement=None, applicant=None, stage=None, stage_logged_at=None, **kwargs):
	"""Same shape/permissions as log_stage_expense -- see its docstring for the same clearance-
	step-cost caveat (known fees are auto-logged as Expense, not Income, at ticketing time)."""
	if amount is None:
		amount = kwargs.get("amount_original", kwargs.get("amount_birr"))
	currency = currency or kwargs.get("currency_original") or "ETB"
	description = description or kwargs.get("reference_text") or kwargs.get("remarks") or "Income"
	placement = placement or kwargs.get("placement_name")
	applicant = applicant or kwargs.get("applicant_name")
	return _log_stage_transaction(
		"Income",
		amount,
		currency,
		description,
		placement_name=placement,
		stage_logged_at=stage_logged_at,
		applicant=applicant,
		stage=stage,
	)


@frappe.whitelist()
def approve_transaction(transaction_name=None, **kwargs):
	"""Finance Manager/Admin only. Moves Pending -> Approved via the sanctioned transition()
	path -- only Approved entries count toward ledger/balance totals (Part D)."""
	if not transaction_name:
		frappe.throw("transaction_name is required.", frappe.ValidationError)
	if not ({"Finance Manager", "Admin"} & set(frappe.get_roles())):
		frappe.throw("Not permitted.", frappe.PermissionError)

	txn = frappe.get_doc("Applicant Transaction", transaction_name)
	txn.approved_by = frappe.session.user
	txn.approved_on = now()
	transition(txn, "Approved")
	return txn.as_dict()


@frappe.whitelist()
def reject_transaction(transaction_name=None, rejection_reason=None, **kwargs):
	"""Finance Manager/Admin only, mandatory reason. Pending -> Rejected; never counts toward
	the ledger."""
	if not transaction_name:
		frappe.throw("transaction_name is required.", frappe.ValidationError)
	if not ({"Finance Manager", "Admin"} & set(frappe.get_roles())):
		frappe.throw("Not permitted.", frappe.PermissionError)
	if not rejection_reason:
		frappe.throw("A reason is required to reject a transaction.", frappe.ValidationError)

	txn = frappe.get_doc("Applicant Transaction", transaction_name)
	txn.rejection_reason = rejection_reason
	transition(txn, "Rejected", remarks=rejection_reason)
	return txn.as_dict()


@frappe.whitelist()
def void_transaction(transaction_name=None, void_reason=None, **kwargs):
	"""No hard delete, ever (addendum). Finance Manager/Admin only, mandatory reason. Only
	legal from Approved (ALLOWED_TRANSITIONS enforces this — transition() itself rejects
	voiding a Pending/Rejected row). Routed through transition() like every other status
	change (never doc.status = X; doc.save() directly) -- the row stays visible with its
	status flagged and a Process Event on the audit trail, never disappears."""
	if not transaction_name:
		frappe.throw("transaction_name is required.", frappe.ValidationError)
	if not ({"Finance Manager", "Admin"} & set(frappe.get_roles())):
		frappe.throw("Not permitted.", frappe.PermissionError)
	if not void_reason:
		frappe.throw("A reason is required to void a transaction.", frappe.ValidationError)

	# A commission still on an invoice (item Pending or Paid) would keep being billed to the agency
	# after voiding -- the invoice total, balance and PDF all still count it (QA P5-03 / S-05).
	# Release it from the invoice first; Released items are off the invoice and may be voided.
	on_invoice = frappe.get_all(
		"Commission Batch Item",
		filters={"transaction": transaction_name, "status": ["in", ["Pending", "Paid"]]},
		pluck="parent",
		limit=1,
	)
	if on_invoice:
		frappe.throw(
			f"This transaction is on invoice {on_invoice[0]}. Release it from that invoice before voiding it.",
			frappe.ValidationError,
		)

	txn = frappe.get_doc("Applicant Transaction", transaction_name)
	transition(txn, "Voided", remarks=void_reason)
	# Voiding a write-off's Expense row takes the write-off off its invoice: recompute the balance
	# (QA P5-04).
	for batch_name in frappe.get_all(
		"Commission Batch Write Off", filters={"transaction": transaction_name}, pluck="parent", distinct=True
	):
		frappe.get_doc("Commission Batch Request", batch_name).save(ignore_permissions=True)
	return txn.as_dict()


@frappe.whitelist()
def list_transactions(
	status=None,
	transaction_type=None,
	placement=None,
	applicant=None,
	from_date=None,
	to_date=None,
	order_by="creation desc",
	limit_page_length=100,
	limit_start=0,
	with_total=0,
	**kwargs,
):
	"""Finance Manager/Admin/System Manager. Applicant Transaction history across every status
	(Pending/Approved/Rejected/Voided) -- unlike get_pending_approval_queue (report_api.py), which
	only ever shows Pending. Surfaces who acted on each row: approved_by/approved_on for
	Approved, rejection_reason for Rejected, logged_by for who originally created it. from_date/
	to_date filter on creation date (inclusive)."""
	if not ({"Finance Manager", "Admin", "System Manager"} & set(frappe.get_roles())):
		frappe.throw("Not permitted.", frappe.PermissionError)
	filters = {}
	status = status or kwargs.get("transaction_status")
	if status:
		filters["status"] = status
	if transaction_type:
		filters["transaction_type"] = transaction_type
	if placement:
		filters["placement"] = placement
	if applicant:
		filters["applicant"] = applicant
	if from_date and to_date:
		filters["creation"] = ["between", [from_date, to_date]]
	elif from_date:
		filters["creation"] = [">=", from_date]
	elif to_date:
		filters["creation"] = ["<=", to_date]
	start, length = page_args(limit_start, limit_page_length)
	rows = frappe.get_all(
		"Applicant Transaction",
		filters=filters,
		fields=[
			"name", "applicant", "placement", "transaction_type", "stage_logged_at", "status",
			"amount_original", "currency_original", "amount_birr", "description",
			"logged_by", "approved_by", "approved_on", "rejection_reason",
			"commission_batch_request", "clearance_step", "fee_type", "awaiting_fx_rate", "creation",
		],
		order_by=order_by,
		limit_start=start,
		limit_page_length=length,
	)
	return paged_result(rows, with_total, lambda: count_rows("Applicant Transaction", filters, ignore_permissions=True))


@frappe.whitelist()
def trigger_early_commission_accrual(placement_name=None, **kwargs):
	"""Part D: "Manual early-trigger (idempotency-guarded either way)" — for cases needing to
	bill sooner than Departed. Same accrue_commission() as the automatic path, so calling this
	and then later reaching Departed naturally is a no-op the second time."""
	if not placement_name:
		frappe.throw("placement_name is required.", frappe.ValidationError)
	if not ({"Finance Manager", "Admin", "Manager"} & set(frappe.get_roles())):
		frappe.throw("Not permitted.", frappe.PermissionError)
	placement = frappe.get_doc("Placement", placement_name)
	txn = accrue_commission(placement)
	if txn is None:
		frappe.throw(f"{placement_name} already has an active commission transaction.", frappe.ValidationError)
	return txn.as_dict()


@frappe.whitelist()
def get_fx_rate(currency=None, as_of_date=None, **kwargs):
	if not currency:
		frappe.throw("currency is required.", frappe.ValidationError)
	if not ({"Finance Manager", "Admin", "System Manager"} & set(frappe.get_roles())):
		frappe.throw("Not permitted.", frappe.PermissionError)
	rate, rate_date = _get_fx_rate(currency, as_of_date)
	return {"currency": currency, "rate_to_birr": rate, "rate_date": rate_date}


@frappe.whitelist()
def set_fx_rate(currency=None, rate_to_birr=None, rate_date=None, **kwargs):
	if not currency:
		frappe.throw("currency is required.", frappe.ValidationError)
	if not ({"Finance Manager", "Admin", "System Manager"} & set(frappe.get_roles())):
		frappe.throw("Not permitted.", frappe.PermissionError)
	rate = next((r for r in (rate_to_birr, kwargs.get("rate_to_etb"), kwargs.get("rate")) if r not in (None, "")), None)
	if rate is None:
		frappe.throw("rate_to_birr is required.", frappe.ValidationError)
	return {"fx_rate": record_fx_rate(currency, rate, rate_date or today())}


@frappe.whitelist()
def fetch_fx_rates_now():
	"""Manually pull live rates from the public source right now (Finance Manager/Admin), instead
	of waiting for the scheduled Global-mode fetch. Custom-mode sites simply never need this --
	setting rates by hand via set_fx_rate stays the default, fully-supported path. Returns the
	currencies actually recorded (empty if the source was unreachable; the existing cache stands)."""
	if not ({"Finance Manager", "Admin", "System Manager"} & set(frappe.get_roles())):
		frappe.throw("Not permitted.", frappe.PermissionError)
	recorded = fetch_daily_fx_rates()
	return {"recorded": recorded, "count": len(recorded)}


@frappe.whitelist()
def get_owed_commissions(contractor=None, destination_country=None, order="oldest", currency=None, **kwargs):
	"""currency narrows to one currency's owed pool -- pass it when building a batch (a batch is
	always single-currency). Omit it to see everything owed regardless of currency; if that spans
	more than one currency, use get_owed_commissions_by_currency to group them for picking."""
	if not ({"Finance Manager", "Admin", "System Manager"} & set(frappe.get_roles())):
		frappe.throw("Not permitted.", frappe.PermissionError)
	contractor = contractor or kwargs.get("contractor_name")
	if not contractor:
		frappe.throw("contractor is required.", frappe.ValidationError)
	if not destination_country:
		destination_country = frappe.db.get_value("Contractor", contractor, "country")
	if not destination_country:
		return []
	return list_owed_commissions(contractor, destination_country, order, currency)


@frappe.whitelist()
def get_owed_commissions_by_currency(contractor=None, destination_country=None, **kwargs):
	"""Owed commissions grouped by currency -- what a "create a batch" screen should show, since
	a batch/invoice is always single-currency and a contractor's rate table can price different
	tracks/genders differently."""
	if not ({"Finance Manager", "Admin", "System Manager"} & set(frappe.get_roles())):
		frappe.throw("Not permitted.", frappe.PermissionError)
	contractor = contractor or kwargs.get("contractor_name")
	if not contractor:
		frappe.throw("contractor is required.", frappe.ValidationError)
	if not destination_country:
		destination_country = frappe.db.get_value("Contractor", contractor, "country")
	if not destination_country:
		return {}
	return list_owed_commissions_by_currency(contractor, destination_country)


@frappe.whitelist()
def create_commission_batch(
	contractor=None,
	destination_country=None,
	transaction_names=None,
	requested_advance_amount=None,
	currency=None,
	include_unpaid_from_previous=None,
	**kwargs,
):
	"""Manual batching path (Part D: "both paths converge on one create_batch_request()
	function" — the other path is the automatic one inside finance_engine.accrue_commission).
	transaction_names selects which owed commissions to include (default: all owed for the
	contractor/country, which can include items carried over from prior batches via
	release_unpaid_items). requested_advance_amount records an up-front "pay this ASAP" ask, in
	the batch's currency.

	include_unpaid_from_previous (2026-09-12, "include unpaid from previous" button): when true,
	also folds in any still-unpaid items from this contractor's other open batches (anything not
	Settled) in the same currency, so this new batch's own total genuinely includes them --
	trackable per item afterward via settle_batch_items, same as any other item. Requires currency
	to be known (pass it explicitly, or it's derived from transaction_names when given).

	A batch is always single-currency (it's what gets invoiced to one agency in one currency).
	If transaction_names is omitted and the owed pool spans more than one currency, pass currency
	to say which one to batch (see get_owed_commissions_by_currency to see the split first)."""
	if not ({"Finance Manager", "Admin", "System Manager"} & set(frappe.get_roles())):
		frappe.throw("Not permitted.", frappe.PermissionError)
	contractor = contractor or kwargs.get("contractor_name")
	if not contractor:
		frappe.throw("contractor is required.", frappe.ValidationError)
	if not destination_country:
		destination_country = frappe.db.get_value("Contractor", contractor, "country")
	if not destination_country:
		frappe.throw("destination_country is required.", frappe.ValidationError)
	if isinstance(transaction_names, str):
		transaction_names = frappe.parse_json(transaction_names)
	requested_advance_amount = requested_advance_amount if requested_advance_amount is not None else kwargs.get("requested_advance")
	include_unpaid_from_previous = include_unpaid_from_previous in (1, "1", True, "true", "True")
	batch = create_batch_request(
		contractor, destination_country, transaction_names, requested_advance_amount, currency,
		include_unpaid_from_previous=include_unpaid_from_previous,
	)
	return batch.as_dict()


@frappe.whitelist()
def update_batch_advance(batch_name=None, requested_advance_amount=None, **kwargs):
	"""Set or change the advance amount requested on an EXISTING batch (2026-09-12).
	create_commission_batch only lets requested_advance_amount be set at creation time -- this is
	the endpoint for adding one afterward, or correcting it. Same deliberately simple model as
	everywhere else Advance appears in this app: just a number added into the printed invoice
	total (Batch Total + requested_advance_amount + previous_unpaid_original) -- no reference
	number, no received-date, no settlement-math linkage (see
	commission_batch_request._apply_settlement_math). Pass 0 to clear a previously-set advance."""
	if not ({"Finance Manager", "Admin", "System Manager"} & set(frappe.get_roles())):
		frappe.throw("Not permitted.", frappe.PermissionError)
	batch_name = batch_name or kwargs.get("batch") or kwargs.get("name")
	if not batch_name or not frappe.db.exists("Commission Batch Request", batch_name):
		frappe.throw("A valid batch_name is required.", frappe.ValidationError)
	if requested_advance_amount is None:
		frappe.throw("requested_advance_amount is required.", frappe.ValidationError)
	amount = flt(requested_advance_amount)
	if amount < 0:
		frappe.throw("requested_advance_amount cannot be negative.", frappe.ValidationError)

	batch = frappe.get_doc("Commission Batch Request", batch_name)
	previous = batch.requested_advance_amount or 0
	batch.requested_advance_amount = amount
	batch.save(ignore_permissions=True)
	log_action(
		"Commission Batch Request",
		batch.name,
		f"[{batch.title or batch.name}] Requested advance changed: {previous} -> {amount} {batch.currency or ''}".strip(),
	)
	return batch.as_dict()


@frappe.whitelist()
def write_off_batch(batch_name=None, write_off_amount=None, write_off_reason=None, **kwargs):
	"""Record an agreed discount on a batch (the agency pays less by negotiation), e.g. a batch
	invoiced at $5000 where the agency negotiates $1000 off and pays $4000. Books an Expense for
	the shortfall and, once paid + write-offs cover the total, settles the batch (advance is
	excluded -- see commission_batch_request._apply_settlement_math).
	Callable multiple times per batch -- each call appends a new write-off row (batch.write_offs)
	rather than replacing a single field, so a batch can have several negotiated discounts over
	time, each with its own reason and its own Expense transaction. write_off_amount is in the
	batch's own currency (batch.currency), same as the invoice -- not Birr; Birr is derived
	internally for accounting."""
	if not ({"Finance Manager", "Admin"} & set(frappe.get_roles())):
		frappe.throw("Not permitted.", frappe.PermissionError)
	batch_name = batch_name or kwargs.get("batch") or kwargs.get("name")
	write_off_amount = write_off_amount if write_off_amount is not None else kwargs.get("amount")
	write_off_reason = write_off_reason or kwargs.get("reason")
	if not batch_name or not frappe.db.exists("Commission Batch Request", batch_name):
		frappe.throw("A valid batch_name is required.", frappe.ValidationError)
	return apply_batch_write_off(batch_name, write_off_amount, write_off_reason).as_dict()


@frappe.whitelist()
def list_batch_write_offs(batch_name=None, **kwargs):
	"""Every write-off row booked against one batch (amount, reason, date, linked Expense
	transaction) -- oldest first, so a "why is this batch short?" screen can show the full
	negotiation history instead of just the summed write_off_total_original. A batch can have
	several rows (write_off_batch is callable more than once per batch)."""
	if not ({"Finance Manager", "Admin", "System Manager"} & set(frappe.get_roles())):
		frappe.throw("Not permitted.", frappe.PermissionError)
	batch_name = batch_name or kwargs.get("batch") or kwargs.get("name")
	if not batch_name or not frappe.db.exists("Commission Batch Request", batch_name):
		frappe.throw("A valid batch_name is required.", frappe.ValidationError)
	return frappe.get_all(
		"Commission Batch Write Off",
		filters={"parent": batch_name, "parenttype": "Commission Batch Request"},
		fields=["name", "amount_original", "amount_birr", "reason", "transaction", "write_off_date", "idx"],
		order_by="idx asc",
	)


@frappe.whitelist()
def release_unpaid_items(item_names=None, **kwargs):
	"""Carry unpaid items out of a (usually settled) batch back into the owed pool so they can be
	pulled into a new commission request. Each keeps a trace-back to its original batch."""
	if not ({"Finance Manager", "Admin"} & set(frappe.get_roles())):
		frappe.throw("Not permitted.", frappe.PermissionError)
	item_names = item_names if item_names is not None else kwargs.get("items")
	if isinstance(item_names, str):
		item_names = frappe.parse_json(item_names)
	return _release_unpaid_items(item_names)


@frappe.whitelist()
def list_commission_batches(contractor=None, status=None, destination_country=None, **kwargs):
	"""List commission batches (Finance view) with their money summary -- for picking a batch to
	settle / write off / inspect."""
	if not ({"Finance Manager", "Admin", "System Manager"} & set(frappe.get_roles())):
		frappe.throw("Not permitted.", frappe.PermissionError)
	filters = {}
	contractor = contractor or kwargs.get("contractor_name")
	if contractor:
		filters["contractor"] = contractor
	if status:
		filters["status"] = status
	if destination_country:
		filters["destination_country"] = destination_country
	return frappe.get_all(
		"Commission Batch Request",
		filters=filters,
		fields=[
			"name", "contractor", "destination_country", "currency", "status",
			"total_amount_original", "total_amount_birr", "requested_advance_amount",
			"paid_amount_original", "paid_amount_birr",
			"advance_amount_original", "advance_amount",
			"write_off_total_original", "write_off_total_birr",
			"balance_due_original", "balance_due_birr", "settled_on", "creation",
		],
		order_by="creation desc",
	)


@frappe.whitelist()
def get_commission_batch(batch_name=None, **kwargs):
	"""Full batch detail incl. per-applicant item rows (name, amount, status, carried-from batch)
	-- the data behind the invoice PDF, for a batch/applicant listing screen."""
	if not ({"Finance Manager", "Admin", "System Manager"} & set(frappe.get_roles())):
		frappe.throw("Not permitted.", frappe.PermissionError)
	batch_name = batch_name or kwargs.get("batch") or kwargs.get("name")
	if not batch_name or not frappe.db.exists("Commission Batch Request", batch_name):
		frappe.throw("A valid batch_name is required.", frappe.ValidationError)
	batch = frappe.get_doc("Commission Batch Request", batch_name)

	# Batch-fetch transaction -> placement -> applicant -> full_name in 3 queries total instead
	# of up to 5 per item row (was N+1 -- see raw.md / the perf pass this came out of).
	txn_names = [row.transaction for row in batch.items]
	txn_by_name = {}
	if txn_names:
		txns = frappe.get_all(
			"Applicant Transaction",
			filters={"name": ["in", txn_names]},
			fields=["name", "placement", "amount_original", "amount_birr"],
		)
		txn_by_name = {t.name: t for t in txns}

	placement_names = list({t.placement for t in txn_by_name.values() if t.placement})
	applicant_by_placement = {}
	if placement_names:
		placements = frappe.get_all(
			"Placement", filters={"name": ["in", placement_names]}, fields=["name", "applicant"]
		)
		applicant_by_placement = {p.name: p.applicant for p in placements}

	applicant_names = list({a for a in applicant_by_placement.values() if a})
	full_name_by_applicant = {}
	if applicant_names:
		applicants = frappe.get_all(
			"Applicant", filters={"name": ["in", applicant_names]}, fields=["name", "full_name"]
		)
		full_name_by_applicant = {a.name: a.full_name for a in applicants}

	items = []
	for row in batch.items:
		txn = txn_by_name.get(row.transaction)
		placement = txn.placement if txn else None
		applicant = applicant_by_placement.get(placement) if placement else None
		items.append(
			{
				"item": row.name,
				"transaction": row.transaction,
				"placement": placement,
				"applicant": applicant,
				"full_name": full_name_by_applicant.get(applicant) if applicant else None,
				"amount_original": txn.amount_original if txn else None,
				"amount_birr": txn.amount_birr if txn else None,
				"status": row.status,
				"original_batch": row.get("original_batch"),
			}
		)
	detail = batch.as_dict()
	detail["items_detail"] = items
	return detail


@frappe.whitelist()
def settle_batch(batch_name=None, settlement_reference=None, **kwargs):
	if not ({"Finance Manager", "Admin"} & set(frappe.get_roles())):
		frappe.throw("Not permitted.", frappe.PermissionError)
	if not batch_name or not frappe.db.exists("Commission Batch Request", batch_name):
		frappe.throw("A valid batch_name is required.", frappe.ValidationError)
	if not settlement_reference:
		frappe.throw("settlement_reference is required.", frappe.ValidationError)
	return settle_batch_request(batch_name, settlement_reference).as_dict()


@frappe.whitelist()
def settle_batch_items(item_names=None, **kwargs):
	"""AGREED_SPEC.md Part 7.3 (backend-issues #09): explicit multi-select manual settlement,
	alongside upload_batch_payment_proof's best-effort parser -- marks specific Commission
	Batch Item child rows Paid and syncs each affected batch's status (Partially Settled until
	every item is Paid, then Settled)."""
	if not item_names:
		frappe.throw("item_names is required.", frappe.ValidationError)
	if not ({"Finance Manager", "Admin"} & set(frappe.get_roles())):
		frappe.throw("Not permitted.", frappe.PermissionError)
	if isinstance(item_names, str):
		item_names = frappe.parse_json(item_names)
	return mark_batch_items_paid(item_names)


@frappe.whitelist()
def upload_batch_payment_proof(batch_name=None, file_url=None, **kwargs):
	"""AGREED_SPEC.md Part 7.3 (backend-issues #09): parses a CSV or PDF listing paid applicant
	names (best-effort), fuzzy-matches against this batch's own item list, marks matched items
	Paid. Unmatched names stay Pending for manual settle_batch_items review -- never blocks."""
	if not batch_name:
		frappe.throw("batch_name is required.", frappe.ValidationError)
	if not file_url:
		frappe.throw("file_url is required.", frappe.ValidationError)
	if not ({"Finance Manager", "Admin"} & set(frappe.get_roles())):
		frappe.throw("Not permitted.", frappe.PermissionError)
	return match_batch_payment_proof(batch_name, file_url)


@frappe.whitelist()
def get_batch_invoice_pdf(batch_name=None, **kwargs):
	"""AGREED_SPEC.md Part 7.3 (backend-issues #09): on-demand PDF (applicant names + amounts),
	built fresh whenever requested, not pre-generated/stored at batch creation."""
	if not batch_name:
		frappe.throw("batch_name is required.", frappe.ValidationError)
	if not ({"Finance Manager", "Admin"} & set(frappe.get_roles())):
		frappe.throw("Not permitted.", frappe.PermissionError)
	frappe.local.response.filename = f"{batch_name}-invoice.pdf"
	frappe.local.response.filecontent = render_batch_invoice_pdf(batch_name)
	frappe.local.response.type = "pdf"


@frappe.whitelist()
def enqueue_get_batch_invoice_pdf(batch_name=None, **kwargs):
	"""Async twin of get_batch_invoice_pdf -- same permission gate, but returns a Background Job
	reference immediately instead of blocking on the render. Poll
	background_jobs.get_job_status(job) for the result."""
	if not batch_name:
		frappe.throw("batch_name is required.", frappe.ValidationError)
	if not ({"Finance Manager", "Admin"} & set(frappe.get_roles())):
		frappe.throw("Not permitted.", frappe.PermissionError)
	if not frappe.db.exists("Commission Batch Request", batch_name):
		frappe.throw(f"Commission Batch Request {batch_name} not found.", frappe.DoesNotExistError)

	from agency_tracking.background_jobs import enqueue_job

	job = enqueue_job(
		"Render Batch Invoice PDF",
		reference_doctype="Commission Batch Request",
		reference_name=batch_name,
		batch_name=batch_name,
	)
	return {"job": job, "status": "Queued"}
