# Copyright (c) 2026, Agency and contributors
# License: MIT. See LICENSE
#
# Part D: Financial Architecture. Pure logic lives here (mirrors clearance_engine.py's role);
# whitelisted entry points live in finance_api.py. Self-registers into
# state_machine.TRANSITION_SIDE_EFFECTS at the bottom — see agency_tracking/__init__.py for why
# that import ordering matters (same reasoning as clearance_engine's Step 7 registration).

import frappe
from frappe.utils import today
from decimal import Decimal

from agency_tracking.db_errors import reraise_if_db_abort
from agency_tracking.state_machine import TRANSITION_SIDE_EFFECTS, lock_doc_row, log_action, sanctioned_write


# --- FX rates (Part D: "live rate fetched at entry... as_of_date override for backdated
# entries", Part H: "scheduled daily fetch from a currency API, cached") ---


def get_fx_rate(currency, as_of_date=None):
	"""Cached rate for `currency` on `as_of_date` (default today). Falls back to the most
	recent cached rate on or before that date — the "historical lookup for backdated entries"
	Part H describes. Throws if nothing's ever been recorded for this currency; there's no safe
	made-up default for money.

	ETB is Birr itself (2026-08-29 correction) -- there's no "conversion" to compute, so it's
	hardcoded to 1.0 rather than requiring a Finance Manager to record a meaningless FX Rate
	row for it."""
	if currency == "ETB":
		return Decimal("1.0"), as_of_date or today()

	as_of_date = as_of_date or today()
	exact = frappe.db.get_value(
		"FX Rate", {"currency": currency, "rate_date": as_of_date}, "rate_to_birr"
	)
	if exact:
		return exact, as_of_date

	fallback = frappe.db.get_value(
		"FX Rate",
		{"currency": currency, "rate_date": ["<=", as_of_date]},
		["rate_to_birr", "rate_date"],
		order_by="rate_date desc",
	)
	if fallback:
		return fallback[0], fallback[1]

	frappe.throw(
		f"No FX rate available for {currency} on or before {as_of_date}. "
		"A Finance Manager needs to record one before this can be logged.",
		frappe.ValidationError,
	)


def positive_rate(value):
	"""An FX rate as an exact Decimal, refused unless it is a number above zero -- a zero or negative
	rate would convert every amount in that currency to nothing or to the wrong sign (QA P7-02)."""
	from decimal import Decimal

	try:
		rate = Decimal(str(value).strip())
	except Exception:
		frappe.throw("rate_to_birr must be a number.", frappe.ValidationError)
	if not rate.is_finite() or rate <= 0:
		frappe.throw("rate_to_birr must be greater than zero.", frappe.ValidationError)
	return rate


def record_fx_rate(currency, rate_to_birr, rate_date=None):
	if currency == "ETB":
		frappe.throw("ETB is Birr itself -- it always converts 1:1, no FX rate to record.", frappe.ValidationError)
	rate_to_birr = positive_rate(rate_to_birr)
	rate_date = rate_date or today()
	existing = frappe.db.get_value("FX Rate", {"currency": currency, "rate_date": rate_date}, "name")
	if existing:
		frappe.db.set_value("FX Rate", existing, "rate_to_birr", rate_to_birr)
		convert_awaiting_fx(currency)
		return existing
	# FXRate.on_update runs convert_awaiting_fx for a newly inserted rate.
	doc = frappe.get_doc(
		{"doctype": "FX Rate", "currency": currency, "rate_date": rate_date, "rate_to_birr": rate_to_birr}
	).insert(ignore_permissions=True)
	return doc.name


# --- Recording before an FX rate exists (2026-09-23) ---
# System-recorded amounts (stage fees, ticket cost) used to be dropped when their currency had no
# FX rate at all. They're now recorded in their own currency with awaiting_fx_rate=1 (amount_birr
# 0), and converted as soon as any rate for that currency is recorded -- by hand (set_fx_rate), by
# the scheduled fetch, or in Desk (all go through record_fx_rate / FXRate.on_update).


def get_fx_rate_or_none(currency, as_of_date=None):
	"""Like get_fx_rate, but (None, None) instead of throwing when this currency has never had a
	rate on or before as_of_date."""
	try:
		return get_fx_rate(currency, as_of_date)
	except frappe.ValidationError:
		frappe.clear_last_message()
		return None, None


def convert_awaiting_fx(currency):
	"""Convert every awaiting_fx_rate Applicant Transaction in `currency`. Uses the rate for the
	transaction's own date when one exists on or before it, otherwise the earliest rate after it
	(the first rate that ever became available). Idempotent; returns the converted names."""
	converted = []
	for txn in frappe.get_all(
		"Applicant Transaction",
		filters={"awaiting_fx_rate": 1, "currency_original": currency},
		fields=["name", "amount_original", "creation"],
	):
		txn_date = frappe.utils.getdate(txn.creation)
		rate, rate_date = get_fx_rate_or_none(currency, txn_date)
		if rate is None:
			later = frappe.db.get_value(
				"FX Rate", {"currency": currency, "rate_date": [">", txn_date]},
				["rate_to_birr", "rate_date"], order_by="rate_date asc",
			)
			if not later:
				continue
			rate, rate_date = later
		rate = Decimal(str(rate))
		frappe.db.set_value(
			"Applicant Transaction",
			txn.name,
			{
				"fx_rate": rate,
				"fx_rate_date": rate_date,
				"amount_birr": round(Decimal(str(txn.amount_original)) * rate, 2),
				"awaiting_fx_rate": 0,
			},
		)
		converted.append(txn.name)
	return converted


# Currencies the app converts to Birr, and the public rate source. We use ExchangeRate-API's
# free, keyless "open" endpoint (open.er-api.com), which -- unlike ECB-sourced APIs such as
# frankfurter.app -- actually covers ETB and the Gulf currencies (SAR/KWD/AED/QAR) this app needs.
# frankfurter returns 404 for an ETB base, which is why the previous implementation silently
# fetched nothing.
FX_TARGET_CURRENCIES = ["SAR", "KWD", "USD", "AED", "QAR"]
FX_SOURCE_URL = "https://open.er-api.com/v6/latest/ETB"


def fetch_daily_fx_rates():
	"""Pull live rates from a public, trusted source and cache them as FX Rate rows.

	Base is ETB, so the API's rates[X] is "X per 1 ETB"; we invert to rate_to_birr = "ETB per 1 X"
	to match how get_fx_rate/record_fx_rate store and use it. This is the auto path (Global mode);
	Custom mode never calls it -- a Finance Manager sets rates by hand via set_fx_rate. Failures
	must never break the app: catch broadly, log, and leave the existing cache / manual entry as
	the fallback get_fx_rate() already provides. Returns the dict of currencies actually recorded
	(empty on failure) so callers/tests can see what happened.
	"""
	import requests

	recorded = {}
	try:
		response = requests.get(FX_SOURCE_URL, timeout=10)
		response.raise_for_status()
		data = response.json()
		if data.get("result") != "success":
			frappe.log_error(title="fetch_daily_fx_rates: source returned non-success", message=str(data)[:500])
			return recorded
		rates = data.get("rates", {})
		rate_date = today()
		for currency in FX_TARGET_CURRENCIES:
			foreign_per_etb = rates.get(currency)
			if foreign_per_etb:
				rate_to_birr = round(Decimal("1.0") / Decimal(str(foreign_per_etb)), 6)
				record_fx_rate(currency, rate_to_birr, rate_date)
				recorded[currency] = float(rate_to_birr)
	except Exception:
		frappe.log_error(title="fetch_daily_fx_rates failed")
	return recorded


FX_INTERVAL_HOURS = {"1 Hour": 1, "3 Hours": 3, "6 Hours": 6, "Daily": 24}


def maybe_fetch_fx_rates():
	"""Runs hourly (hooks.py) but only actually calls the API when FX Rate Settings says to.
	mode="Custom" -> always a no-op, Finance Manager/Admin use set_fx_rate exclusively.
	mode="Global" -> only fires once the configured fetch_interval has actually elapsed since
	the last successful fetch (Frappe's cron granularity doesn't support arbitrary intervals
	directly, so this polls hourly and self-throttles)."""
	settings = frappe.get_single("FX Rate Settings")
	if settings.mode != "Global":
		return
	interval_hours = FX_INTERVAL_HOURS.get(settings.fetch_interval or "Daily", 24)
	if settings.last_fetched_at:
		elapsed_hours = (frappe.utils.now_datetime() - settings.last_fetched_at).total_seconds() / 3600
		if elapsed_hours < interval_hours:
			return
	fetch_daily_fx_rates()
	frappe.db.set_value("FX Rate Settings", None, "last_fetched_at", frappe.utils.now_datetime())


# --- Commission rate resolution (Part D pseudocode, transcribed) ---


def get_commission_rate(placement):
	"""Resolve the commission (amount, currency) for a placement.

	A per-placement manual amount always wins (one-off / negotiated deals). Otherwise fall back to
	the contractor's default rate table, which is now keyed by destination country + entry track
	(Standard/Muayena) + gender -- so an agency configures a rate per male/female for each track."""
	if placement.manual_commission_amount and placement.manual_commission_currency:
		return placement.manual_commission_amount, placement.manual_commission_currency

	applicant = frappe.get_doc("Applicant", placement.applicant)
	return get_contractor_default_rate(
		placement.contractor, placement.destination_country, applicant.entry_track, applicant.gender
	)


def get_contractor_default_rate(contractor_name, destination_country, entry_track=None, gender=None):
	"""Look up an agency's configured default rate. entry_track/gender narrow the match to the
	specific (Standard|Muayena) x (Male|Female) row; omitting them matches the first row for the
	country (kept for backward-compatible callers). The rate's currency is whatever the operator
	chose on the row (SAR/KWD/USD/ETB/AED/QAR)."""
	filters = {"parent": contractor_name, "destination_country": destination_country}
	if entry_track:
		filters["entry_track"] = entry_track
	if gender:
		filters["gender"] = gender
	row = frappe.db.get_value("Contractor Commission Rate", filters, ["rate", "currency"])
	if not row:
		scope = " / ".join(filter(None, [contractor_name, destination_country, entry_track, gender]))
		frappe.throw(
			f"No default commission rate configured for {scope}.",
			frappe.ValidationError,
		)
	return row[0], row[1]


# --- Accrual (Part D: "on reaching Departed (default) or via manual early-trigger
# (idempotency-guarded either way)") ---


def accrue_commission(placement, from_status=None, actor=None):
	if placement.status == "Cancelled":
		# P4-14: a cancelled case earns no commission (the early trigger reached this).
		frappe.throw(f"{placement.name} is Cancelled -- no commission.", frappe.ValidationError)
	if placement.is_free_replacement:
		# Part A.4: "commission fee waived for that one cycle" — already collected on the
		# original placement this one replaces. Not an idempotency no-op; there was never
		# going to be a commission transaction for this placement at all.
		return None
	# S-10: serialise accruals for this placement (early trigger vs Departed), then check with a
	# locking read -- a plain read would use this transaction's snapshot and miss the other
	# request's just-committed row.
	lock_doc_row("Placement", placement.name)
	if frappe.db.get_value(
		"Applicant Transaction",
		{"placement": placement.name, "transaction_type": "Commission", "status": ["!=", "Voided"]},
		"name",
		for_update=True,
	):
		return None  # idempotency guard — already accrued, early-trigger or Departed alike

	amount, currency = get_commission_rate(placement)
	# No FX rate yet: recorded in its own currency, awaiting the rate (converted by
	# convert_awaiting_fx when one is recorded), like stage fees -- P4-09 / D-07.
	fx_rate, fx_rate_date = get_fx_rate_or_none(currency)
	awaiting_fx = fx_rate is None
	fx_rate = Decimal("0") if awaiting_fx else Decimal(str(fx_rate))
	with sanctioned_write():  # system-created Approved ledger row (QA A1)
		txn = frappe.get_doc(
			{
				"doctype": "Applicant Transaction",
				"placement": placement.name,
				"transaction_type": "Commission",
				"amount_original": Decimal(str(amount)),
				"currency_original": currency,
				"fx_rate": fx_rate,
				"fx_rate_date": fx_rate_date,
				"amount_birr": round(Decimal(str(amount)) * fx_rate, 2),
				"awaiting_fx_rate": 1 if awaiting_fx else 0,
				"stage_logged_at": placement.status,
				"logged_by": actor or frappe.session.user,
				# System-computed, not a discretionary staff entry -- auto-Approved, skips the
				# Finance review step that human-logged income/expense entries go through.
				"status": "Approved",
			}
		).insert(ignore_permissions=True)

	_maybe_auto_batch(placement.contractor, placement.destination_country)
	return txn


# --- Batching (Part D: "both paths converge on one create_batch_request() function") ---


def _owed_commission_filters(contractor_name, destination_country, currency=None):
	placements = frappe.get_all(
		"Placement",
		filters={"contractor": contractor_name, "destination_country": destination_country},
		pluck="name",
	)
	filters = {
		"placement": ["in", placements or [""]],
		"transaction_type": "Commission",
		"status": "Approved",
		"commission_batch_request": ["is", "not set"],
		# Invoiced only once it has its FX rate: the batch's Birr totals are fixed when it's built.
		"awaiting_fx_rate": 0,
	}
	if currency:
		filters["currency_original"] = currency
	return filters


def list_owed_commissions(contractor_name, destination_country, order="oldest", currency=None):
	"""Owed commissions for a contractor+country, optionally narrowed to one currency. A
	contractor's rate table can price different tracks/genders in different currencies, so
	without a currency filter this can return a mix -- callers that batch (create_batch_request)
	always group by currency, since a batch/invoice is single-currency."""
	order_by = "creation asc" if order == "oldest" else "creation desc"
	return frappe.get_all(
		"Applicant Transaction",
		filters=_owed_commission_filters(contractor_name, destination_country, currency),
		fields=["name", "placement", "amount_original", "currency_original", "amount_birr", "creation"],
		order_by=order_by,
	)


def list_owed_commissions_by_currency(contractor_name, destination_country):
	"""Same pool as list_owed_commissions, grouped by currency -- what a batch picker should show
	so a Finance user can't accidentally try to batch two currencies together."""
	rows = list_owed_commissions(contractor_name, destination_country)
	grouped = {}
	for row in rows:
		grouped.setdefault(row["currency_original"], []).append(row)
	return grouped


def _unpaid_item_names_from_open_batches(contractor_name, currency):
	"""Every still-Pending Commission Batch Item sitting in one of this contractor's other open
	batches (anything not Settled -- Draft/Sent aren't used any more, 2026-09-23) in the same currency -- the exact same pool the
	invoice PDF's own print-only "previous unpaid" line already sums for display (see
	render_batch_invoice_pdf). Used by create_batch_request's include_unpaid_from_previous flag
	(2026-09-12) to actually fold that amount into a NEW batch as real, trackable items, instead
	of just a cosmetic number on the printed invoice."""
	open_batch_names = frappe.get_all(
		"Commission Batch Request",
		filters={"contractor": contractor_name, "currency": currency, "status": ["!=", "Settled"]},
		pluck="name",
	)
	if not open_batch_names:
		return []
	return frappe.get_all(
		"Commission Batch Item",
		filters={"parent": ["in", open_batch_names], "status": "Pending"},
		pluck="name",
	)


def _original_batch_for(transaction_name):
	"""If this commission was previously carried out of an earlier batch (its old item row was
	marked Released), return that earlier batch for trace-back. None for a first-time batching."""
	return frappe.db.get_value(
		"Commission Batch Item",
		{"transaction": transaction_name, "status": "Released"},
		"parent",
		order_by="modified desc",
	)


def _batch_item_rows(transaction_names):
	"""Build item child rows, stamping original_batch on any commission carried over from a prior
	batch's released (unpaid) item so the new batch shows where it was first requested."""
	return [
		{"transaction": t, "original_batch": _original_batch_for(t)} for t in transaction_names
	]


def _single_currency_of(transaction_names):
	"""A batch is one invoice in one currency -- verify the given transactions agree, and return
	it. Throws rather than silently picking one if they disagree (mixed-currency batching is a
	Finance-user mistake, e.g. picking Standard/USD and Muayena/SAR rows for the same contractor
	into one request)."""
	currencies = set(
		frappe.get_all(
			"Applicant Transaction", filters={"name": ["in", transaction_names]}, pluck="currency_original"
		)
	)
	if len(currencies) > 1:
		frappe.throw(
			f"Cannot batch commissions in different currencies together ({', '.join(sorted(currencies))}). "
			"Create separate batches per currency.",
			frappe.ValidationError,
		)
	return currencies.pop() if currencies else None


def _lock_and_verify_unclaimed(transaction_names):
	"""Row-lock exactly the Applicant Transaction rows about to be claimed into a new batch, and
	re-verify each is still unclaimed (commission_batch_request IS NULL) under that lock.
	2026-09-12 fix for a real race: two concurrent create_batch_request calls (manual, or two
	Placements simultaneously tripping _maybe_auto_batch's threshold for the same contractor)
	could otherwise both read the same still-unclaimed pool before either commits, and both
	create a batch containing the same commission -- double-invoicing it. A locking read always
	returns latest-committed data regardless of this transaction's own snapshot, same primitive as
	lock_applicant_row/lock_doc_row use for the identical class of bug elsewhere in this app."""
	if not transaction_names:
		return
	placeholders = ", ".join(["%s"] * len(transaction_names))
	still_unclaimed = {
		row[0]
		for row in frappe.db.sql(
			f"SELECT `name` FROM `tabApplicant Transaction` WHERE `name` IN ({placeholders}) "
			"AND `commission_batch_request` IS NULL FOR UPDATE",
			tuple(transaction_names),
		)
	}
	already_claimed = set(transaction_names) - still_unclaimed
	if already_claimed:
		frappe.throw(
			f"Already claimed by another batch, not available: {', '.join(sorted(already_claimed))}.",
			frappe.ValidationError,
		)


def create_batch_request(
	contractor_name, destination_country, transaction_names=None, requested_advance_amount=None, currency=None,
	include_unpaid_from_previous=False,
):
	"""currency narrows the owed pool when transaction_names isn't given explicitly -- required
	whenever a contractor+country has owed commissions in more than one currency (see
	list_owed_commissions_by_currency), since a batch/invoice is always single-currency.

	include_unpaid_from_previous (2026-09-12, "include unpaid from previous" action): also folds
	in any still-Pending items sitting in this contractor's other open batches (anything not
	Settled) in the same currency -- the exact pool the invoice PDF's own print-only "previous
	unpaid" line already sums for display, but actually made part of THIS batch's real, per-item-
	trackable total instead of just a cosmetic number on the printout. Releases those items from
	their old batch first (same mechanism as release_unpaid_items, called manually), so they
	genuinely move, not just get counted twice."""
	if transaction_names is None:
		owed = list_owed_commissions(contractor_name, destination_country, currency=currency)
		if not currency:
			distinct = {row["currency_original"] for row in owed}
			if len(distinct) > 1:
				frappe.throw(
					f"Owed commissions for {contractor_name} / {destination_country} span multiple "
					f"currencies ({', '.join(sorted(distinct))}). Specify which currency to batch.",
					frappe.ValidationError,
				)
		transaction_names = [row["name"] for row in owed]

	if include_unpaid_from_previous:
		carry_currency = currency or _single_currency_of(transaction_names)
		if not carry_currency:
			frappe.throw(
				"currency is required to include unpaid items from previous batches.",
				frappe.ValidationError,
			)
		carried_item_names = _unpaid_item_names_from_open_batches(contractor_name, carry_currency)
		if carried_item_names:
			carried_txn_names = frappe.get_all(
				"Commission Batch Item", filters={"name": ["in", carried_item_names]}, pluck="transaction"
			)
			release_unpaid_items(carried_item_names)
			transaction_names = list(dict.fromkeys(list(transaction_names) + carried_txn_names))
			currency = carry_currency

	if not transaction_names:
		existing = frappe.db.get_value(
			"Commission Batch Request",
			{"contractor": contractor_name, "destination_country": destination_country, "status": "Draft"},
			"name",
		)
		if existing:
			return frappe.get_doc("Commission Batch Request", existing)
		batch = frappe.get_doc(
			{
				"doctype": "Commission Batch Request",
				"contractor": contractor_name,
				"destination_country": destination_country,
				"currency": currency,
				"status": "Draft",
				"requested_advance_amount": requested_advance_amount or 0,
				"items": [],
			}
		).insert(ignore_permissions=True)
		return batch

	_lock_and_verify_unclaimed(transaction_names)
	awaiting = frappe.get_all(
		"Applicant Transaction", {"name": ["in", list(transaction_names)], "awaiting_fx_rate": 1}, pluck="name"
	)
	if awaiting:
		frappe.throw(
			f"Still waiting for an FX rate, so not invoiceable yet: {', '.join(sorted(awaiting))}.",
			frappe.ValidationError,
		)
	batch_currency = _single_currency_of(transaction_names)
	batch = frappe.get_doc(
		{
			"doctype": "Commission Batch Request",
			"contractor": contractor_name,
			"destination_country": destination_country,
			"currency": batch_currency,
			"status": "Draft",
			"requested_advance_amount": requested_advance_amount or 0,
			"items": _batch_item_rows(transaction_names),
		}
	).insert(ignore_permissions=True)

	frappe.db.set_value(
		"Applicant Transaction", {"name": ["in", transaction_names]}, "commission_batch_request", batch.name
	)
	return batch


def apply_batch_write_off(batch_name, write_off_amount, write_off_reason):
	"""Record an agreed discount the agency won't pay (Requested vs Paid vs Expense): books an
	Expense Applicant Transaction for the written-off amount, links it to the batch, and lets the
	controller reduce balance_due (settling the batch once paid + write-offs cover the total). A
	batch can have MULTIPLE write-offs -- each negotiation round (or partial discount) appends its
	own row to batch.write_offs rather than replacing a single field, so the batch keeps a full
	history of every discount agreed, not just the last one.

	write_off_amount is in the BATCH'S OWN CURRENCY (e.g. the $1000 negotiated off a $5000 USD
	batch), matching how the agency actually negotiates and how the invoice is denominated --
	not Birr. It's converted to Birr at the rate the batch's still-unpaid commissions were booked at
	(D-03, user 2026-09-30), not today's: waiving a debt moves no money, so a full write-off must net
	the batch's Birr balance to exactly zero -- no FX gain or loss on money that never moved."""
	if not write_off_reason:
		frappe.throw("A reason is required to write off a batch amount.", frappe.ValidationError)
	amount = Decimal(str(write_off_amount or 0))
	if amount <= 0:
		frappe.throw("write_off_amount must be greater than zero.", frappe.ValidationError)

	# 2026-09-12 fix: lock BEFORE reading the batch, not after -- two concurrent write-offs on the
	# same batch (two tabs, two staff, or a double-click) previously both read the same
	# pre-write accounted total, both passed the ceiling check below independently, and both
	# committed, over-crediting the batch past its own total with no error at all. Locking first
	# means the second call's read happens only after the first has fully committed, so it sees
	# the first write-off already counted in existing_write_off_original.
	lock_doc_row("Commission Batch Request", batch_name)
	batch = frappe.get_doc("Commission Batch Request", batch_name)
	# Reconcile against everything already accounted for -- per-item payments + every existing
	# write-off + this new one can't exceed the obligation, else the batch is over-credited
	# (audit N-1). Advance is deliberately excluded -- it's a loan requested ahead, not a payment
	# against this batch, so it doesn't count toward this ceiling (matches _apply_settlement_math).
	# All in the batch's own currency, same as the invoice the agency is negotiating against.
	paid_items_original, paid_items_birr = batch.paid_from_items()
	existing_write_off_original, existing_write_off_birr = batch.write_off_totals()
	accounted = (
		Decimal(str(paid_items_original))
		+ Decimal(str(existing_write_off_original))
		+ amount
	)
	if accounted > Decimal(str(batch.total_amount_original or 0)):
		frappe.throw(
			f"Paid-per-item ({paid_items_original}) + "
			f"existing write-offs ({existing_write_off_original}) + this write-off ({amount}) cannot "
			f"exceed the batch total ({batch.total_amount_original or 0}) {batch.currency}.",
			frappe.ValidationError,
		)

	# The carrying rate of what's still owed (> 0: the ceiling check above leaves at least `amount`).
	unpaid_original = Decimal(str(batch.total_amount_original or 0)) - paid_items_original - existing_write_off_original
	unpaid_birr = Decimal(str(batch.total_amount_birr or 0)) - paid_items_birr - existing_write_off_birr
	fx_rate = (unpaid_birr / unpaid_original).quantize(Decimal("0.000000001"))
	fx_rate_date = today()
	amount_birr = round(amount * fx_rate, 2)

	with sanctioned_write():  # system-created Approved ledger row (QA A1)
		txn = frappe.get_doc(
			{
				"doctype": "Applicant Transaction",
				"transaction_type": "Expense",
				"amount_original": amount,
				"currency_original": batch.currency,
				"fx_rate": Decimal(str(fx_rate)),
				"fx_rate_date": fx_rate_date,
				"amount_birr": amount_birr,
				"commission_batch_request": batch.name,
				"description": f"Commission write-off (agreed discount) for {batch.name}: {write_off_reason}",
				"stage_logged_at": "Commission Batch",
				"logged_by": frappe.session.user,
				# System-recorded settlement adjustment, not a discretionary human ledger entry.
				"status": "Approved",
			}
		).insert(ignore_permissions=True)

	batch.append(
		"write_offs",
		{
			"amount_original": amount,
			"amount_birr": amount_birr,
			"reason": write_off_reason,
			"transaction": txn.name,
			"write_off_date": today(),
		},
	)
	batch.save(ignore_permissions=True)
	log_action(
		"Commission Batch Request",
		batch.name,
		f"[{batch.title or batch.name}] Write-off {amount} {batch.currency} ({amount_birr} Birr): {write_off_reason} (txn {txn.name})",
	)
	return batch


def release_unpaid_items(item_names):
	"""Carry unpaid items out of their (usually already-settled) batch back into the owed pool so
	they can be pulled into a NEW request. The old item row stays as history, flipped to Released
	(dropping out of the old batch's total); the underlying commission transaction is unlinked so
	list_owed_commissions surfaces it again. The new batch stamps original_batch for trace-back."""
	if not item_names:
		frappe.throw("No items given.", frappe.ValidationError)
	released, affected = [], set()
	for item_name in item_names:
		row = frappe.db.get_value(
			"Commission Batch Item", item_name, ["parent", "transaction", "status"], as_dict=True
		)
		if not row:
			continue
		if row.status == "Paid":
			frappe.throw(f"{item_name} is already Paid and cannot be released.", frappe.ValidationError)
		if row.status == "Released":
			continue
		frappe.db.set_value("Applicant Transaction", row.transaction, "commission_batch_request", None)
		frappe.db.set_value("Commission Batch Item", item_name, "status", "Released")
		released.append(item_name)
		if row.parent:
			affected.add(row.parent)
	# Recompute each source batch's total/status now that some items no longer count.
	for batch_name in affected:
		batch = frappe.get_doc("Commission Batch Request", batch_name)
		batch.save(ignore_permissions=True)
		log_action(
			"Commission Batch Request",
			batch_name,
			f"[{batch.title or batch_name}] Released unpaid items back to the owed pool: {released}",
		)
	return {"released_items": released, "affected_batches": list(affected)}


def settle_batch_request(batch_name, settlement_reference):
	"""Shared by the manual settle_batch API call and the Step 9 reconciliation matcher — one
	function both paths converge on, same reasoning as create_batch_request(). Whole-batch
	settlement (e.g. a bank statement line matching the batch's full total) -- marks every
	item Paid too, so the per-item and whole-batch settlement paths never disagree."""
	if not settlement_reference:
		frappe.throw("A settlement reference is required.", frappe.ValidationError)
	batch = frappe.get_doc("Commission Batch Request", batch_name)
	if batch.status == "Settled":
		return batch  # idempotent — a statement line re-matched against an already-settled batch is a no-op
	for item in batch.items:
		# A Released item was carried into a later invoice and is owed there, not here (QA P5-05).
		if item.status != "Released":
			item.status = "Paid"
	batch.status = "Settled"
	batch.settlement_reference = settlement_reference
	batch.settled_on = today()
	batch.save(ignore_permissions=True)
	return batch


def _sync_batch_status_from_items(batch):
	"""Batch-level status follows its items: any Paid but not all -> Partially Settled; all
	Paid -> Settled (settled_on stamped once, on first reaching that point)."""
	statuses = [item.status for item in batch.items]
	if statuses and all(s == "Paid" for s in statuses):
		batch.status = "Settled"
		if not batch.settled_on:
			batch.settled_on = today()
	elif any(s == "Paid" for s in statuses):
		batch.status = "Partially Settled"
	batch.save(ignore_permissions=True)


def mark_batch_items_paid(item_names):
	"""Explicit multi-select manual settlement -- item_names are Commission Batch Item child
	row names. Groups by parent batch so each affected batch's status gets synced once."""
	if not item_names:
		frappe.throw("No items given.", frappe.ValidationError)
	affected_batches = set()
	for item_name in item_names:
		parent, status = frappe.db.get_value("Commission Batch Item", item_name, ["parent", "status"]) or (None, None)
		# A Released item is owed on the invoice it was carried into, not this one (QA P5-06).
		if status == "Released":
			frappe.throw(
				f"{item_name} was released from invoice {parent} to a later invoice; mark it paid there.",
				frappe.ValidationError,
			)
		frappe.db.set_value("Commission Batch Item", item_name, "status", "Paid")
		if parent:
			affected_batches.add(parent)
	for batch_name in affected_batches:
		batch = frappe.get_doc("Commission Batch Request", batch_name)
		_sync_batch_status_from_items(batch)
	return {"updated_items": item_names, "affected_batches": list(affected_batches)}


def match_batch_payment_proof(batch_name, file_url):
	"""Agency sends a CSV or PDF listing paid applicant names -- best-effort parse + fuzzy
	name match against this batch's own item list (via each item's Applicant Transaction ->
	Placement -> Applicant). Unmatched names are simply skipped (stay Pending for manual
	settle_batch_items review), same 'never blocks' philosophy as contract_parser.py and the
	existing bank-statement reconciliation matcher."""
	from agency_tracking.reconciliation_engine import parse_paid_applicant_names

	paid_names = parse_paid_applicant_names(file_url)
	batch = frappe.get_doc("Commission Batch Request", batch_name)

	matched_items = []
	unmatched_names = set(paid_names)
	for item in batch.items:
		if item.status == "Paid":
			continue
		placement_name = frappe.db.get_value("Applicant Transaction", item.transaction, "placement")
		if not placement_name:
			continue
		applicant_name = frappe.db.get_value("Placement", placement_name, "applicant")
		full_name = frappe.db.get_value("Applicant", applicant_name, "full_name") or ""
		match = next((p for p in unmatched_names if p.strip().lower() == full_name.strip().lower()), None)
		if match:
			item.status = "Paid"
			matched_items.append(item.name)
			unmatched_names.discard(match)

	_sync_batch_status_from_items(batch)
	return {
		"matched_items": matched_items,
		"unmatched_names": list(unmatched_names),
	}


def render_batch_invoice_pdf(batch_name):
	"""On-demand PDF (applicant names + amounts) via Frappe's standard print/wkhtmltopdf path
	-- not pre-generated/stored at batch creation, built fresh whenever requested.

	Agency-facing, so everything is shown in the batch's own currency (self.currency) --
	the amounts the foreign agency actually negotiated in. Birr never appears here; it's
	only the internal income/expense figure, visible in the app's own Finance views."""
	from agency_tracking.pdf_utils import attach_datauri, render_pdf

	batch = frappe.get_doc("Commission Batch Request", batch_name)

	# idx numbering is 1-based over ALL items (Released ones included in the count, just not
	# appended below) -- preserved as-is, only the lookups are batched.
	included_items = [(idx, item) for idx, item in enumerate(batch.items, start=1) if item.status != "Released"]

	# Batch-fetch transaction -> placement -> applicant in 3 queries total instead of up to 3
	# per row (was N+1 -- see raw.md / the perf pass this came out of).
	txn_names = [item.transaction for _, item in included_items]
	txn_by_name = {}
	if txn_names:
		txns = frappe.get_all(
			"Applicant Transaction",
			filters={"name": ["in", txn_names]},
			fields=["name", "placement", "amount_original", "stage_logged_at"],
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
	applicant_by_name = {}
	if applicant_names:
		applicants = frappe.get_all(
			"Applicant", filters={"name": ["in", applicant_names]}, fields=["name", "full_name", "passport_number"]
		)
		applicant_by_name = {a.name: a for a in applicants}

	rows = []
	for idx, item in included_items:
		txn = txn_by_name.get(item.transaction)
		placement_name = txn.placement if txn else None
		applicant_name = applicant_by_placement.get(placement_name) if placement_name else None
		applicant = applicant_by_name.get(applicant_name) if applicant_name else None
		rows.append(
			{
				"idx": idx,
				"full_name": (applicant and applicant.full_name) or "—",
				"passport_number": applicant and applicant.passport_number,
				"transaction_date": txn.stage_logged_at if txn else None,
				"payment_reference": item.payment_reference,
				"amount_original": txn.amount_original if txn else None,
				"status": item.status,
			}
		)

	contractor = frappe.db.get_value(
		"Contractor", batch.contractor, ["contractor_name", "license_no", "telephone"], as_dict=True
	) or {}

	# 2026-09-09: TOTAL on the printed invoice is now batch total + requested advance (an
	# up-front "pay this ASAP" ask, not yet received -- see create_commission_batch's
	# requested_advance_amount) + an arrears carry-forward from the contractor's other still-open
	# batches in the same currency. This is print-only -- it doesn't touch balance_due_original,
	# which stays just this batch's own total minus paid/write-offs.
	from frappe.utils import flt

	previous_unpaid_original = flt(
		(
			frappe.get_all(
				"Commission Batch Request",
				filters={
					"contractor": batch.contractor,
					"currency": batch.currency,
					"name": ["!=", batch.name],
					# Only invoices issued BEFORE this one (2026-09-23) -- now that every unsettled
					# batch counts as open (Draft included), re-printing an older invoice would
					# otherwise also pull in newer invoices' balances as "previous".
					"creation": ["<", batch.creation],
					"status": ["!=", "Settled"],
				},
				fields=["sum(balance_due_original) as total"],
			)
			or [{}]
		)[0].get("total")
	)

	settings = frappe.get_single("Agency Tracking Settings")
	agency = {
		"agency_name": settings.agency_name,
		"agency_address": settings.agency_address,
		"agency_phone": settings.agency_phone,
		"logo": attach_datauri(settings.logo),
		"stamp_image": attach_datauri(settings.stamp_image),
		"bank_name": settings.bank_name,
		"account_name": settings.account_name,
		"account_number": settings.account_number,
		"bank_address": settings.bank_address,
		"bank_swift_code": settings.bank_swift_code,
		"bank_phone": settings.bank_phone,
		"bank_email": settings.bank_email,
	}

	return render_pdf(
		"agency_tracking/templates/commission_batch_invoice.html",
		{
			"batch": batch,
			"rows": rows,
			"contractor_name": contractor.get("contractor_name"),
			"contractor_license_no": contractor.get("license_no"),
			"contractor_telephone": contractor.get("telephone"),
			"agency": agency,
			"today": today(),
			"previous_unpaid_original": previous_unpaid_original,
		},
	)


def _maybe_auto_batch(contractor_name, destination_country):
	"""Threshold is checked per currency -- a contractor with 12 owed USD commissions and 8 owed
	SAR commissions has two independent pools, since batches (and their invoices) are always
	single-currency. Hitting the threshold in one currency doesn't pull in the other."""
	contractor = frappe.get_doc("Contractor", contractor_name)
	if contractor.batch_mode != "Auto-Threshold" or not contractor.batch_threshold:
		return
	owed_by_currency = list_owed_commissions_by_currency(contractor_name, destination_country)
	for currency, rows in owed_by_currency.items():
		if len(rows) >= contractor.batch_threshold:
			create_batch_request(contractor_name, destination_country, currency=currency)


# --- Placement-stage write authorization (addendum: "whoever's assigned to the placement's
# current stage, via a narrow whitelisted function") ---


def is_assigned_to_placement(user, placement_name):
	clearance_step_names = frappe.get_all(
		"Clearance Step", filters={"placement": placement_name}, pluck="name"
	)
	has_clearance_todo = bool(clearance_step_names) and frappe.db.exists(
		"ToDo",
		{
			"reference_type": "Clearance Step",
			"reference_name": ["in", clearance_step_names],
			"allocated_to": user,
			"status": "Open",
		},
	)
	has_placement_todo = frappe.db.exists(
		"ToDo",
		{"reference_type": "Placement", "reference_name": placement_name, "allocated_to": user, "status": "Open"},
	)
	return bool(has_clearance_todo or has_placement_todo)


def _on_placement_departed(placement, from_status=None):
	"""TRANSITION_SIDE_EFFECTS only holds one handler per (doctype, status) key -- this wraps
	accrue_commission rather than registering a second handler at the same key, which would
	silently replace it (see clearance_engine.py's own comment on this same single-slot design).

	Also closes the "Confirm departure..." Placement ToDo(s) here rather than inside
	accrue_commission itself: accrue_commission is also called directly, standalone, from
	finance_api.py for an early commission trigger *before* a Placement has actually reached
	Departed (see its own comment, "bill sooner than Departed") -- closing the departure task
	from inside accrue_commission would have marked it done on that early-billing path too, even
	though nobody had actually confirmed the applicant departed yet. This wrapper only runs on
	the real Ticketed -> Departed transition, so the ToDo only closes when departure is genuine.

	The two are independently guarded -- a commission-accrual failure (e.g. a missing FX/
	commission-rate config) must not also block the ToDo close, or this wrapper would introduce
	a coupling that didn't exist before: today a failed accrue_commission is already just logged
	and swallowed by transition()'s own outer try/except (real failures need a human to notice
	and resolve manually, per its comment above), and departure confirmation closing its own task
	shouldn't start depending on billing having gone through cleanly."""
	from agency_tracking.clearance_engine import _close_placement_todos, _placement_label, _placement_todo_to_role

	try:
		accrue_commission(placement, from_status=from_status)
		failure = None
	except Exception as exc:
		reraise_if_db_abort(exc)
		failure = str(exc)
		frappe.log_error(
			title="accrue_commission failed on Departed",
			message=f"{placement.name}: {frappe.get_traceback()}",
		)
	_close_placement_todos(placement.name)
	if failure:
		# P4-09 / D-07: the case departs, and Finance gets a task it can see (after the close above).
		_placement_todo_to_role(
			placement,
			"Finance Manager",
			f"Departed with no commission: {_placement_label(placement.name)}. {failure} "
			"Fix it, then record the commission (trigger_early_commission_accrual).",
		)


TRANSITION_SIDE_EFFECTS[("Placement", "Departed")] = _on_placement_departed
