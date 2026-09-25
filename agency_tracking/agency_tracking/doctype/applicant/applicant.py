# Copyright (c) 2026, Agency and contributors
# License: MIT. See LICENSE

import frappe
from frappe.model.document import Document
from decimal import Decimal
from agency_tracking.state_machine import guard_status_write

# Part A.2 / business-workflow-srs.md Stage 1: bare minimum to open a file. Registrar-confirmed
# floor (2026-08-29): phone/address are NOT required at Draft -- only identity + which track.
DRAFT_REQUIRED_FIELDS = ["full_name", "gender", "nationality", "entry_track"]

# Part A.2 / SRS Stage 2 (Standard track): full field floor before a candidate is "official".
# national_id, labor_id, emergency_contact_name/phone are deliberately NOT required here
# (2026-08-29 correction) -- those are LMIS-stage data, captured later via
# applicant_api.update_applicant_for_lmis once the candidate reaches the LMIS clearance step,
# not known/collected at Registration time.
STANDARD_REGISTERED_REQUIRED_FIELDS = DRAFT_REQUIRED_FIELDS + [
	"destination_country",
	"salary_amount",
	"salary_currency",
	"religion",
	"marital_status",
	"passport_number",
	"passport_issue_date",
	"passport_expiry_date",
	"passport_issue_place",
	"date_of_birth",
	"education",
	"target_job",
	"photograph",
	"passport_scan",
]

# Part A.1: Muayena registers with a lighter, global-only field floor
# (passport, medical, photos — CV-specific fields optional/unused; national_id is LMIS-stage,
# same reasoning as Standard above).
# destination_country IS required here (2026-08-29 correction) -- it's selected during
# Draft/Registered same as Standard, not deferred until a contract is uploaded. The earlier
# assumption that it becomes known only at Placement creation was wrong; see
# placement_api.create_muayena_placement, which no longer needs to set it as a side effect.
MUAYENA_REGISTERED_REQUIRED_FIELDS = DRAFT_REQUIRED_FIELDS + [
	"destination_country",
	"passport_number",
	"passport_issue_date",
	"passport_expiry_date",
	"passport_issue_place",
	"date_of_birth",
	"photograph",
	"passport_scan",
]

FIELD_FLOOR = {
	("Standard", "Draft"): DRAFT_REQUIRED_FIELDS,
	("Standard", "Registered"): STANDARD_REGISTERED_REQUIRED_FIELDS,
	("Muayena", "Draft"): DRAFT_REQUIRED_FIELDS,
	("Muayena", "Registered"): MUAYENA_REGISTERED_REQUIRED_FIELDS,
}

# Fields whose uniqueness is enforced manually (see validate_uniqueness) rather than via
# a DB-level `unique` flag, since they're blank at Draft and a DB unique index would collide
# on repeated empty strings across multiple Draft rows.
UNIQUE_FIELDS = ["passport_number", "national_id", "labor_id"]

# 2026-09-23: stored in ALL CAPS, whitespace-collapsed -- the passport prints all of these in
# capitals, and the CV/invoices/reports/portal all display them as stored. The passport reader
# already emitted uppercase names; typed entry didn't, so it's normalized here on every save.
UPPERCASE_FIELDS = ("first_name", "middle_name", "last_name", "full_name", "place_of_birth", "passport_issue_place")


class Applicant(Document):
	def before_validate(self):
		# Must run before validate() (Frappe's save order is before_validate -> validate ->
		# before_save), not before_save -- a before_save hook fires *after* validate_field_floor
		# has already run and thrown, so any field the OCR would have filled in is too late to
		# help this same save. Found 2026-08-29 via a real "uploading a passport on a
		# near-empty Draft throws a field-floor error" report.
		self.normalize_unique_blanks()
		self.autofill_from_passport()
		self.calc_passport_issue_date()
		self.calc_age()

	def normalize_unique_blanks(self):
		"""Store NULL (not "") for blank uniquely-constrained fields (audit G-012) so empty values
		never collide and a DB-level unique index stays possible; validate_uniqueness remains the
		friendly-error path for populated duplicates."""
		for fieldname in UNIQUE_FIELDS:
			if not self.get(fieldname):
				self.set(fieldname, None)

	def validate(self):
		guard_status_write(self, ("Draft",))  # QA A1: status only via the app's actions
		self.normalize_uppercase_fields()
		self.set_full_name()
		self.validate_passport_dates()
		self.validate_field_floor()
		self.validate_uniqueness()

	def validate_passport_dates(self):
		"""Logical/date-sanity checks (audit G-008): the field floor only checks presence, not that
		the values make sense. Catches OCR/entry errors before they flow into the CV and departure."""
		issue = self.passport_issue_date
		expiry = self.passport_expiry_date
		if issue and expiry and frappe.utils.getdate(expiry) <= frappe.utils.getdate(issue):
			frappe.throw(
				"Passport expiry date must be after the issue date.", frappe.ValidationError
			)
		if self.date_of_birth and frappe.utils.getdate(self.date_of_birth) > frappe.utils.getdate():
			frappe.throw("Date of birth cannot be in the future.", frappe.ValidationError)
		# An already-expired passport must not pass registration (it can't get anyone anywhere).
		if expiry and self.status in ("Registered", "CV Generated") and frappe.utils.getdate(expiry) < frappe.utils.getdate():
			frappe.throw(
				f"Passport expired on {expiry}; it must be renewed before the applicant can be Registered.",
				frappe.ValidationError,
			)

	def before_save(self):
		# Before the row is written, so the "r2:" reference is what gets saved: from on_update the
		# local file was deleted but the DB kept pointing at it (QA S-03).
		self.sync_media_to_r2()

	def on_update(self):
		self.maybe_log_fee_transaction()
		self.sync_fee_log()

	def sync_media_to_r2(self):
		"""2026-09-21: the "photos"/"videos" storage categories existed in storage_engine.py's
		STORAGE_CATEGORIES from the start, and portal_api.get_candidate_photo was already written
		"storage-agnostic ... correct both now and after the R2 cutover" -- but nothing ever
		actually called migrate_attach_to_r2 for these fields, so the cutover never happened.
		Closes that gap the same way sync_fee_log already does for receipts: best-effort, no-op
		once already an R2 URL, silently leaves the local file in place if R2 isn't configured
		(see migrate_attach_to_r2 / upload_to_r2's Public URL Base guard)."""
		from agency_tracking.storage_engine import migrate_attach_to_r2

		migrate_attach_to_r2(self, "photograph", "photos", applicant_name=self.name)
		migrate_attach_to_r2(self, "photo_full_body", "photos", applicant_name=self.name)
		migrate_attach_to_r2(self, "experience_video", "videos", applicant_name=self.name)
		for row in self.get("fee_log") or []:
			migrate_attach_to_r2(row, "receipt_url", "finance-receipts", applicant_name=self.name)

	def autofill_from_passport(self):
		"""Auto-parse the passport scan's MRZ on every upload/replacement and fill in currently-blank
		fields only -- never overwrites something the registrar already typed. Runs again when the
		scan is REPLACED (audit G-010): re-parses the new scan, fills any still-blank fields, and
		flags needs_passport_review if the new scan's values conflict with existing non-blank ones
		(rather than silently ignoring the correction or clobbering verified data). Best-effort,
		never blocks the save (see passport_parser.parse_passport_mrz)."""
		if not (self.passport_scan and self.has_value_changed("passport_scan")):
			return
		try:
			from agency_tracking.passport_parser import parse_passport_mrz

			file_doc = frappe.db.get_value("File", {"file_url": self.passport_scan}, "name")
			if not file_doc:
				return
			file_path = frappe.get_doc("File", file_doc).get_full_path()
			extracted = parse_passport_mrz(file_path)
		except Exception:
			frappe.log_error(title="Passport auto-fill failed", message=f"Applicant {self.name}")
			return

		if not extracted:
			return

		# Passport parsing is strictly informational (button-only stage transitions): it may fill
		# blank data fields but must never touch status/lifecycle. Belt-and-suspenders against a
		# future MRZ regex capturing a stray "status" token -- see state_machine.LIFECYCLE_FIELDS.
		from agency_tracking.state_machine import strip_lifecycle_fields

		extracted = strip_lifecycle_fields(extracted)

		# The parser's own low-confidence signal (checksum failure / visual-only / etc.).
		needs_review = bool(extracted.pop("needs_passport_review", False))

		# first_name/last_name is a joint condition -- only split into the pair if BOTH are
		# currently blank, never partially overwrite one half of an already-entered name.
		name_keys = ("first_name", "middle_name", "last_name")
		if any(k in extracted for k in name_keys) and (self.first_name or self.last_name or self.middle_name):
			for k in name_keys:
				extracted.pop(k, None)

		for fieldname, value in extracted.items():
			current = self.get(fieldname)
			if not current:
				self.set(fieldname, value)
			elif str(current).strip().upper() != str(value).strip().upper():
				# Case-insensitive: UPPERCASE_FIELDS are stored in caps, so a re-scan reading the
				# same text in another case is not a disagreement.
				# A replaced scan disagrees with data already on file -- surface it, don't clobber.
				needs_review = True

		if needs_review:
			self.needs_passport_review = 1

	def calc_passport_issue_date(self):
		"""Derive Passport Issue Date from expiry (5-year assumption) ONLY when it's blank -- audit
		G-007: the field is now editable, so a printed/OCR'd issue date or a manual correction (e.g.
		for a non-5-year passport) is never overwritten. Reuses the same pure function the MRZ path
		uses, so OCR and direct entry agree when neither supplied an issue date."""
		if self.passport_issue_date or not self.passport_expiry_date:
			return
		from agency_tracking.passport_parser import infer_passport_issue_date

		self.passport_issue_date = infer_passport_issue_date(self.passport_expiry_date)

	def calc_age(self):
		"""Age is fully derived from Date of Birth (2026-08-29, read_only in applicant.json) --
		never manually entered."""
		if not self.date_of_birth:
			self.age = None
			return
		dob = frappe.utils.getdate(self.date_of_birth)
		today = frappe.utils.getdate()
		self.age = today.year - dob.year - ((today.month, today.day) < (dob.month, dob.day))

	def normalize_uppercase_fields(self):
		for fieldname in UPPERCASE_FIELDS:
			value = self.get(fieldname)
			if isinstance(value, str):
				self.set(fieldname, " ".join(value.split()).upper() or None)

	def set_full_name(self):
		"""full_name is always derived from first/middle/last when any part is set (2026-09-23),
		so correcting one part can't leave a stale full_name behind -- previously it was only
		filled when blank. A client that sends only full_name, with no parts, keeps it as sent."""
		parts = [self.first_name, self.middle_name, self.last_name]
		if any(parts):
			self.full_name = " ".join(filter(None, parts))

	def validate_field_floor(self):
		required = FIELD_FLOOR.get((self.entry_track, self.status))
		if required is None:
			# CV Generated and beyond: field floor was already enforced on the way into
			# Registered; nothing new is required to hold that status.
			return
		missing = [
			frappe.get_meta(self.doctype).get_label(fieldname)
			for fieldname in required
			if not self.get(fieldname)
		]
		if missing:
			frappe.throw(
				"{0} track, {1} status requires: {2}".format(
					self.entry_track, self.status, ", ".join(missing)
				),
				frappe.ValidationError,
			)

	def validate_uniqueness(self):
		for fieldname in UNIQUE_FIELDS:
			value = self.get(fieldname)
			if not value:
				continue
			existing = frappe.db.get_value(
				self.doctype,
				{fieldname: value, "name": ["!=", self.name or ""]},
				"name",
			)
			if existing:
				frappe.throw(
					"Another {0} ({1}) already has {2} '{3}'.".format(
						self.doctype, existing, frappe.get_meta(self.doctype).get_label(fieldname), value
					),
					frappe.DuplicateEntryError,
				)

	def maybe_log_fee_transaction(self):
		"""Fires the moment fee_status flips to Paid (button-driven or a direct Desk edit) --
		one single path for both, since log_applicant_fee (applicant_api.py) just sets
		fee_status='Paid' and saves, letting this hook do the actual logging. Idempotent via
		fee_transaction (never re-logs once set) and has_value_changed (never fires on an
		unrelated save of an already-Paid row)."""
		if not (
			self.fee_required
			and self.registration_fee_amount
			and self.fee_status == "Paid"
			and not self.fee_transaction
			and self.has_value_changed("fee_status")
		):
			return

		from agency_tracking.finance_engine import get_fx_rate

		fx_rate, fx_rate_date = get_fx_rate(self.fee_currency or "ETB")
		txn = frappe.get_doc(
			{
				"doctype": "Applicant Transaction",
				"applicant": self.name,
				"placement": self.active_placement or None,
				"transaction_type": self.fee_direction or "Income",
				"amount_original": Decimal(str(self.registration_fee_amount)),
				"currency_original": self.fee_currency or "ETB",
				"fx_rate": Decimal(str(fx_rate)),
				"fx_rate_date": fx_rate_date,
				"amount_birr": round(Decimal(str(self.registration_fee_amount)) * Decimal(str(fx_rate)), 2),
				"description": (self.fee_type or "Registration Fee")
				+ f" for {self.name}"
				+ (f" -- {self.fee_notes}" if self.fee_notes else ""),
				"stage_logged_at": self.status,
				"logged_by": frappe.session.user,
			}
		).insert(ignore_permissions=True)

		self.db_set("fee_transaction", txn.name, update_modified=False)
		if not self.fee_payment_date:
			self.db_set("fee_payment_date", frappe.utils.today(), update_modified=False)

	def sync_fee_log(self):
		"""Table-based income/expense log (2026-08-29) -- unlike the single Registration Fee
		above, this allows any number of entries per applicant. Every row without a linked
		transaction yet gets auto-logged as a Pending Applicant Transaction on this save (no
		separate button/endpoint needed, same as maybe_log_fee_transaction); every row that
		already has one gets its Status refreshed from the ledger's current state, so Finance
		approving/rejecting/voiding on the Applicant Transaction itself is reflected back here
		without the row itself ever needing another edit."""
		from agency_tracking.finance_engine import get_fx_rate

		for row in self.get("fee_log") or []:
			if row.transaction:
				new_status = frappe.db.get_value("Applicant Transaction", row.transaction, "status")
				if new_status and new_status != row.status:
					row.db_set("status", new_status)
				continue

			if not (row.description and row.amount):
				continue

			fx_rate, fx_rate_date = get_fx_rate(row.currency or "ETB")
			txn = frappe.get_doc(
				{
					"doctype": "Applicant Transaction",
					"applicant": self.name,
					"placement": self.active_placement or None,
					"transaction_type": row.transaction_type or "Income",
					"amount_original": Decimal(str(row.amount)),
					"currency_original": row.currency or "ETB",
					"fx_rate": Decimal(str(fx_rate)),
					"fx_rate_date": fx_rate_date,
					"amount_birr": round(Decimal(str(row.amount)) * Decimal(str(fx_rate)), 2),
					"description": row.description,
					"stage_logged_at": self.status,
					"logged_by": frappe.session.user,
				}
			).insert(ignore_permissions=True)
			row.db_set("transaction", txn.name)
			row.db_set("status", "Pending")


def get_permission_query_conditions(user):
	"""S-3 (2026-09-05): clearance-country roles see only applicants who have a placement with a
	clearance step of their own type. Management and every other internal role keep full access."""
	from agency_tracking.agency_tracking.doctype.clearance_step.clearance_step import scoped_clearance_step_types

	types = scoped_clearance_step_types(user)
	if types is None:
		return ""
	if not types:
		return "1=0"
	escaped = ", ".join(frappe.db.escape(t) for t in types)
	return (
		"`tabApplicant`.name in (select applicant from `tabPlacement` where name in "
		f"(select placement from `tabClearance Step` where step_type in ({escaped})))"
	)


def has_permission(doc, ptype=None, user=None):
	"""Single-document mirror of get_permission_query_conditions above (2026-09-11, same class
	of gap found and fixed for Clearance Step/Background Job/Process Event/Applicant
	Transaction/Placement this session): every corridor role also has blanket DocType-level read
	on Applicant (applicant.json), so without this any of them could read ANY applicant's record
	-- passport number, DOB, photos, everything -- by name, despite being correctly scoped in
	every list view."""
	from agency_tracking.agency_tracking.doctype.clearance_step.clearance_step import scoped_clearance_step_types

	user = user or frappe.session.user
	if not doc or not doc.get("name"):
		return True
	types = scoped_clearance_step_types(user)
	if types is None:
		return True
	if not types:
		return False
	placement_names = frappe.db.get_all("Placement", filters={"applicant": doc.name}, pluck="name")
	if not placement_names:
		return False
	return bool(frappe.db.exists("Clearance Step", {"placement": ["in", placement_names], "step_type": ["in", types]}))
