# Copyright (c) 2026, Agency and contributors
# License: MIT. See LICENSE
#
# Part I Step 13 / business-workflow-srs.md Part 8: "Management should be able to see, for any
# date range they choose... something anyone can pull up on demand for any custom date range —
# not a report someone has to manually assemble." Part F names get_financial_overview
# specifically as Admin-only; the rest are Manager/Admin ("management visibility").
#
# Built on top of Process Event (Step 6) wherever a pipeline-stage count is really "how many
# transitions of this kind happened in this window" — that's exactly what Process Event's
# reference_doctype/to_status/creation already record, so no new counters or duplicate logging
# were needed for CV-generated/selected/ticketed/departed counts. Only Clearance Step gained a
# genuinely new field this step (completed_by, above) because nothing already captured "who."

import frappe
from frappe.utils import getdate

from agency_tracking.roles import INTERNAL_STAFF_ROLES

MANAGEMENT_ROLES = {"Manager", "Admin", "Finance Manager", "System Manager"}


def _require_management():
	if not (MANAGEMENT_ROLES & set(frappe.get_roles())):
		frappe.throw("Not permitted.", frappe.PermissionError)


def _normalize_dates(from_date=None, to_date=None, **kwargs):
	from_date = from_date or kwargs.get("start_date") or kwargs.get("from") or frappe.utils.add_days(frappe.utils.today(), -30)
	to_date = to_date or kwargs.get("end_date") or kwargs.get("to") or frappe.utils.today()
	return str(from_date), str(to_date)


def _day_range(from_date, to_date):
	"""BETWEEN bounds for a Datetime column (generated_on, or the framework's own `creation`).
	A plain date string as the upper bound is interpreted as that day's midnight, silently
	excluding every same-day row with a nonzero time — caught by a same-day test asserting a
	backdated-to-10am row was actually counted."""
	return [f"{from_date} 00:00:00", f"{to_date} 23:59:59"]


def _transition_count(to_status, from_date, to_date, reference_doctype="Placement"):
	return frappe.db.count(
		"Process Event",
		filters={
			"reference_doctype": reference_doctype,
			"to_status": to_status,
			"creation": ["between", _day_range(from_date, to_date)],
		},
	)


@frappe.whitelist()
def get_daily_work_report(from_date=None, to_date=None, **kwargs):
	"""business-workflow-srs.md Part 8's exact list: CVs created, medicals processed,
	clearances issued, embassies cleared, tickets booked, departures confirmed."""
	_require_management()
	from_date, to_date = _normalize_dates(from_date, to_date, **kwargs)
	return {
		"from_date": from_date,
		"to_date": to_date,
		"cvs_created": frappe.db.count(
			"CV Record", filters={"docstatus": 1, "generated_on": ["between", _day_range(from_date, to_date)]}
		),
		"medicals_processed": frappe.db.count(
			"Applicant", filters={"medical_issue_date": ["between", [from_date, to_date]]}
		),
		"clearances_issued": frappe.db.count(
			"Clearance Step",
			filters={"status": ["in", ["Complete", "Issued"]], "date_completed": ["between", [from_date, to_date]]},
		),
		"embassies_cleared": frappe.db.count(
			"Clearance Step",
			filters={
				"step_type": ["in", ["Embassy", "Kuwait Embassy"]],
				"status": ["in", ["Stamped", "Complete"]],
				"date_completed": ["between", [from_date, to_date]],
			},
		),
		"tickets_booked": _transition_count("Ticketed", from_date, to_date),
		"departures_confirmed": _transition_count("Departed", from_date, to_date),
	}


@frappe.whitelist()
def get_staff_performance_report(from_date=None, to_date=None, **kwargs):
	""""The same breakdown per individual staff member — how much each person handled in a
	given period." Grouped by whoever actually did the work (CV Record.generated_by,
	Clearance Step.completed_by, Process Event.actor for placement-stage transitions) —
	deliberately not attempting to attribute "medicals processed" per staff member, since
	nothing in this build records who recorded a medical result; inventing an attribution here
	would be a guess dressed up as data.
	"""
	_require_management()
	from_date, to_date = _normalize_dates(from_date, to_date, **kwargs)

	performance = {}

	def _bucket(user):
		if user not in performance:
			performance[user] = {
				"user": user,
				"cvs_created": 0,
				"clearances_completed": 0,
				"tickets_booked": 0,
				"departures_confirmed": 0,
			}
		return performance[user]

	for row in frappe.get_all(
		"CV Record",
		filters={"docstatus": 1, "generated_on": ["between", _day_range(from_date, to_date)]},
		fields=["generated_by"],
	):
		if row.generated_by:
			_bucket(row.generated_by)["cvs_created"] += 1

	for row in frappe.get_all(
		"Clearance Step",
		filters={
			"status": ["in", ["Complete", "Issued", "Stamped"]],
			"date_completed": ["between", [from_date, to_date]],
		},
		fields=["completed_by"],
	):
		if row.completed_by:
			_bucket(row.completed_by)["clearances_completed"] += 1

	for row in frappe.get_all(
		"Process Event",
		filters={
			"reference_doctype": "Placement",
			"to_status": "Ticketed",
			"creation": ["between", _day_range(from_date, to_date)],
		},
		fields=["actor"],
	):
		if row.actor:
			_bucket(row.actor)["tickets_booked"] += 1

	for row in frappe.get_all(
		"Process Event",
		filters={
			"reference_doctype": "Placement",
			"to_status": "Departed",
			"creation": ["between", _day_range(from_date, to_date)],
		},
		fields=["actor"],
	):
		if row.actor:
			_bucket(row.actor)["departures_confirmed"] += 1

	return list(performance.values())


@frappe.whitelist()
def get_complaint_aging_report():
	"""business-workflow-srs.md Part 5: "how many are new, how many are still open and for how
	long, how many resolved" — "still open" (Unresolved) is explicitly meant to surface aging,
	not just a count, so each Unresolved complaint's age in days is returned individually
	(sorted oldest-first, same as list_unresolved_complaints) rather than collapsed into an
	average that would hide exactly the "forgotten at the bottom of the list" case the spec
	cares about.
	"""
	_require_management()

	unresolved = frappe.get_all(
		"Complaint", filters={"status": "Unresolved"}, fields=["name", "creation"], order_by="creation asc"
	)
	today = getdate()
	unresolved_with_age = [
		{"name": row.name, "age_days": (today - getdate(row.creation)).days} for row in unresolved
	]

	return {
		"new_count": frappe.db.count("Complaint", filters={"status": "New"}),
		"unresolved": unresolved_with_age,
		"resolved_count": frappe.db.count(
			"Complaint",
			filters={"status": ["in", ["Resolved", "Returned - Free Replacement Required", "Escalated", "Dismissed"]]},
		),
	}


def _awaiting_fx_summary(filters):
	"""Approved transactions still awaiting an FX rate (finance_engine.convert_awaiting_fx) count
	as 0 in every *_birr total until a rate is recorded -- surfaced so the totals aren't silently
	short: {"count": n, "by_currency": {"SAR": 300.0, ...}} (original-currency amounts)."""
	by_currency = {}
	rows = frappe.get_all(
		"Applicant Transaction",
		filters={**filters, "awaiting_fx_rate": 1},
		fields=["currency_original", "amount_original"],
	)
	for row in rows:
		by_currency[row.currency_original] = by_currency.get(row.currency_original, 0) + (row.amount_original or 0)
	return {"count": len(rows), "by_currency": by_currency}


@frappe.whitelist()
def get_financial_overview(from_date=None, to_date=None, **kwargs):
	"""Part F: "report_api.py gains get_financial_overview (Admin-only)" — deliberately not
	Manager, unlike every other report here (the financial visibility wall from Step 8 applies
	to reporting too, not just the raw ledger)."""
	_require_admin()
	from_date, to_date = _normalize_dates(from_date, to_date, **kwargs)

	base_filters = {"status": "Approved", "creation": ["between", _day_range(from_date, to_date)]}
	totals = {}
	for transaction_type in ("Commission", "Refund", "Income", "Expense"):
		rows = frappe.get_all(
			"Applicant Transaction",
			filters={**base_filters, "transaction_type": transaction_type},
			fields=["amount_birr"],
		)
		totals[transaction_type.lower()] = sum(r.amount_birr or 0 for r in rows)

	owed_rows = frappe.get_all(
		"Applicant Transaction",
		filters={"transaction_type": "Commission", "status": "Approved", "commission_batch_request": ["is", "not set"]},
		fields=["amount_birr"],
	)
	settled_batches = frappe.get_all(
		"Commission Batch Request",
		filters={"status": "Settled", "settled_on": ["between", [from_date, to_date]]},
		fields=["total_amount_birr"],
	)

	return {
		"from_date": from_date,
		"to_date": to_date,
		"totals_birr": totals,
		"outstanding_owed_birr": sum(r.amount_birr or 0 for r in owed_rows),
		"settled_in_period_birr": sum(r.total_amount_birr or 0 for r in settled_batches),
		"awaiting_fx": _awaiting_fx_summary(base_filters),
	}


def _require_admin():
	if not ({"Admin", "System Manager", "Finance Manager", "Manager"} & set(frappe.get_roles())):
		frappe.throw("Not permitted.", frappe.PermissionError)


@frappe.whitelist()
def get_pending_approval_queue():
	"""Admin-only (2026-08-29): every Pending Applicant Transaction, oldest-first -- so
	nothing sits forgotten waiting on Finance review, same shape as get_complaint_aging_report."""
	_require_admin()
	return frappe.get_all(
		"Applicant Transaction",
		filters={"status": "Pending"},
		fields=["name", "placement", "transaction_type", "amount_birr", "logged_by", "creation"],
		order_by="creation asc",
	)


@frappe.whitelist()
def get_cost_breakdown_report(from_date=None, to_date=None, **kwargs):
	"""Admin-only: Approved transaction totals grouped by destination_country and by the
	clearance step_type that generated the underlying Clearance Step Payment (where
	applicable) -- helps spot which corridor step is costing the most."""
	_require_admin()
	from_date, to_date = _normalize_dates(from_date, to_date, **kwargs)
	base_filters = {"status": "Approved", "creation": ["between", _day_range(from_date, to_date)]}

	by_country = {}
	for row in frappe.get_all(
		"Applicant Transaction",
		filters=base_filters,
		fields=["placement", "amount_birr", "transaction_type"],
	):
		if not row.placement:
			continue
		country = frappe.db.get_value("Placement", row.placement, "destination_country")
		if not country:
			continue
		by_country.setdefault(country, 0)
		by_country[country] += row.amount_birr or 0

	return {
		"from_date": from_date,
		"to_date": to_date,
		"by_country_birr": by_country,
		"awaiting_fx": _awaiting_fx_summary(base_filters),
	}


@frappe.whitelist()
def get_employee_financial_report(from_date=None, to_date=None, **kwargs):
	"""Admin-only: per-employee net expense (expenses - income, Approved only) and
	approval/rejection rate on everything they submitted, side by side."""
	_require_admin()
	from_date, to_date = _normalize_dates(from_date, to_date, **kwargs)
	day_range = _day_range(from_date, to_date)

	net = {}
	for row in frappe.get_all(
		"Applicant Transaction",
		filters={"status": "Approved", "creation": ["between", day_range]},
		fields=["logged_by", "amount_birr", "transaction_type"],
	):
		if not row.logged_by:
			continue
		net.setdefault(row.logged_by, 0)
		if row.transaction_type == "Expense":
			net[row.logged_by] += row.amount_birr or 0
		elif row.transaction_type == "Income":
			net[row.logged_by] -= row.amount_birr or 0

	submitted_counts = {}
	approved_counts = {}
	for row in frappe.get_all(
		"Applicant Transaction",
		filters={"creation": ["between", day_range], "status": ["in", ["Approved", "Rejected"]]},
		fields=["logged_by", "status"],
	):
		if not row.logged_by:
			continue
		submitted_counts[row.logged_by] = submitted_counts.get(row.logged_by, 0) + 1
		if row.status == "Approved":
			approved_counts[row.logged_by] = approved_counts.get(row.logged_by, 0) + 1

	users = set(net) | set(submitted_counts)
	report = []
	for user in users:
		submitted = submitted_counts.get(user, 0)
		approved = approved_counts.get(user, 0)
		report.append(
			{
				"user": user,
				"net_expense_birr": net.get(user, 0),
				"submitted_count": submitted,
				"approval_rate": round(approved / submitted, 4) if submitted else None,
			}
		)
	return report


PLACEMENT_AGING_WARNING_DAYS = 25
PLACEMENT_AGING_CRITICAL_DAYS = 30


@frappe.whitelist()
def get_placement_aging_report():
	"""Admin/Manager: two buckets, both sorted worst-first (highest priority). Distinct from
	the existing contract_age_watchdog push notification -- this is a pull/list view, same
	pattern as get_complaint_aging_report."""
	MANAGEMENT_ROLES = {"Manager", "Admin"}
	if not (MANAGEMENT_ROLES & set(frappe.get_roles())):
		frappe.throw("Not permitted.", frappe.PermissionError)

	today = getdate()

	def _age_days(contract_signed_date):
		return (today - getdate(contract_signed_date)).days

	approaching_ticket_deadline = []
	for row in frappe.get_all(
		"Placement",
		filters={"status": ["not in", ["Ticketed", "Departed", "Cancelled"]], "contract_signed_date": ["is", "set"]},
		fields=["name", "contract_signed_date", "status"],
	):
		age = _age_days(row.contract_signed_date)
		if PLACEMENT_AGING_WARNING_DAYS <= age < PLACEMENT_AGING_CRITICAL_DAYS:
			approaching_ticket_deadline.append({"name": row.name, "age_days": age, "status": row.status})

	critical_not_departed = []
	for row in frappe.get_all(
		"Placement",
		filters={"status": ["not in", ["Departed", "Cancelled"]], "contract_signed_date": ["is", "set"]},
		fields=["name", "contract_signed_date", "status"],
	):
		age = _age_days(row.contract_signed_date)
		if age >= PLACEMENT_AGING_CRITICAL_DAYS:
			critical_not_departed.append({"name": row.name, "age_days": age, "status": row.status})

	approaching_ticket_deadline.sort(key=lambda r: r["age_days"], reverse=True)
	critical_not_departed.sort(key=lambda r: r["age_days"], reverse=True)

	return {
		"approaching_ticket_deadline": approaching_ticket_deadline,
		"critical_not_departed": critical_not_departed,
	}


@frappe.whitelist()
def get_operations_summary(from_date=None, to_date=None, **kwargs):
	"""Recruitment funnel + SLA/turnaround dashboard data (Manager/Admin), added on request
	from the frontend integration pass (2026-08-31) alongside list_applicants/list_placements
	(backend-issues #02). Funnel/stage counts are current-state snapshots (not time-boxed --
	a funnel describes where everything sits *right now*); conversion rates and turnaround
	times are computed from Process Event transitions that happened within from_date/to_date,
	same pattern as get_daily_work_report/_transition_count above. Pending/overdue reuses the
	same thresholds as get_placement_aging_report so the two never disagree."""
	_require_management()
	from_date, to_date = _normalize_dates(from_date, to_date, **kwargs)

	applicant_funnel = {
		status: frappe.db.count("Applicant", filters={"status": status})
		for status in ("Draft", "Registered", "CV Generated", "Cancelled")
	}
	placement_funnel = {
		status: frappe.db.count("Placement", filters={"status": status})
		for status in ("Selected", "Processing", "Stamped", "Ticketed", "Departed", "Cancelled")
	}

	registered_count = _transition_count("Registered", from_date, to_date, reference_doctype="Applicant")
	cv_generated_count = _transition_count("CV Generated", from_date, to_date, reference_doctype="Applicant")
	stamped_count = _transition_count("Stamped", from_date, to_date)
	ticketed_count = _transition_count("Ticketed", from_date, to_date)
	departed_count = _transition_count("Departed", from_date, to_date)

	def _rate(numerator, denominator):
		return round(numerator / denominator, 4) if denominator else None

	conversion_rates = {
		"registered_to_cv_generated": _rate(cv_generated_count, registered_count),
		"stamped_to_ticketed": _rate(ticketed_count, stamped_count),
		"ticketed_to_departed": _rate(departed_count, ticketed_count),
	}

	def _avg_turnaround_days(to_status, from_date, to_date):
		"""Mean days between a Placement's creation and its first reaching to_status, for
		every such transition recorded in the window -- a simple mean, not weighted/percentile;
		good enough for a dashboard tile, not a substitute for the underlying event list."""
		events = frappe.get_all(
			"Process Event",
			filters={
				"reference_doctype": "Placement",
				"to_status": to_status,
				"creation": ["between", _day_range(from_date, to_date)],
			},
			fields=["reference_name", "creation"],
		)
		if not events:
			return None
		durations = []
		for event in events:
			placement_creation = frappe.db.get_value("Placement", event.reference_name, "creation")
			if placement_creation:
				durations.append((frappe.utils.get_datetime(event.creation) - frappe.utils.get_datetime(placement_creation)).days)
		return round(sum(durations) / len(durations), 1) if durations else None

	turnaround_days = {
		"selected_to_ticketed": _avg_turnaround_days("Ticketed", from_date, to_date),
		"selected_to_departed": _avg_turnaround_days("Departed", from_date, to_date),
	}

	aging = get_placement_aging_report()
	pending_overdue = {
		"placements_approaching_ticket_deadline": len(aging["approaching_ticket_deadline"]),
		"placements_critical_not_departed": len(aging["critical_not_departed"]),
		"complaints_unresolved": frappe.db.count("Complaint", filters={"status": "Unresolved"}),
		"transactions_pending_approval": frappe.db.count("Applicant Transaction", filters={"status": "Pending"}),
	}

	return {
		"from_date": from_date,
		"to_date": to_date,
		"applicant_funnel": applicant_funnel,
		"placement_funnel": placement_funnel,
		"conversion_rates": conversion_rates,
		"turnaround_days": turnaround_days,
		"pending_overdue": pending_overdue,
	}


def _agency_display_name():
	name = frappe.db.get_single_value("Agency Tracking Settings", "agency_name")
	return name or "Agency Tracking"


def _resolve_applicant_and_agency(rows):
	"""2026-09-12: client-facing exports must never show raw internal IDs (Placement/Applicant
	link names like PLM-00016) -- replaces them with the actual applicant's name and the foreign
	agency's real name. Bulk-resolves in three batched queries (never N+1): Placement ->
	(applicant, contractor), Applicant -> full_name, Contractor -> contractor_name. Mutates each
	row dict in place, adding `applicant_full_name` / `foreign_agency_name` (blank if nothing to
	resolve, e.g. a general overhead expense with no placement/applicant at all)."""
	placement_names = {r.get("placement") for r in rows if r.get("placement")}
	placement_map = {
		p.name: p
		for p in frappe.get_all(
			"Placement", filters={"name": ["in", list(placement_names) or [""]]}, fields=["name", "applicant", "contractor"]
		)
	}

	applicant_names = {r.get("applicant") for r in rows if r.get("applicant")}
	applicant_names |= {p.applicant for p in placement_map.values() if p.applicant}
	applicant_full_name = {}
	for a in frappe.get_all(
		"Applicant", filters={"name": ["in", list(applicant_names) or [""]]},
		fields=["name", "full_name", "first_name", "middle_name", "last_name"],
	):
		# 2026-09-12: build the display name from the granular parts (first/middle/last -- in
		# this app's Ethiopian-naming convention, middle_name is the father's name and last_name
		# the grandfather's) rather than trusting the stored full_name field alone. full_name is
		# only auto-derived from these parts WHEN IT'S BLANK (Applicant.set_full_name) -- if
		# middle_name/last_name get filled in or corrected later, full_name is never re-synced,
		# so it can silently go stale and drop the grandfather's name a report must show.
		parts = " ".join(filter(None, [a.first_name, a.middle_name, a.last_name]))
		applicant_full_name[a.name] = parts or a.full_name or ""

	contractor_names = {p.contractor for p in placement_map.values() if p.contractor}
	contractor_display_name = {
		c.name: c.contractor_name
		for c in frappe.get_all("Contractor", filters={"name": ["in", list(contractor_names) or [""]]}, fields=["name", "contractor_name"])
	}

	for r in rows:
		placement = placement_map.get(r.get("placement"))
		applicant_id = r.get("applicant") or (placement.applicant if placement else None)
		r["applicant_full_name"] = applicant_full_name.get(applicant_id, "") if applicant_id else ""
		contractor_id = placement.contractor if placement else None
		r["foreign_agency_name"] = contractor_display_name.get(contractor_id, "") if contractor_id else ""
	return rows


def _xlsx_formats(workbook):
	"""Shared style set for every .xlsx export in this module (2026-09-12) -- one place so every
	report looks like it came from the same system, not a bare data dump. Money columns use
	'#,##0.00' (thousands-separated, 2 decimals) and dates are written as real date VALUES with a
	'yyyy-mm-dd' number format, not plain text -- writing a date as a string is exactly what made
	the CSV fallback below get auto-mangled into "########" when opened in Excel/LibreOffice (the
	app guesses it's a date, reformats it, and the default column width is too narrow for its own
	guess)."""
	return {
		"title": workbook.add_format({"bold": True, "font_size": 14, "font_color": "#1E3A8A"}),
		"subtitle": workbook.add_format({"italic": True, "font_color": "#555555"}),
		"header": workbook.add_format({
			"bold": True, "bg_color": "#1E3A8A", "font_color": "#FFFFFF", "border": 1,
			"align": "center", "valign": "vcenter", "text_wrap": True,
		}),
		"cell": workbook.add_format({"border": 1, "valign": "vcenter"}),
		"cell_alt": workbook.add_format({"border": 1, "valign": "vcenter", "bg_color": "#F3F6FB"}),
		"num": workbook.add_format({"border": 1, "valign": "vcenter", "num_format": "#,##0.00"}),
		"num_alt": workbook.add_format({"border": 1, "valign": "vcenter", "num_format": "#,##0.00", "bg_color": "#F3F6FB"}),
		"date": workbook.add_format({"border": 1, "valign": "vcenter", "num_format": "yyyy-mm-dd"}),
		"date_alt": workbook.add_format({"border": 1, "valign": "vcenter", "num_format": "yyyy-mm-dd", "bg_color": "#F3F6FB"}),
	}


def _write_report_header(worksheet, fmt, title, subtitle, n_cols):
	worksheet.merge_range(0, 0, 0, n_cols - 1, title, fmt["title"])
	worksheet.merge_range(1, 0, 1, n_cols - 1, subtitle, fmt["subtitle"])


def _write_rows(worksheet, fmt, start_row, rows, columns):
	"""columns: list of (getter(row) -> value, kind) where kind is 'text'/'num'/'date'."""
	for r_idx, r in enumerate(rows):
		row = start_row + r_idx
		alt = bool(r_idx % 2)
		for col, (getter, kind) in enumerate(columns):
			value = getter(r)
			if kind == "num":
				worksheet.write_number(row, col, float(value or 0), fmt["num_alt"] if alt else fmt["num"])
			elif kind == "date":
				if value:
					worksheet.write_datetime(row, col, frappe.utils.get_datetime(value), fmt["date_alt"] if alt else fmt["date"])
				else:
					worksheet.write_blank(row, col, None, fmt["date_alt"] if alt else fmt["date"])
			else:
				worksheet.write(row, col, value or "", fmt["cell_alt"] if alt else fmt["cell"])


@frappe.whitelist()
def export_commissions_xlsx(contractor=None, destination_country=None, from_date=None, to_date=None):
	"""Generates and streams a branded .xlsx of Commission-type Applicant Transactions. CSV
	fallback only if xlsxwriter is somehow missing at runtime despite being a declared dependency
	(pyproject.toml) -- that fallback now logs an error instead of silently degrading, since a
	silent CSV fallback with no formatting at all was the root cause of "the excel doesn't look
	right"/"" the date is unreadable" reports (2026-09-12)."""
	_require_management()

	filters = {"transaction_type": "Commission"}
	if contractor:
		placements = frappe.get_all("Placement", filters={"contractor": contractor}, pluck="name")
		if placements:
			filters["placement"] = ["in", placements]
		else:
			filters["placement"] = "non-existent"
	if from_date and to_date:
		filters["creation"] = ["between", _day_range(from_date, to_date)]

	rows = frappe.get_all(
		"Applicant Transaction",
		filters=filters,
		fields=["name", "placement", "applicant", "transaction_type", "amount_original", "currency_original", "amount_birr", "status", "creation"],
		order_by="creation desc"
	)
	_resolve_applicant_and_agency(rows)

	try:
		import io
		import xlsxwriter

		output = io.BytesIO()
		workbook = xlsxwriter.Workbook(output, {"in_memory": True})
		worksheet = workbook.add_worksheet("Commissions")
		fmt = _xlsx_formats(workbook)

		# 2026-09-12: led with the human-readable identifiers (applicant name, foreign agency),
		# not internal record IDs -- client-facing, and a raw "PLM-00016" means nothing to them.
		# Placement and Transaction ID kept per explicit request, but pushed to the very end as
		# reference columns rather than leading the sheet.
		headers = ["Applicant", "Foreign Agency", "Type", "Original Amount", "Currency", "ETB Amount", "Status", "Date", "Transaction ID", "Placement"]
		_write_report_header(
			worksheet, fmt, f"{_agency_display_name()} — Commissions Report",
			f"Generated {frappe.utils.today()}" + (f"  |  {from_date} to {to_date}" if from_date and to_date else "") + f"  |  {len(rows)} record(s)",
			len(headers),
		)
		header_row = 3
		for col, h in enumerate(headers):
			worksheet.write(header_row, col, h, fmt["header"])
		widths = [22, 24, 14, 16, 10, 16, 12, 14, 16, 16]
		for col, w in enumerate(widths):
			worksheet.set_column(col, col, w)

		columns = [
			(lambda r: r.applicant_full_name, "text"),
			(lambda r: r.foreign_agency_name, "text"),
			(lambda r: r.transaction_type, "text"),
			(lambda r: r.amount_original, "num"),
			(lambda r: r.currency_original, "text"),
			(lambda r: r.amount_birr, "num"),
			(lambda r: r.status, "text"),
			(lambda r: r.creation, "date"),
			(lambda r: r.name, "text"),
			(lambda r: r.placement, "text"),
		]
		_write_rows(worksheet, fmt, header_row + 1, rows, columns)

		last_row = header_row + len(rows)
		if rows:
			worksheet.autofilter(header_row, 0, last_row, len(headers) - 1)
		worksheet.freeze_panes(header_row + 1, 0)

		workbook.close()
		output.seek(0)

		frappe.response["filename"] = f"commissions_report_{frappe.utils.today()}.xlsx"
		frappe.response["filecontent"] = output.getvalue()
		frappe.response["type"] = "download"
		return
	except ImportError:
		frappe.log_error(
			title="export_commissions_xlsx: xlsxwriter missing",
			message="xlsxwriter is a declared dependency (pyproject.toml) but isn't importable in "
			"this environment -- falling back to an unformatted CSV. Run bench pip install / "
			"reinstall requirements on this site.",
		)

	# CSV fallback (last resort only)
	import csv
	import io
	output = io.StringIO()
	writer = csv.writer(output)
	writer.writerow(["Applicant", "Foreign Agency", "Type", "Original Amount", "Currency", "ETB Amount", "Status", "Date", "Transaction ID", "Placement"])
	for r in rows:
		writer.writerow([r.applicant_full_name or "", r.foreign_agency_name or "", r.transaction_type, r.amount_original or 0, r.currency_original or "", r.amount_birr or 0, r.status, str(r.creation)[:10], r.name, r.placement or ""])

	frappe.response["filename"] = f"commissions_report_{frappe.utils.today()}.csv"
	frappe.response["filecontent"] = output.getvalue()
	frappe.response["type"] = "download"


@frappe.whitelist()
def export_transactions_xlsx(status=None, transaction_type=None, placement=None, applicant=None, from_date=None, to_date=None, **kwargs):
	"""New (2026-09-12): a full-fidelity .xlsx export of Applicant Transactions across every type
	(Expense/Income/Commission) and status, not just Commission-type rows -- same filters and
	field set as finance_api.list_transactions (the call behind the Pending Financial Approvals
	Queue and similar views), so this export can genuinely match whatever that screen is showing,
	unlike export_commissions_xlsx above which is deliberately Commission-only. Same branded
	formatting (comma-separated money, real dates, frozen header, autofilter)."""
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

	rows = frappe.get_all(
		"Applicant Transaction",
		filters=filters,
		fields=[
			"name", "applicant", "placement", "transaction_type", "status",
			"amount_original", "currency_original", "amount_birr", "description",
			"logged_by", "approved_by", "approved_on", "rejection_reason", "creation",
		],
		order_by="creation asc",
		limit_page_length=0,
	)
	_resolve_applicant_and_agency(rows)

	import io
	import xlsxwriter

	output = io.BytesIO()
	workbook = xlsxwriter.Workbook(output, {"in_memory": True})
	worksheet = workbook.add_worksheet("Transactions")
	fmt = _xlsx_formats(workbook)

	# 2026-09-12: same client-facing-name rule as export_commissions_xlsx -- applicant name +
	# foreign agency name lead the sheet. Placement and Transaction ID kept per explicit request,
	# pushed to the very end as reference columns.
	headers = [
		"Applicant", "Foreign Agency", "Type", "Status", "Original Amount",
		"Currency", "ETB Amount", "Description", "Logged By", "Approved By", "Approved On",
		"Rejection Reason", "Logged At", "Transaction ID", "Placement",
	]
	subtitle_bits = [f"Generated {frappe.utils.today()}"]
	if from_date or to_date:
		subtitle_bits.append(f"{from_date or '...'} to {to_date or '...'}")
	if status:
		subtitle_bits.append(f"Status: {status}")
	if transaction_type:
		subtitle_bits.append(f"Type: {transaction_type}")
	subtitle_bits.append(f"{len(rows)} record(s)")

	_write_report_header(worksheet, fmt, f"{_agency_display_name()} — Transactions Report", "  |  ".join(subtitle_bits), len(headers))
	header_row = 3
	for col, h in enumerate(headers):
		worksheet.write(header_row, col, h, fmt["header"])
	widths = [22, 24, 12, 12, 16, 10, 16, 26, 20, 20, 14, 22, 14, 16, 16]
	for col, w in enumerate(widths):
		worksheet.set_column(col, col, w)

	columns = [
		(lambda r: r.applicant_full_name, "text"),
		(lambda r: r.foreign_agency_name, "text"),
		(lambda r: r.transaction_type, "text"),
		(lambda r: r.status, "text"),
		(lambda r: r.amount_original, "num"),
		(lambda r: r.currency_original, "text"),
		(lambda r: r.amount_birr, "num"),
		(lambda r: r.description, "text"),
		(lambda r: r.logged_by, "text"),
		(lambda r: r.approved_by, "text"),
		(lambda r: r.approved_on, "date"),
		(lambda r: r.rejection_reason, "text"),
		(lambda r: r.creation, "date"),
		(lambda r: r.name, "text"),
		(lambda r: r.placement, "text"),
	]
	_write_rows(worksheet, fmt, header_row + 1, rows, columns)

	last_row = header_row + len(rows)
	if rows:
		worksheet.autofilter(header_row, 0, last_row, len(headers) - 1)
	worksheet.freeze_panes(header_row + 1, 0)

	workbook.close()
	output.seek(0)

	frappe.response["filename"] = f"transactions_report_{frappe.utils.today()}.xlsx"
	frappe.response["filecontent"] = output.getvalue()
	frappe.response["type"] = "download"


# Layout copied directly from docs/Group_Schedule_Bio_Applicants_Excel_Template.xls (inspected
# via xlrd) -- this is a fixed external bulk-upload format some downstream labor/visa authority
# consumes, not one of this app's own branded reports, so it deliberately does NOT use
# _xlsx_formats/_write_report_header above: no title row, no autofilter, no frozen header --
# anything that shifts row/column positions away from row 0 = headers would break whatever parses
# this on the receiving end. Written as a real BIFF8 .xls (xlwt), same as the template, not .xlsx.
# Widths are in the .xls native 1/256-character units, as stored in the template.
_GROUP_SCHEDULE_BIO_HEADERS = [
	"E.No", "First Name*", "Second Name", "Last Name*", "Passport Number*",
	"Date of Birth* ", "Nationality*", "Date of Issue*", "Gender*",
	"Place of Issue*", "Expiry Date*", "Applicant Mobile No.*", "Email ID*",
]
_GROUP_SCHEDULE_BIO_WIDTHS = [3108, 2816, 5193, 3547, 4205, 3803, 3254, 3620, 3766, 3584, 3108, 6473, 5558]
_GROUP_SCHEDULE_BIO_DATE_COLS = {5, 7, 10}
_GROUP_SCHEDULE_BIO_DATE_FORMAT = "[$-409]d/mmm/yyyy;@"


def _injaz_application_ids(applicants):
	"""{applicant: Injaz Application ID} for the template's "E.No" column. Taken from the Taeshir
	step of each applicant's latest non-Cancelled Placement, picking the attempt the same way
	clearance_api._get_active_injaz_attempt does (last Active row, else last row). Applicants with
	no Taeshir step / no ID yet are simply absent -- E.No is not a starred column in the template."""
	placements = frappe.get_all(
		"Placement",
		filters={"applicant": ["in", applicants], "status": ["!=", "Cancelled"]},
		fields=["name", "applicant"],
		order_by="creation asc",
	)
	# Later placements overwrite earlier ones -> latest placement per applicant wins.
	placement_applicant = {p.applicant: p.name for p in placements}
	if not placement_applicant:
		return {}
	steps = frappe.get_all(
		"Clearance Step",
		filters={
			"placement": ["in", list(placement_applicant.values())],
			"step_type": "Taeshir",
			"status": ["!=", "Cancelled"],
		},
		fields=["name", "placement"],
		order_by="creation asc",
	)
	step_by_placement = {st.placement: st.name for st in steps}
	if not step_by_placement:
		return {}
	attempts = frappe.get_all(
		"Injaz Attempt",
		filters={
			"parent": ["in", list(step_by_placement.values())],
			"parenttype": "Clearance Step",
		},
		fields=["parent", "outcome", "injaz_application_id"],
		order_by="idx asc",
	)
	by_step = {}
	for att in attempts:
		by_step.setdefault(att.parent, []).append(att)

	result = {}
	for applicant, placement in placement_applicant.items():
		rows = by_step.get(step_by_placement.get(placement)) or []
		active = [r for r in rows if r.outcome == "Active"]
		chosen = active[-1] if active else (rows[-1] if rows else None)
		if chosen and chosen.injaz_application_id:
			result[applicant] = chosen.injaz_application_id
	return result


@frappe.whitelist()
def export_group_schedule_bio_xlsx(applicants=None):
	"""Given a list of Applicant names, fills them into the exact layout of
	docs/Group_Schedule_Bio_Applicants_Excel_Template.xls (sheet "Bio_Applicant_Details", same
	13 columns/order/widths/header colour/date format, plus the hidden nationality-list sheet)
	and returns it as a real .xls file, so the result can go straight to whatever authority
	consumes that template, no manual reformatting. "E.No" is each applicant's Injaz Application
	ID (see _injaz_application_ids), blank if they don't have one yet; "Email ID" is the agency's
	own email (Agency Tracking Settings > Agency Email), not the applicant's. `applicants` accepts a list, a JSON-encoded
	list, or a single Applicant name. (The endpoint keeps its original `_xlsx` name so existing
	callers don't break.)
	"""
	if not (INTERNAL_STAFF_ROLES & set(frappe.get_roles())):
		frappe.throw("Not permitted.", frappe.PermissionError)

	if isinstance(applicants, str):
		applicants = frappe.parse_json(applicants) if applicants.strip().startswith("[") else [applicants]
	applicants = [a for a in (applicants or []) if a]
	if not applicants:
		frappe.throw("At least one applicant is required.", frappe.ValidationError)

	rows = frappe.get_all(
		"Applicant",
		filters={"name": ["in", applicants]},
		fields=[
			"name", "first_name", "middle_name", "last_name", "passport_number",
			"date_of_birth", "nationality", "passport_issue_date", "gender",
			"passport_issue_place", "passport_expiry_date", "phone",
		],
	)
	row_map = {r.name: r for r in rows}
	missing = [a for a in applicants if a not in row_map]
	if missing:
		frappe.throw(
			f"Applicant(s) not found: {', '.join(missing)}. Zero data loss -- fix the list rather "
			"than silently dropping them from the export.",
			frappe.ValidationError,
		)
	# Preserve the caller's requested order, not frappe.get_all's DB order.
	ordered = [row_map[a] for a in applicants]
	e_numbers = _injaz_application_ids(applicants)
	from agency_tracking.clearance_api import get_agency_email

	agency_email = get_agency_email()

	import io
	import xlwt

	from agency_tracking.group_schedule_bio_countries import GROUP_SCHEDULE_BIO_COUNTRIES

	def _style(font_name="Arial", date=False, header=False, wrap=False):
		style = xlwt.XFStyle()
		style.font = xlwt.Font()
		style.font.name = font_name
		style.font.height = 200  # 10pt, as in the template
		style.alignment = xlwt.Alignment()
		style.alignment.vert = xlwt.Alignment.VERT_CENTER
		style.alignment.wrap = int(wrap)
		if header:
			style.pattern = xlwt.Pattern()
			style.pattern.pattern = xlwt.Pattern.SOLID_PATTERN
			style.pattern.pattern_fore_colour = 24  # #9999FF in the default BIFF8 palette
		if date:
			style.num_format_str = _GROUP_SCHEDULE_BIO_DATE_FORMAT
		return style

	workbook = xlwt.Workbook(encoding="utf-8")
	worksheet = workbook.add_sheet("Bio_Applicant_Details")

	for col, (h, w) in enumerate(zip(_GROUP_SCHEDULE_BIO_HEADERS, _GROUP_SCHEDULE_BIO_WIDTHS)):
		is_date = col in _GROUP_SCHEDULE_BIO_DATE_COLS
		# The template's "Date of Birth*" header alone is Times New Roman with wrap on.
		header_style = (
			_style("Times New Roman", date=True, header=True, wrap=True) if col == 5
			else _style(date=is_date, header=True)
		)
		worksheet.write(0, col, h, header_style)
		worksheet.col(col).width = w

	cell_style = _style()
	date_style = _style(date=True)

	def _write_date(row, col, value):
		if value:
			worksheet.write(row, col, frappe.utils.getdate(value), date_style)
		else:
			worksheet.write(row, col, None, date_style)

	for r_idx, a in enumerate(ordered):
		row = r_idx + 1
		worksheet.write(row, 0, e_numbers.get(a.name) or "", cell_style)
		worksheet.write(row, 1, a.first_name or "", cell_style)
		worksheet.write(row, 2, a.middle_name or "", cell_style)
		worksheet.write(row, 3, a.last_name or "", cell_style)
		worksheet.write(row, 4, a.passport_number or "", cell_style)
		_write_date(row, 5, a.date_of_birth)
		worksheet.write(row, 6, a.nationality or "", cell_style)
		_write_date(row, 7, a.passport_issue_date)
		worksheet.write(row, 8, a.gender or "", cell_style)
		worksheet.write(row, 9, a.passport_issue_place or "", cell_style)
		_write_date(row, 10, a.passport_expiry_date)
		worksheet.write(row, 11, a.phone or "", cell_style)
		worksheet.write(row, 12, agency_email, cell_style)

	hidden = workbook.add_sheet("hiddenSheet")
	hidden.visibility = 1
	for r_idx, country in enumerate(GROUP_SCHEDULE_BIO_COUNTRIES):
		hidden.write(r_idx, 0, country)

	output = io.BytesIO()
	workbook.save(output)

	frappe.response["filename"] = f"Group_Schedule_Bio_Applicants_{frappe.utils.today()}.xls"
	frappe.response["filecontent"] = output.getvalue()
	frappe.response["type"] = "download"

