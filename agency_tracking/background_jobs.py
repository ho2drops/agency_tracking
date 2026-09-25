# Copyright (c) 2026, Agency and contributors
# License: MIT. See LICENSE
#
# Generic dispatch for the six OCR/PDF endpoints that are slow enough to justify moving off the
# request path (real OCR/PDF rendering, not something more CPU will meaningfully speed up).
# Additive only -- every synchronous twin (parse_passport_file, parse_contract_file,
# parse_visa_file, render_injaz_pdf, render_cv_pdf, get_batch_invoice_pdf) keeps working exactly
# as before; each enqueue_* wrapper is a thin, permission-matched alternative entry point.
#
# frappe.enqueue is new to this app (2026-09-11). Two things worth knowing if you touch this:
# - job_id=<Background Job name> is the correlation key (not job_name=, which is a deprecated,
#   display-only kwarg on frappe.enqueue itself -- passing our own id under that name would be
#   silently swallowed before ever reaching run_background_job).
# - frappe.enqueue captures frappe.session.user and the worker (execute_job) calls
#   frappe.set_user() with it automatically -- handlers run with the real requesting user's
#   permissions, no manual frappe.set_user() needed here.

import frappe
from frappe.utils import now_datetime

NOTIFY_TEMPLATE = "background_job_completed"


def _run_parse_passport(file_url=None, **kwargs):
	from agency_tracking.passport_parser import parse_passport_file

	return parse_passport_file(file_url)


def _run_parse_contract(file_url=None, destination_country=None, **kwargs):
	from agency_tracking.contract_parser import _parse_contract

	# Runs as the requester (frappe.enqueue carries the user); the file-read check applies.
	return _parse_contract(file_url, destination_country=destination_country)


def _run_parse_visa(file_url=None, **kwargs):
	from agency_tracking.contract_parser import _parse_visa

	return _parse_visa(file_url)


def _run_render_injaz(clearance_step_name=None, **kwargs):
	from agency_tracking.clearance_api import _build_injaz_pdf

	return _build_injaz_pdf(clearance_step_name)


def _run_render_cv(applicant_name=None, **kwargs):
	from agency_tracking.cv_api import _render_cv_pdf

	applicant = frappe.get_doc("Applicant", applicant_name)
	return _render_cv_pdf(applicant), f"CV_{applicant_name}.pdf"


def _run_render_batch_invoice(batch_name=None, **kwargs):
	from agency_tracking.finance_engine import render_batch_invoice_pdf

	return render_batch_invoice_pdf(batch_name), f"{batch_name}-invoice.pdf"


# job_type -> (handler, "json" | "file" result kind, timeout seconds)
JOB_REGISTRY = {
	"Parse Passport": (_run_parse_passport, "json", 600),
	"Parse Contract": (_run_parse_contract, "json", 300),
	"Parse Visa": (_run_parse_visa, "json", 300),
	"Render Injaz PDF": (_run_render_injaz, "file", 300),
	"Render CV PDF": (_run_render_cv, "file", 300),
	"Render Batch Invoice PDF": (_run_render_batch_invoice, "file", 300),
}


def enqueue_job(job_type, reference_doctype=None, reference_name=None, **kwargs):
	"""Create a Background Job row and hand it to the RQ 'long' queue. Callers are the six
	enqueue_* whitelisted wrappers, which have already run the same permission gate and param
	validation as their synchronous twin -- this function trusts that and just does the queuing."""
	if job_type not in JOB_REGISTRY:
		frappe.throw(f"Unknown job_type '{job_type}'.", frappe.ValidationError)
	_, _, timeout = JOB_REGISTRY[job_type]

	doc = frappe.get_doc(
		{
			"doctype": "Background Job",
			"job_type": job_type,
			"status": "Queued",
			"requested_by": frappe.session.user,
			"reference_doctype": reference_doctype,
			"reference_name": reference_name,
			"input_json": kwargs,
		}
	).insert(ignore_permissions=True)

	# Mandatory: Frappe only auto-commits a request's writes on unsafe HTTP methods or an
	# explicit flag. Without this, the row can still be invisible (or rolled back) by the time
	# the worker tries to load it -- possibly before the worker even starts.
	frappe.db.commit()

	frappe.enqueue(
		"agency_tracking.background_jobs.run_background_job",
		queue="long",
		timeout=timeout,
		job_id=doc.name,
		bg_job=doc.name,
	)
	return doc.name


def run_background_job(bg_job):
	"""The actual RQ entrypoint -- invoked by Frappe via this dotted string path, so it must stay
	import-safe at module scope. Never raises: a failure is recorded on the Background Job doc,
	not propagated (the worker process must survive a bad job)."""
	job = frappe.get_doc("Background Job", bg_job)
	if job.status != "Queued":
		return  # already picked up -- guards against a duplicate/retried dequeue

	frappe.db.set_value("Background Job", bg_job, {"status": "Running", "started_at": now_datetime()})
	frappe.db.commit()

	handler, kind, _ = JOB_REGISTRY[job.job_type]
	kwargs = frappe.parse_json(job.input_json) if job.input_json else {}

	try:
		result = handler(**kwargs)
		updates = {"status": "Completed", "completed_at": now_datetime()}
		if kind == "json":
			updates["result_json"] = frappe.as_json(result)
		else:
			pdf_bytes, filename = result
			# Same insert-a-File pattern as cv_api._attach_cv_pdf -- is_private since these PDFs
			# carry PII (passport numbers, photos). Using db.set_value (not doc.save()) for every
			# Background Job mutation below means there's no held, now-stale in-memory doc to
			# reload afterward, unlike _attach_cv_pdf's own caller.
			file_doc = frappe.get_doc(
				{
					"doctype": "File",
					"file_name": filename,
					"attached_to_doctype": "Background Job",
					"attached_to_name": bg_job,
					"content": pdf_bytes,
					"is_private": 1,
				}
			).insert(ignore_permissions=True)
			updates["result_file"] = file_doc.file_url
			updates["result_json"] = frappe.as_json({"filename": filename, "file_url": file_doc.file_url})
		frappe.db.set_value("Background Job", bg_job, updates)
		frappe.db.commit()
	except Exception:
		# Roll back first -- a poisoned transaction must be cleared before the Failed status
		# write, or that write can silently never persist either.
		frappe.db.rollback()
		frappe.db.set_value(
			"Background Job",
			bg_job,
			{"status": "Failed", "error": frappe.get_traceback(), "completed_at": now_datetime()},
		)
		frappe.db.commit()
		frappe.log_error(title=f"Background Job failed: {bg_job}")

	# Best-effort: a notification failure must never flip a Completed job back to looking broken.
	try:
		from agency_tracking.notification_engine import notify

		final_status = frappe.db.get_value("Background Job", bg_job, "status")
		notify(
			job.requested_by,
			NOTIFY_TEMPLATE,
			{
				"job": bg_job,
				"job_type": job.job_type,
				"status": final_status,
				"reference_doctype": job.reference_doctype,
				"reference_name": job.reference_name,
			},
		)
	except Exception:
		frappe.log_error(title="Background Job notify failed")


@frappe.whitelist()
def get_job_status(job_name=None, **kwargs):
	"""Poll a Background Job of any job_type. The requester sees their own; Manager/Admin/
	System Manager see all (BackgroundJob.has_permission)."""
	if not job_name:
		frappe.throw("job_name is required.", frappe.ValidationError)
	if not frappe.db.exists("Background Job", job_name):
		frappe.throw(f"Background Job {job_name} not found.", frappe.DoesNotExistError)
	job = frappe.get_doc("Background Job", job_name)
	if not job.has_permission("read"):
		frappe.throw("Not permitted.", frappe.PermissionError)
	return {
		"name": job.name,
		"job_type": job.job_type,
		"status": job.status,
		"requested_by": job.requested_by,
		"reference_doctype": job.reference_doctype,
		"reference_name": job.reference_name,
		"started_at": job.started_at,
		"completed_at": job.completed_at,
		"result": frappe.parse_json(job.result_json) if job.result_json else None,
		"result_file": job.result_file,
		"error": job.error,
	}
