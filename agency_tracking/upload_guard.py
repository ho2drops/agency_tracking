# Copyright (c) 2026, Agency and contributors
# License: MIT. See LICENSE
#
# Every upload is checked here, on the server. The browser's own checks (file picker filter, size
# hint) are a convenience and can be skipped by anyone calling the endpoint directly (QA P8-16).
#
# Two checks, one table each:
#   - at upload (upload_file below, which replaces the framework's endpoint): the file is a kind
#     this app stores at all, its content is what its extension says, it is not too large, and a
#     foreign agency's file is stored private whatever the caller asked for;
#   - when a file is put into a slot (check_slots, a validate hook): a photo slot holds an image,
#     a document slot an image or a PDF. The upload itself does not say which slot it is for.

import os

import frappe

from agency_tracking.roles import FOREIGN_AGENCY, INTERNAL_STAFF_ROLES

MAX_UPLOAD_MB = 20
MAX_UPLOAD_BYTES = MAX_UPLOAD_MB * 1024 * 1024

IMAGE = {".jpg", ".jpeg", ".png", ".webp"}
DOCUMENT = IMAGE | {".pdf"}
# Videos are not held to MAX_UPLOAD_MB; the framework's own max_file_size is their only limit.
VIDEO = {".mp4", ".webm", ".mov"}
# Bank statements, read by reconciliation_engine. Staff only.
STATEMENT = {".csv"}

# How each checked kind starts. A file whose content does not match its extension is refused.
_JPEG = (b"\xff\xd8\xff",)
SIGNATURES = {
	".jpg": _JPEG,
	".jpeg": _JPEG,
	".png": (b"\x89PNG\r\n\x1a\n",),
	".pdf": (b"%PDF-",),
}

# doctype -> {field: extensions it may hold}. Child-table doctypes are checked through their parent.
SLOTS = {
	"Applicant": {"photograph": IMAGE, "photo_full_body": IMAGE, "passport_scan": DOCUMENT, "experience_video": VIDEO},
	"Placement": {"contract_file": DOCUMENT, "visa_file": DOCUMENT},
	"Applicant Transaction": {"receipt_image": DOCUMENT},
	"Applicant Fee Log": {"receipt_url": DOCUMENT},
	"Clearance Step Payment": {"receipt_url": DOCUMENT},
	"Injaz Attempt": {"receipt_photo": IMAGE},
	"Chat Message": {"attachment": DOCUMENT},
	"Bank Statement": {"statement_file": STATEMENT | {".pdf"}},
	"Agency Tracking Settings": {"logo": IMAGE, "stamp_image": IMAGE},
}


def is_agency_user():
	"""A foreign agency's own login: linked to a Contractor, or holding only the agency role."""
	user = frappe.session.user
	if user == "Administrator":
		return False
	roles = set(frappe.get_roles(user))
	if frappe.db.exists("Contractor", {"user": user}):
		return True
	return FOREIGN_AGENCY in roles and not (INTERNAL_STAFF_ROLES & roles)


def _extension(filename):
	return os.path.splitext(str(filename or ""))[1].lower()


def _kinds(extensions):
	return ", ".join(sorted(e[1:].upper() for e in extensions if e != ".jpeg"))


def _is_webp(content):
	return content[:4] == b"RIFF" and content[8:12] == b"WEBP"


def check_upload(filename, content):
	"""Refuse a file this app does not store, a file that is not what its extension says, or one
	over the size limit. Raises ValidationError with a message a user can act on."""
	ext = _extension(filename)
	allowed = DOCUMENT | VIDEO | (set() if is_agency_user() else STATEMENT)
	if ext not in allowed:
		frappe.throw(
			f"This kind of file cannot be uploaded. Use {_kinds(DOCUMENT)} (or {_kinds(VIDEO)} for a video).",
			frappe.ValidationError,
		)
	content = content or b""
	if ext not in VIDEO and len(content) > MAX_UPLOAD_BYTES:
		frappe.throw(
			f"The file is {len(content) / 1024 / 1024:.1f} MB. The limit is {MAX_UPLOAD_MB} MB.", frappe.ValidationError
		)
	head = content[:16]
	genuine = _is_webp(head) if ext == ".webp" else any(head.startswith(s) for s in SIGNATURES.get(ext, (b"",)))
	if not genuine:
		frappe.throw(f"This file is not a real {ext[1:].upper()} file.", frappe.ValidationError)


def privacy_for(requested):
	"""The is_private value an upload is stored with: always private for a foreign agency."""
	return 1 if is_agency_user() else requested


@frappe.whitelist()
def upload_file():
	"""The upload endpoint (hooks.override_whitelisted_methods routes /api/method/upload_file
	here). Checks the file before the framework reads, resizes or stores it, then hands over."""
	from frappe.handler import upload_file as framework_upload_file

	try:
		files = frappe.request.files
	except RuntimeError:  # no HTTP request: called in-process, there is nothing to store
		frappe.throw("No file was sent.", frappe.ValidationError)
	uploaded = files.get("file")
	if uploaded:
		content = uploaded.stream.read()
		uploaded.stream.seek(0)
		check_upload(uploaded.filename, content)
	frappe.form_dict.is_private = privacy_for(frappe.form_dict.get("is_private"))
	return framework_upload_file()


def _refuse_slot(doc, field, allowed):
	label = doc.meta.get_label(field) or field
	frappe.throw(f"{label} must be a {_kinds(allowed)} file.", frappe.ValidationError)


def check_slots(doc, method=None):
	"""validate hook: a file newly put into a slot is of a kind that slot holds. Only a changed
	value is checked, so a record carrying an older file can still be edited."""
	before = doc.get_doc_before_save()
	for field, allowed in SLOTS.get(doc.doctype, {}).items():
		value = doc.get(field)
		if value and (before is None or before.get(field) != value) and _extension(value) not in allowed:
			_refuse_slot(doc, field, allowed)

	old_rows = {row.name: row for row in (before.get_all_children() if before else [])}
	for row in doc.get_all_children():
		for field, allowed in SLOTS.get(row.doctype, {}).items():
			value = row.get(field)
			old = old_rows.get(row.name)
			if value and (old is None or old.get(field) != value) and _extension(value) not in allowed:
				_refuse_slot(row, field, allowed)
