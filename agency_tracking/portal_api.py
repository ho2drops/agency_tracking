# Copyright (c) 2026, Agency and contributors
# License: MIT. See LICENSE
#
# Part F: module-scoped whitelisted functions, no raw /api/resource/* exposure. Foreign Agency
# users get NO direct doctype permissions on Applicant/Placement/Contractor (see the doctype
# JSONs) — every bit of portal access is mediated here, with its own explicit role/ownership
# checks, per Part G ("Foreign Agency (portal): Own country's catalog, own placements...").

import frappe

from agency_tracking.pagination import count_rows, page_args, paged_result
from agency_tracking.state_machine import lock_applicant_row

# Non-PII browsing fields only (business names/skills, not passport/national ID/phone/address/
# emergency contacts) — the spec doesn't enumerate an exact portal field list, so this is a
# judgment call; tightened rather than loosened since the alternative is leaking PII to a
# third-party agency before any commission has even been agreed.
# 2026-09-19 product decision: an agency already receives every one of these fields on the
# generated CV PDF (cv_api.py's _cv_context) once a candidate reaches CV Generated -- so none of
# it is a NEW disclosure to withhold from the portal API. Both the candidate-browsing list
# (list_portal_candidates) and the single-candidate detail call (get_candidate_detail) now return
# this same set -- no separate, narrower "list" tier anymore. Still excludes what's genuinely NOT
# on the CV and has no agency-facing purpose: national_id, labor_id, phone/alternate_phone, email,
# home address, emergency_contact_*, region/sub_region, and all internal-only fields (fees,
# medical_remarks).
PORTAL_FIELDS = [
	"name",
	"full_name",
	"gender",
	"nationality",
	"date_of_birth",
	"age",
	"target_job",
	"education",
	"photograph",
	"photo_full_body",
	"destination_country",
	"religion",
	"marital_status",
	"children",
	"height",
	"weight",
	"complexion",
	"institution",
	"graduation_year",
	"english_level",
	"arabic_level",
	"current_employer",
	"years_of_experience",
	"experience_country",
	"experience_period",
	"experience_video",
	"education_remarks",
	"coc_status",
	"salary_amount",
	"salary_currency",
	"place_of_birth",
	"city",
	"leaving_town",
	"remarks",
	"passport_number",
	"passport_issue_date",
	"passport_expiry_date",
	"passport_issue_place",
	"passport_scan",
	"skill_cleaning",
	"skill_cooking",
	"skill_washing",
	"skill_ironing",
	"skill_baby_sitting",
	"skill_children_care",
	"skill_arabic_cooking",
	"skill_elderly_care",
	"skill_driving",
	"skill_sewing",
	# Only ever returned as "FIT" -- see _portal_medical_status. UNFIT candidates are excluded from
	# the portal entirely, and Pending/blank is dropped rather than shown (2026-09-23).
	"medical_status",
]


def _portal_medical_status(rows):
	"""2026-09-23 product decision: agencies see medical_status only when it's FIT. Anything else
	(Pending, blank) is removed from the row, not shown as a value -- medical detail is mostly a
	post-selection concern (Placement.medical_selected_status / medical_2_status)."""
	for row in rows:
		if row and row.get("medical_status") != "FIT":
			row.pop("medical_status", None)
	return rows

# Kept as an alias, not a narrower list -- get_candidate_detail used to return a richer set than
# list_portal_candidates; now they're identical (see PORTAL_FIELDS' own comment). Two names are
# kept only so call sites read clearly ("the list fields" vs "the detail fields") without implying
# the sets differ.
PORTAL_DETAIL_FIELDS = PORTAL_FIELDS

# Fields a Foreign Agency may see on its OWN placements (portal_api.list_my_placements). Lifecycle,
# contract/visa identifiers, medical checkpoints and travel logistics — the things an agency needs
# to track its own workers. Deliberately excludes internal cost/commission fields (ticket_cost,
# reschedule_cost, manual_commission_*) and employer/sponsor PII (national IDs, addresses, the
# cross-check agency-name/license fields) that are internal-staff-only.
PORTAL_PLACEMENT_FIELDS = [
	"name",
	"applicant",
	"contractor",
	"destination_country",
	"status",
	"cv_record",
	"cycle_number",
	"contract_file",
	"contract_signed_date",
	"contract_number",
	"visa_number",
	"employer_name",
	"employment_site",
	"contract_duration",
	"contract_salary_amount",
	"contract_salary_currency",
	"visa_file",
	"visa_type",
	"visa_issue_date",
	"visa_expiry_date",
	"visa_reference_number",
	"sponsor_name",
	"medical_selected_status",
	"medical_selected_examination_date",
	"medical_2_status",
	"medical_2_examination_date",
	"ticket_number",
	"flight_date",
	"is_rescheduled",
	"reschedule_date",
	"reschedule_cause",
	"is_free_replacement",
	"free_replacement_for_complaint",
	"departed_on",
]


def _get_contractor_for_session_user(contractor_override=None):
	is_internal = frappe.session.user == "Administrator" or bool(
		{"Manager", "Admin", "System Manager"} & set(frappe.get_roles())
	)
	if "Foreign Agency" in frappe.get_roles() and not is_internal:
		# Strictly tenant-scoped: Foreign Agency callers MUST ONLY ever access their own linked Contractor.
		contractor_name = frappe.db.get_value("Contractor", {"user": frappe.session.user}, "name")
		if not contractor_name:
			frappe.throw("Foreign agency user is not linked to any Contractor.", frappe.PermissionError)
		if contractor_override and contractor_override != contractor_name:
			frappe.throw("Not permitted to access data for another agency.", frappe.PermissionError)
		return frappe.get_doc("Contractor", contractor_name)

	if contractor_override:
		# Acting for an agency is reserved for internal readers; any other logged-in user used to be
		# able to pass any agency's name and act as it (QA P6-01; Registrar access is decision D-20).
		if not is_internal:
			frappe.throw("Not permitted.", frappe.PermissionError)
		return frappe.get_doc("Contractor", contractor_override)

	if is_internal:
		frappe.throw(
			"A contractor must be specified for this operation (pass contractor_name / contractor).",
			frappe.ValidationError,
		)
	frappe.throw("Not permitted.", frappe.PermissionError)



def _is_internal_placement_reader():
	"""Internal staff who legitimately see every placement regardless of Contractor — the same
	set that can override country scoping in select_candidate. Foreign Agency is deliberately
	NOT here: it is tenant-scoped to its own Contractor."""
	return frappe.session.user == "Administrator" or bool(
		{"Manager", "Admin", "System Manager"} & set(frappe.get_roles())
	)


def _own_placement_or_403(placement_name, contractor):
	"""Multi-tenant isolation gate for returning an *existing* Placement to a portal caller.

	Idempotent re-selection by the placement's real owner returns the placement; internal staff
	(_is_internal_placement_reader) also get it. Any other agency gets a bare PermissionError —
	no placement name, contractor, status, financial, clearance, ticket, or any other field is
	read or returned. This is the single choke point both select_candidate return paths funnel
	through, so a row-lock/idempotency race cannot bypass the ownership check."""
	owner_contractor = frappe.db.get_value("Placement", placement_name, "contractor")
	if _is_internal_placement_reader() or owner_contractor == contractor.name:
		return frappe.get_doc("Placement", placement_name).as_dict()
	frappe.throw("Not permitted.", frappe.PermissionError)


def _get_latest_cv_record(applicant_name):
	return frappe.db.get_value(
		"CV Record",
		{"applicant": applicant_name, "docstatus": 1},
		"name",
		order_by="creation desc",
	)


@frappe.whitelist()
def list_portal_candidates(target_job=None, gender=None, limit_start=0, limit_page_length=0, with_total=0, **kwargs):
	"""business-workflow-srs.md: "Contractors can browse available registered candidates (CV
	status), filtered by their quota country." Only CV Generated candidates (Part A.2 Stage 4);
	only the contractor's own country."""
	allowed_roles = {"Foreign Agency", "Manager", "Admin", "System Manager", "Registrar"}
	if frappe.session.user != "Administrator" and not (allowed_roles & set(frappe.get_roles())):
		frappe.throw("Not permitted.", frappe.PermissionError)
	filters = {
		"status": "CV Generated",
		"entry_track": "Standard",
		# Candidate exclusivity: the instant an applicant is selected by any agency, active_placement
		# is set and they must leave every agency's marketplace. Enforced backend-side here (not in
		# the frontend) so a placed/reserved candidate is never returned to an unrelated agency.
		"active_placement": ["is", "not set"],
		# A medically UNFIT applicant never appears in the portal (2026-09-23) -- normally they can't
		# reach CV Generated at all (cv_generation_gate), this covers one marked UNFIT afterwards.
		"medical_status": ["!=", "UNFIT"],
	}
	is_internal = frappe.session.user == "Administrator" or bool(
		{"Manager", "Admin", "System Manager"} & set(frappe.get_roles())
	)
	if "Foreign Agency" in frappe.get_roles() and not is_internal:
		contractor = _get_contractor_for_session_user()
		filters["destination_country"] = contractor.country
	elif kwargs.get("contractor_name") or kwargs.get("contractor"):
		contractor = _get_contractor_for_session_user(contractor_override=kwargs.get("contractor_name") or kwargs.get("contractor"))
		filters["destination_country"] = contractor.country
	elif kwargs.get("destination_country"):
		filters["destination_country"] = kwargs.get("destination_country")
	if target_job:
		filters["target_job"] = target_job
	if gender:
		filters["gender"] = gender

	# Country-ban filter (2026-09-22): a banned (applicant, country) pair must never appear in
	# the marketplace, including for an applicant who was already CV Generated when the ban was
	# set -- register_applicant/generate_cv only stop a NEW arrival, so this listing needs its
	# own check to remove someone already inside once a ban lands on them.
	banned_names = frappe.db.sql_list(
		"""
		SELECT acb.applicant
		FROM `tabApplicant Country Ban` acb
		INNER JOIN `tabApplicant` a ON a.name = acb.applicant
		WHERE acb.active = 1 AND acb.country = a.destination_country
		"""
	)
	if banned_names:
		filters["name"] = ["not in", banned_names]

	# default=0: every candidate when no page size is given (its historical behavior).
	start, length = page_args(limit_start, limit_page_length, default=0)
	rows = frappe.get_list(
		"Applicant",
		filters=filters,
		fields=PORTAL_FIELDS,
		ignore_permissions=True,
		limit_start=start,
		limit_page_length=length,
		order_by="modified desc",
	)
	# Resolve any R2-stored media (photograph/photo_full_body/experience_video/passport_scan --
	# see storage_engine.py's 2026-09-21 private-bucket rewrite) to short-lived signed URLs. A
	# safe no-op for rows whose files are still local, or not yet migrated to R2 at all.
	from agency_tracking.storage_engine import resolve_r2_fields

	resolve_r2_fields(rows, ["photograph", "photo_full_body", "experience_video", "passport_scan"])
	return paged_result(
		_portal_medical_status(rows), with_total, lambda: count_rows("Applicant", filters, ignore_permissions=True)
	)


def _assert_can_view_candidate(applicant_name):
	"""Shared permission gate for viewing a single portal candidate's detail OR photo. A Foreign
	Agency may only view a candidate currently available in its OWN destination country (Standard,
	CV Generated, not yet locked), OR one whose active Placement it already owns. Any other
	candidate is a bare 403. Internal staff / Registrar / management may view any. Returns the
	candidate's scope row."""
	allowed_roles = {"Foreign Agency", "Manager", "Admin", "System Manager", "Registrar"}
	if frappe.session.user != "Administrator" and not (allowed_roles & set(frappe.get_roles())):
		frappe.throw("Not permitted.", frappe.PermissionError)

	scope = frappe.db.get_value(
		"Applicant",
		applicant_name,
		["name", "status", "entry_track", "destination_country", "active_placement", "medical_status"],
		as_dict=True,
	)
	if not scope:
		frappe.throw(f"{applicant_name} not found.", frappe.DoesNotExistError)

	if "Foreign Agency" in frappe.get_roles() and not _is_internal_placement_reader():
		contractor = _get_contractor_for_session_user()
		owns_placement = bool(scope.active_placement) and (
			frappe.db.get_value("Placement", scope.active_placement, "contractor") == contractor.name
		)
		available = (
			scope.entry_track == "Standard"
			and scope.status == "CV Generated"
			and not scope.active_placement
			and scope.destination_country == contractor.country
			and scope.medical_status != "UNFIT"
		)
		if not (available or owns_placement):
			frappe.throw("Not permitted.", frappe.PermissionError)
	return scope


@frappe.whitelist()
def get_candidate_detail(applicant_name=None, **kwargs):
	"""Full CV-equivalent profile for a single catalog candidate (PORTAL_FIELDS -- same set
	list_portal_candidates now returns per row too, see that constant's own comment).

	Same tenant/country scoping as the marketplace (see _assert_can_view_candidate)."""
	applicant_name = applicant_name or kwargs.get("applicant") or kwargs.get("name")
	if not applicant_name:
		frappe.throw("applicant_name is required.", frappe.ValidationError)
	_assert_can_view_candidate(applicant_name)
	row = frappe.db.get_value("Applicant", applicant_name, PORTAL_DETAIL_FIELDS, as_dict=True)

	from agency_tracking.storage_engine import resolve_r2_fields

	resolve_r2_fields([row], ["photograph", "photo_full_body", "experience_video", "passport_scan"])
	return _portal_medical_status([row])[0]


# The ONLY candidate images an agency may load. Sensitive files (passport_scan, contract, visa) are
# never served here.
CANDIDATE_PHOTO_KINDS = {"photograph", "photo_full_body"}


@frappe.whitelist()
def get_candidate_photo(applicant_name=None, kind="photograph", **kwargs):
	"""Serve a portal candidate's marketing photo to an agency allowed to view that candidate (same
	gate as get_candidate_detail). Agencies can't read the underlying (private) File docs directly,
	so this is their only path to these images -- and it exposes ONLY photograph / photo_full_body,
	never passport/contract/visa files.

	Storage-agnostic: a local Frappe file is streamed inline; a photo offloaded to R2 (2026-09-21:
	R2 is a PRIVATE bucket now, see storage_engine.py) is served via a redirect to a short-lived
	signed URL generated only after the permission gate above already passed -- never a bare
	public bucket URL. Missing photo -> 404."""
	applicant_name = applicant_name or kwargs.get("applicant") or kwargs.get("name")
	kind = kind if kind in CANDIDATE_PHOTO_KINDS else "photograph"
	if not applicant_name:
		frappe.throw("applicant_name is required.", frappe.ValidationError)
	_assert_can_view_candidate(applicant_name)

	value = frappe.db.get_value("Applicant", applicant_name, kind)
	if not value:
		frappe.throw("No photo on file for this candidate.", frappe.DoesNotExistError)

	from agency_tracking.storage_engine import is_r2_ref, resolve_r2_url

	if is_r2_ref(value):
		frappe.local.response["type"] = "redirect"
		frappe.local.response["location"] = resolve_r2_url(value)
		return

	# Local Frappe file: stream the bytes ourselves (the agency has no direct File read permission,
	# but our own gate above already authorized this specific candidate's photo).
	file_name = frappe.db.get_value("File", {"file_url": value}, "name")
	if not file_name:
		frappe.throw("Photo file not found.", frappe.DoesNotExistError)
	file_doc = frappe.get_doc("File", file_name)
	frappe.local.response.filename = file_doc.file_name
	frappe.local.response.filecontent = file_doc.get_content()
	frappe.local.response.type = "download"


@frappe.whitelist()
def select_candidate(applicant_name=None, free_replacement_for_complaint=None, contractor_name=None, **kwargs):
	"""Part A.2 Stage 4: atomic, globally exclusive selection. The instant one agency selects
	a candidate, they vanish from every other agency's view — enforced here with a row lock
	(SELECT ... FOR UPDATE) so two concurrent selections can't both see the candidate as free.
	"""
	applicant_name = applicant_name or kwargs.get("applicant")
	contractor_name = contractor_name or kwargs.get("contractor")
	if not applicant_name:
		frappe.throw("applicant_name is required.", frappe.ValidationError)

	contractor = _get_contractor_for_session_user(contractor_override=contractor_name)

	applicant = frappe.get_doc("Applicant", applicant_name)
	if applicant.active_placement:
		# CRITICAL multi-tenant gate: only the owning Contractor (or internal staff) may see an
		# already-existing Placement. A different agency selecting an already-taken applicant gets
		# a bare 403 — never the other agency's Placement document or any of its metadata.
		return _own_placement_or_403(applicant.active_placement, contractor)
	if applicant.entry_track != "Standard":
		frappe.throw("Only Standard-track candidates are selected via the portal.", frappe.ValidationError)
	if applicant.status != "CV Generated":
		frappe.throw(
			f"{applicant_name} is not currently portal-visible (status: {applicant.status}).",
			frappe.ValidationError,
		)
	if applicant.destination_country != contractor.country and frappe.session.user != "Administrator" and not ({"Manager", "Admin", "System Manager"} & set(frappe.get_roles())):
		frappe.throw("Not permitted.", frappe.PermissionError)
	if applicant.medical_status == "UNFIT":
		# Never in the listing (2026-09-23); this catches a stale page. Same generic wording to an
		# agency as the ban backstop below -- the medical result isn't disclosed to them.
		frappe.throw(
			f"{applicant_name} is medically UNFIT and cannot be selected."
			if _is_internal_placement_reader()
			else "This candidate is no longer available.",
			frappe.ValidationError,
		)

	# Backstop (2026-09-22): list_portal_candidates already excludes a banned applicant, but a
	# stale/cached portal page could still submit a select for one banned since it rendered. No
	# override here -- select_candidate has no Manager-override surface today, unlike
	# register_applicant/generate_cv/restart_applicant; a blocked selection must go through one
	# of those (or a future dedicated override param) rather than silently overridable here.
	# An agency gets a generic "no longer available" (2026-09-23) -- the ban itself (its existence,
	# country, ACB id) is an internal decision and must not leak to the foreign agency; internal
	# staff still get the detailed message. Nobody is notified of a blocked attempt (2026-09-23).
	from agency_tracking.applicant_api import _check_country_ban_or_throw

	if _is_internal_placement_reader():
		_check_country_ban_or_throw(applicant_name, applicant.destination_country, False, None)
	else:
		if frappe.db.exists(
			"Applicant Country Ban",
			{"applicant": applicant_name, "country": applicant.destination_country, "active": 1},
		):
			frappe.throw("This candidate is no longer available.", frappe.ValidationError)

	if free_replacement_for_complaint:
		complaint = frappe.get_doc("Complaint", free_replacement_for_complaint)
		if complaint.status != "Returned - Free Replacement Required":
			frappe.throw(
				f"{free_replacement_for_complaint} is not an approved free-replacement complaint "
				f"(status: {complaint.status}).",
				frappe.ValidationError,
			)
		if complaint.contractor != contractor.name:
			frappe.throw("Not permitted.", frappe.PermissionError)
		if frappe.db.exists("Placement", {"free_replacement_for_complaint": free_replacement_for_complaint}):
			frappe.throw(
				f"{free_replacement_for_complaint}'s free replacement has already been used.",
				frappe.ValidationError,
			)

	# Row lock held until this request's transaction commits — a second, concurrent
	# select_candidate() for the same applicant blocks here until the first is done, then
	# sees active_placement already set and is rejected. Without this, two agencies could
	# both read active_placement as empty before either had written it.
	current_lock = lock_applicant_row(applicant_name)
	if current_lock:
		# Same isolation gate as the pre-lock path — a placement that appeared while we waited on
		# the row lock is returned only if it is ours (idempotent), otherwise a bare 403. This is
		# what stops the lock/idempotency window from leaking another agency's placement.
		return _own_placement_or_403(current_lock, contractor)

	placement = frappe.get_doc(
		{
			"doctype": "Placement",
			"applicant": applicant_name,
			"contractor": contractor.name,
			"destination_country": applicant.destination_country,
			"status": "Selected",
			"cv_record": _get_latest_cv_record(applicant_name),
			"is_free_replacement": 1 if free_replacement_for_complaint else 0,
			"free_replacement_for_complaint": free_replacement_for_complaint,
		}
	).insert(ignore_permissions=True)

	frappe.db.set_value("Applicant", applicant_name, "active_placement", placement.name)
	return placement.as_dict()


@frappe.whitelist()
def list_my_placements(status=None, limit_page_length=100, limit_start=0, order_by="modified desc", contractor_name=None, with_total=0, **kwargs):
	"""Foreign Agency's own placement read surface (backend-issues, multi-tenant audit).

	placement_api.list_placements is internal-staff-only (Placement's doctype-level read grants
	never include Foreign Agency, by design — Part F/G), so agencies 403 there. Rather than
	weaken that grant, this dedicated portal op derives the Contractor from the session user
	(never from a caller-supplied contractor id) and returns ONLY that Contractor's placements,
	limited to PORTAL_PLACEMENT_FIELDS. Unlinked Foreign Agency users 403 via
	_get_contractor_for_session_user. Tenant isolation is enforced entirely server-side."""
	contractor = _get_contractor_for_session_user(contractor_override=contractor_name or kwargs.get("contractor"))
	filters = {"contractor": contractor.name}
	if status:
		filters["status"] = status
	start, length = page_args(limit_start, limit_page_length)
	rows = frappe.get_all(
		"Placement",
		filters=filters,
		fields=PORTAL_PLACEMENT_FIELDS,
		limit_page_length=length,
		limit_start=start,
		order_by=order_by,
		ignore_permissions=True,
	)
	return paged_result(rows, with_total, lambda: count_rows("Placement", filters, ignore_permissions=True))


@frappe.whitelist()
def list_my_wakala_requests(contractor_name=None, **kwargs):
	"""New (2026-08-29): a Contractor-scoped list of every unpaid Wakala-bearing Embassy step
	for their own placements — the page the watchdog/manual reminders (watchdogs.
	wakala_reminder_watchdog) are actually pointing them at. Mirrors list_my_clearance_steps()'s
	pattern for the internal-staff side."""
	contractor = _get_contractor_for_session_user(contractor_override=contractor_name or kwargs.get("contractor"))
	placement_names = frappe.get_all("Placement", filters={"contractor": contractor.name}, pluck="name")
	if not placement_names:
		return []
	return frappe.get_all(
		"Clearance Step",
		filters={
			"placement": ["in", placement_names],
			"step_type": "Embassy",
			"wakala_status": ["!=", "Paid"],
		},
		fields=["name", "placement", "wakala_amount", "wakala_status", "status"],
		ignore_permissions=True,
	)
