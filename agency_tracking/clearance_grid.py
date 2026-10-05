# Copyright (c) 2026, Agency and contributors
# License: MIT. See LICENSE
#
# Excel-like editing of clearance steps (client item #11, 2026-09-23). One grid per step_type:
#   list_clearance_grid         rows for one step_type: step + applicant + current Injaz attempt
#   get_clearance_grid_columns  the columns, and which ones the caller may edit
#   save_clearance_grid         many rows' changed cells in one call
#
# Saving a cell never writes the field directly when a real action exists for it: each cell goes
# through the same clearance_api / applicant_api function as its button (status -> start/complete/
# submit/stamp/reject, Wakala -> record_wakala_payment, Injaz -> record_injaz_payment, ...), so fees,
# gates, notifications and the audit trail behave exactly as if the officer had clicked it. Moving
# a step BACKWARDS (reopen) is never a cell edit -- it stays reopen_clearance_step with its reason.
#
# Each row is its own unit: all its cells apply or none do (savepoint), and one failing row never
# blocks the others. A row whose step changed since the caller loaded it (`modified` mismatch) is
# refused, not overwritten.

import frappe
from frappe.utils import cint, get_datetime

from agency_tracking import clearance_api
from agency_tracking.agency_tracking.doctype.clearance_step.clearance_step import CLEARANCE_ROLE_BY_STEP_TYPE
from agency_tracking.pagination import count_rows, page_args, paged_result, require_list_permission
from agency_tracking.state_machine import CLEARANCE_STEP_DONE_STATUSES
from agency_tracking.db_errors import reraise_if_db_abort

MANAGEMENT = {"Manager", "Admin", "System Manager"}
LMIS_APPLICANT_ROLES = {"Saudi LMIS", "Kuwait LMIS", "Manager", "Admin"}
EMBASSY_TYPES = ("Embassy", "Kuwait Embassy")
LMIS_TYPES = ("LMIS Clearance", "Kuwait LMIS")
MAX_ROWS_PER_SAVE = 200

# Forward-only status order per step_type (what a cell may move a step TO).
STATUS_FLOW = {
	"LMIS Clearance": ["Pending", "In Progress", "Issued"],
	"Kuwait LMIS": ["Pending", "In Progress", "Issued"],
	"Taeshir": ["Pending", "In Progress", "Complete"],
	"Telesign": ["Pending", "In Progress", "Complete"],
	"Embassy": ["Pending", "In Progress", "Submitted", "Stamped", "Rejected"],
	"Kuwait Embassy": ["Pending", "In Progress", "Submitted", "Stamped", "Rejected"],
}

_READ_ONLY_COLUMNS = [
	("name", "Step", "Data"),
	("placement", "Placement", "Data"),
	("applicant", "Applicant", "Data"),
	("full_name", "Name", "Data"),
	("passport_number", "Passport No", "Data"),
	("destination_country", "Country", "Data"),
	("placement_status", "Placement Status", "Data"),
	("date_started", "Started", "Date"),
	("completed_by", "Completed By", "Data"),
]
_LMIS_APPLICANT_COLUMNS = [
	("national_id", "National ID", "Data", None),
	("labor_id", "Labor ID", "Data", None),
	("coc_status", "COC Status", "Select", ["", "Pending", "Issued", "Not Started"]),
	("exam_date", "COC Exam Date", "Date", None),
	("emergency_contact_name", "Emergency Contact", "Data", None),
	("emergency_contact_phone", "Emergency Phone", "Data", None),
	("emergency_contact_address", "Emergency Address", "Data", None),
]
_APPLICANT_FIELDS = [c[0] for c in _LMIS_APPLICANT_COLUMNS]


def _editable_columns(step_type):
	"""[(field, label, type, options)] editable on this step_type's grid (before the per-user check)."""
	cols = [
		("status", "Status", "Select", STATUS_FLOW[step_type]),
		("reference_no", "Reference No", "Data", None),
		("date_completed", "Completed On", "Date", None),
	]
	if step_type in LMIS_TYPES:
		cols += _LMIS_APPLICANT_COLUMNS
	if step_type == "Kuwait LMIS":
		cols += [
			("police_ashara_status", "Police Ashara", "Select", ["Pending", "Scheduled", "Completed", "Failed"]),
			("police_ashara_appointment_date", "Police Ashara Date", "Date", None),
			("police_ashara_remark", "Police Ashara Remark", "Data", None),
		]
	if step_type == "Taeshir":
		cols += [
			("injaz_application_id", "Injaz Application ID", "Data", None),
			("appointment_date", "Appointment", "Date", None),
			("injaz_payment_status", "Injaz Paid", "Select", ["Unpaid", "Paid"]),
			("injaz_paid_date", "Injaz Paid On", "Date", None),
		]
	if step_type == "Embassy":
		cols += [
			("wakala_status", "Wakala", "Select", ["Pending", "Paid"]),
			("wakala_reference_no", "Musaned No", "Data", None),
		]
	if step_type in EMBASSY_TYPES:
		cols.append(("rejection_remark", "Rejection Remark", "Data", None))
	return cols


def _require_step_type(step_type):
	if step_type not in STATUS_FLOW:
		frappe.throw(f"step_type must be one of {', '.join(STATUS_FLOW)}.", frappe.ValidationError)


def _user_can_edit_step_type(step_type, roles):
	"""Grid-level answer (the per-row gate is still clearance_api._can_act_on_step, run by every
	action): management, the role mapped to this step_type, or a Clearance Officer (ToDo-assigned
	rows only -- a row they aren't assigned to is refused on save)."""
	return bool(MANAGEMENT & roles) or CLEARANCE_ROLE_BY_STEP_TYPE.get(step_type) in roles or "Clearance Officer" in roles


def _require_grid_editor():
	"""Endpoint gate for saving: refuse anyone who can't edit any step_type's grid. Rows are still
	checked one by one (P6-04: agencies and no-role users reached the save before this)."""
	roles = set(frappe.get_roles())
	if not any(_user_can_edit_step_type(t, roles) for t in STATUS_FLOW):
		frappe.throw("Not permitted.", frappe.PermissionError)


@frappe.whitelist()
def get_clearance_grid_columns(step_type=None, **kwargs):
	"""Columns for one step_type's grid: {field, label, type, options, editable}. `editable` is for
	the calling user; a status column's options are forward moves only (reopen is a separate
	action). Columns are the same for everyone, only `editable` differs."""
	_require_step_type(step_type)
	roles = set(frappe.get_roles())
	can_edit = _user_can_edit_step_type(step_type, roles)
	can_edit_applicant = bool(LMIS_APPLICANT_ROLES & roles)
	columns = [
		{"field": f, "label": label, "type": t, "options": None, "editable": False}
		for f, label, t in _READ_ONLY_COLUMNS
	]
	for f, label, t, options in _editable_columns(step_type):
		editable = can_edit and (can_edit_applicant if f in _APPLICANT_FIELDS else True)
		columns.append({"field": f, "label": label, "type": t, "options": options, "editable": editable})
	return columns


def _active_attempts(step_names):
	"""{step name: current Injaz attempt} -- the Active one, else the last one (same rule as
	clearance_api._get_active_injaz_attempt), in one query."""
	if not step_names:
		return {}
	by_step = {}
	for a in frappe.get_all(
		"Injaz Attempt",
		filters={"parent": ["in", step_names], "parenttype": "Clearance Step"},
		fields=["parent", "outcome", "injaz_application_id", "appointment_date", "payment_status", "paid_date", "idx"],
		order_by="idx asc",
	):
		current = by_step.get(a.parent)
		if current is None or current.outcome != "Active" or a.outcome == "Active":
			by_step[a.parent] = a
	return by_step


def _grid_rows(steps):
	"""Attach placement, applicant and (Taeshir) Injaz columns to Clearance Step rows."""
	placement_names = list({s.placement for s in steps if s.placement})
	placements = {
		p.name: p
		for p in frappe.get_all(
			"Placement", filters={"name": ["in", placement_names]}, fields=["name", "applicant", "status", "destination_country"]
		)
	} if placement_names else {}
	applicant_names = list({p.applicant for p in placements.values() if p.applicant})
	applicants = {
		a.name: a
		for a in frappe.get_all(
			"Applicant",
			filters={"name": ["in", applicant_names]},
			fields=["name", "full_name", "passport_number", "modified"] + _APPLICANT_FIELDS,
		)
	} if applicant_names else {}
	attempts = _active_attempts([s.name for s in steps if s.step_type == "Taeshir"])
	rows = []
	for s in steps:
		p = placements.get(s.placement) or frappe._dict()
		a = applicants.get(p.applicant) or frappe._dict()
		row = dict(s)
		row.update(
			{
				"applicant": p.applicant,
				"placement_status": p.status,
				"destination_country": p.destination_country,
				"full_name": a.full_name,
				"passport_number": a.passport_number,
				"applicant_modified": a.modified,
			}
		)
		if s.step_type in LMIS_TYPES:
			row.update({f: a.get(f) for f in _APPLICANT_FIELDS})
		if s.step_type == "Taeshir":
			att = attempts.get(s.name) or frappe._dict()
			row.update(
				{
					"injaz_application_id": att.injaz_application_id,
					"appointment_date": att.appointment_date,
					"injaz_payment_status": att.payment_status,
					"injaz_paid_date": att.paid_date,
					"injaz_outcome": att.outcome,
				}
			)
		rows.append(row)
	return rows


_STEP_FIELDS = [
	"name", "placement", "step_type", "status", "sequence_order", "is_mandatory", "date_started",
	"date_completed", "completed_by", "reference_no", "rejection_remark", "wakala_status",
	"wakala_reference_no", "police_ashara_status", "police_ashara_appointment_date",
	"police_ashara_remark", "modified",
]


def _step_filters(step_type, status, include_closed, search):
	filters = {"step_type": step_type}
	if status:
		filters["status"] = ["in", frappe.parse_json(status)] if str(status).startswith("[") else status
	placement_filter = {}
	if not cint(include_closed):
		placement_filter["status"] = ["not in", ["Departed", "Cancelled"]]
	if search:
		like = f"%{search}%"
		matches = frappe.get_all(
			"Applicant", or_filters={"full_name": ["like", like], "passport_number": ["like", like], "name": ["like", like]}, pluck="name"
		)
		placement_filter["applicant"] = ["in", matches or [""]]
	if placement_filter:
		filters["placement"] = ["in", frappe.get_all("Placement", filters=placement_filter, pluck="name") or [""]]
	return filters


@frappe.whitelist()
def list_clearance_grid(
	step_type=None, status=None, search=None, include_closed=0, limit_start=None, limit_page_length=None, with_total=0, **kwargs
):
	"""Rows for one step_type's grid, oldest first. status: one status or a JSON list; search: applicant
	name / passport / APP id; include_closed=1 also shows steps of Departed/Cancelled placements
	(read-only history). Same row scoping as every other Clearance Step list (role -> step_type,
	Clearance Officer -> ToDo-assigned rows). Each row carries `modified` (and `applicant_modified`),
	which save_clearance_grid needs back to detect a row changed by someone else meanwhile."""
	require_list_permission("Clearance Step")
	_require_step_type(step_type)
	filters = _step_filters(step_type, status, include_closed, search)
	start, length = page_args(limit_start, limit_page_length)
	steps = frappe.get_list(
		"Clearance Step", filters=filters, fields=_STEP_FIELDS, order_by="creation asc", limit_start=start, limit_page_length=length
	)
	rows = _grid_rows(steps)
	return paged_result(rows, with_total, lambda: count_rows("Clearance Step", filters))


# --- saving ---


def _apply_status(step, target, fields, override_reason):
	"""Move `step` to `target` through the matching action. Forward moves only."""
	flow = STATUS_FLOW[step.step_type]
	current = step.status
	if target == current:
		return
	if target not in flow:
		frappe.throw(f"'{target}' isn't a status of a {step.step_type} step.", frappe.ValidationError)
	if current in flow and flow.index(target) < flow.index(current) or current == "Rejected":
		frappe.throw(
			f"Moving a '{current}' step back to '{target}' is a reopen -- use Reopen (with a reason), not the grid.",
			frappe.ValidationError,
		)
	name = step.name
	if target == "In Progress":
		clearance_api.start_clearance_step(name)
	elif target == "Submitted":
		if current == "Pending":
			clearance_api.start_clearance_step(name)
		clearance_api.submit_embassy_step(name, override_reason=override_reason)
	elif target == "Stamped":
		clearance_api.stamp_embassy_step(name, reference_no=fields.get("reference_no"), override_reason=override_reason)
	elif target == "Rejected":
		# The remark must come with this save -- never reuse one left on the step from an earlier cycle.
		clearance_api.reject_embassy_step(name, rejection_remark=fields.get("rejection_remark"))
	else:  # Issued / Complete
		clearance_api.complete_clearance_step(
			name, reference_no=fields.get("reference_no"), date_completed=fields.get("date_completed")
		)


def _apply_data_corrections(step, fields):
	"""reference_no / date_completed / rejection_remark changed without a status change."""
	ref, done_on, remark = fields.get("reference_no"), fields.get("date_completed"), fields.get("rejection_remark")
	if remark is not None and step.status == "Rejected":
		clearance_api.reject_embassy_step(step.name, rejection_remark=remark)
	elif remark is not None:
		frappe.throw("A rejection remark only applies to a Rejected step.", frappe.ValidationError)
	if ref is None and done_on is None:
		return
	if step.step_type in EMBASSY_TYPES and done_on is not None:
		frappe.throw("An Embassy step's completion date is set when it's Stamped/Rejected and can't be edited here.", frappe.ValidationError)
	if step.status == "Stamped":
		clearance_api.stamp_embassy_step(step.name, reference_no=ref)
	elif step.status in CLEARANCE_STEP_DONE_STATUSES and step.step_type not in EMBASSY_TYPES:
		clearance_api.complete_clearance_step(step.name, reference_no=ref, date_completed=done_on)
	elif done_on is not None:
		frappe.throw("Completed On can only be corrected on a completed step (or set together with its status).", frappe.ValidationError)
	else:
		# Not done yet: the reference number is plain data until completion.
		doc = clearance_api._load_actionable_step(step.name)
		if not clearance_api._can_act_on_step(doc):
			frappe.throw("Not permitted.", frappe.PermissionError)
		doc.reference_no = ref
		doc.save(ignore_permissions=True)
		clearance_api.log_action("Clearance Step", doc.name, f"[{doc.title or doc.name}] Reference no set: {ref or '-'} (grid)")


def _apply_row(step, fields, override_reason, cell):
	"""Apply one row's changed cells in dependency order: data the status gates read (Injaz paid,
	Wakala paid, remarks) first, the status move last. Raises on the first failure."""
	name = step.name
	applicant_updates = {k: fields[k] for k in _APPLICANT_FIELDS if k in fields}
	if applicant_updates:
		cell.append(next(iter(applicant_updates)))
		if step.step_type not in LMIS_TYPES:
			frappe.throw(f"{', '.join(applicant_updates)} can only be edited on an LMIS grid.", frappe.ValidationError)
		from agency_tracking.applicant_api import update_applicant_for_lmis

		clearance_api._load_actionable_step(name)  # closed placements are read-only here too
		update_applicant_for_lmis(frappe.db.get_value("Placement", step.placement, "applicant"), **applicant_updates)

	if step.step_type == "Taeshir":
		if "injaz_application_id" in fields or "appointment_date" in fields:
			cell.append("appointment_date" if "appointment_date" in fields else "injaz_application_id")
			clearance_api.set_taeshir_appointment(
				name, appointment_date=fields.get("appointment_date"), injaz_application_id=fields.get("injaz_application_id")
			)
		if "injaz_payment_status" in fields or "injaz_paid_date" in fields:
			cell.append("injaz_payment_status")
			clearance_api.record_injaz_payment(
				name, payment_status=fields.get("injaz_payment_status") or "Paid", paid_date=fields.get("injaz_paid_date")
			)
	if step.step_type == "Embassy" and ("wakala_status" in fields or "wakala_reference_no" in fields):
		cell.append("wakala_status" if "wakala_status" in fields else "wakala_reference_no")
		clearance_api.record_wakala_payment(
			name, wakala_status=fields.get("wakala_status"), reference_no=fields.get("wakala_reference_no")
		)
	if step.step_type == "Kuwait LMIS" and any(k.startswith("police_ashara_") for k in fields):
		cell.append(next(k for k in fields if k.startswith("police_ashara_")))
		clearance_api.record_police_ashara(
			name,
			police_ashara_status=fields.get("police_ashara_status"),
			police_ashara_appointment_date=fields.get("police_ashara_appointment_date"),
			police_ashara_remark=fields.get("police_ashara_remark"),
		)

	step.reload()
	if "status" in fields and fields["status"] != step.status:
		cell.append("status")
		_apply_status(step, fields["status"], fields, override_reason)
	else:
		cell.append(next((k for k in ("reference_no", "date_completed", "rejection_remark") if k in fields), None))
		_apply_data_corrections(step, fields)


def _allowed_fields(step_type):
	return {f for f, *_ in _editable_columns(step_type)}


@frappe.whitelist()
def save_clearance_grid(changes=None, **kwargs):
	"""changes: [{"name": <Clearance Step>, "modified": <row's modified as loaded>,
	              "fields": {<column>: <new value>, ...}, "override_reason": <optional>}]
	Only changed cells need sending. Returns one result per row, in order:
	  {"name", "ok": true,  "row": <fresh grid row>}
	  {"name", "ok": false, "error", "field", "conflict": bool, "row": <current grid row>}
	A row is all-or-nothing; rows are independent. conflict=true means someone else saved that row
	after it was loaded -- reload it and re-apply. override_reason is the Manager/Admin override for
	submitting/stamping an Embassy step with Wakala unpaid (same rule as the buttons)."""
	_require_grid_editor()
	changes = frappe.parse_json(changes) if isinstance(changes, str) else (changes or [])
	if not isinstance(changes, list) or not changes:
		frappe.throw("changes must be a non-empty list.", frappe.ValidationError)
	if len(changes) > MAX_ROWS_PER_SAVE:
		frappe.throw(f"At most {MAX_ROWS_PER_SAVE} rows per save.", frappe.ValidationError)

	results = []
	for change in changes:
		name = (change or {}).get("name")
		fields = dict((change or {}).get("fields") or {})
		result = {"name": name, "ok": False}
		if not name or not frappe.db.exists("Clearance Step", name):
			result["error"] = f"Clearance Step {name} not found."
			results.append(result)
			continue
		step = frappe.get_doc("Clearance Step", name)
		unknown = set(fields) - _allowed_fields(step.step_type)
		loaded = change.get("modified")
		save_point = f"sp_{frappe.generate_hash(length=10)}"
		cell = []
		frappe.db.savepoint(save_point)
		try:
			if unknown:
				frappe.throw(f"Not editable here: {', '.join(sorted(unknown))}.", frappe.ValidationError)
			if not step.has_permission("read"):
				frappe.throw("Not permitted.", frappe.PermissionError)
			if loaded and get_datetime(loaded) != get_datetime(step.modified):
				result["conflict"] = True
				frappe.throw(
					f"Changed by {step.modified_by} at {step.modified} after you loaded it -- reload this row.",
					frappe.ValidationError,
				)
			_apply_row(step, fields, change.get("override_reason"), cell)
			result["ok"] = True
		except Exception as e:
			reraise_if_db_abort(e)
			frappe.db.rollback(save_point=save_point)
			frappe.clear_last_message()
			result["error"] = str(e) or type(e).__name__
			result["field"] = cell[-1] if cell else None
		# Only a caller who may read the step gets it back -- a refused agency / no-role caller used
		# to receive the whole row (name, passport, national ID, emergency contacts) (QA P6-04).
		if not step.has_permission("read"):
			result["row"] = None
			results.append(result)
			continue
		fresh = frappe.get_all("Clearance Step", filters={"name": name}, fields=_STEP_FIELDS)
		result["row"] = _grid_rows(fresh)[0] if fresh else None
		results.append(result)
	return results
