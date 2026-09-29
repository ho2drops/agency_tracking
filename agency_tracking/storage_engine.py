# Copyright (c) 2026, Agency and contributors
# License: MIT. See LICENSE
#
# Cloudflare R2 (S3-compatible object storage), for the documents that don't belong in
# Frappe's own local file storage: Finance receipt images, generated Injaz papers, generated
# CV PDFs, parsed contracts/visas, applicant photos/videos. One upload function, reused
# everywhere. Key convention:
#   agency/{applicant_name}/{category}/{filename}
# where category is one of "cv", "injaz", "finance-receipts", "contracts", "visas", "photos",
# "videos".
#
# Credentials are left empty until an admin enters them in Storage Settings -- calls fail with a
# clear "not configured" error in the meantime, never crash the calling flow (same honesty
# standard as fetch_daily_fx_rates/push notifications elsewhere in this app). Once credentials
# and a bucket name are provided, the bucket itself is auto-provisioned on first use
# (ensure_bucket_exists): head_bucket to check, create_bucket if it 404s -- so an admin only has
# to create the R2 API token, not pre-create the bucket by hand.

import mimetypes

import frappe

STORAGE_CATEGORIES = {"cv", "injaz", "finance-receipts", "contracts", "visas", "photos", "videos"}

# Per-process cache of buckets already verified/created this worker's lifetime, so head_bucket
# isn't re-issued on every single upload. Keyed by bucket name.
_verified_buckets = set()


def _r2_client():
	settings = frappe.get_single("Storage Settings")
	secret = settings.get_password("r2_secret_access_key", raise_exception=False)
	if not (settings.r2_account_id and settings.r2_access_key_id and secret and settings.r2_bucket_name):
		frappe.throw(
			"Cloudflare R2 is not configured yet (Storage Settings). "
			"An admin needs to enter the R2 credentials and bucket name.",
			frappe.ValidationError,
		)
	import boto3
	from botocore.config import Config

	return (
		boto3.client(
			"s3",
			endpoint_url=f"https://{settings.r2_account_id}.r2.cloudflarestorage.com",
			aws_access_key_id=settings.r2_access_key_id,
			aws_secret_access_key=secret,
			region_name="auto",
			# R2 rejects SigV2 presigned URLs (401); boto3 can fall back to V2 for presigning.
			config=Config(signature_version="s3v4"),
		),
		settings,
	)


def ensure_bucket_exists(client, bucket_name):
	"""Verify the bucket is reachable; auto-create it if it doesn't exist yet.

	Contract (Phase 2): given valid credentials, an admin should not have to pre-create the
	bucket. We head_bucket first; a 404 / NoSuchBucket means "valid creds, bucket just isn't
	there yet" -> create_bucket. Any other ClientError (403 unauthorized, invalid access key,
	signature mismatch, endpoint unreachable) is surfaced as a clear frappe.ValidationError so
	the caller sees actionable feedback instead of an unhandled 500. Result is cached per
	process so the head_bucket round-trip happens once, not on every upload."""
	if bucket_name in _verified_buckets:
		return

	from botocore.exceptions import ClientError, BotoCoreError, EndpointConnectionError

	try:
		client.head_bucket(Bucket=bucket_name)
		_verified_buckets.add(bucket_name)
		return
	except ClientError as e:
		error_code = str(e.response.get("Error", {}).get("Code", ""))
		status = e.response.get("ResponseMetadata", {}).get("HTTPStatusCode")
		if error_code in ("404", "NoSuchBucket") or status == 404:
			# Valid creds, bucket absent -> provision it.
			try:
				client.create_bucket(Bucket=bucket_name)
				_verified_buckets.add(bucket_name)
				return
			except ClientError as ce:
				# A concurrent worker may have just created it -- treat "already owned by you" as success.
				ce_code = str(ce.response.get("Error", {}).get("Code", ""))
				if ce_code in ("BucketAlreadyOwnedByYou", "BucketAlreadyExists"):
					_verified_buckets.add(bucket_name)
					return
				frappe.throw(
					f"R2 bucket '{bucket_name}' does not exist and could not be created ({ce_code or ce}). "
					"Check that the API token has bucket-create permission.",
					frappe.ValidationError,
				)
		elif error_code in ("403", "AccessDenied", "InvalidAccessKeyId", "SignatureDoesNotMatch"):
			frappe.throw(
				f"R2 rejected the credentials while accessing bucket '{bucket_name}' ({error_code}). "
				"Verify the Account ID, Access Key ID, and Secret Access Key in Storage Settings.",
				frappe.ValidationError,
			)
		else:
			frappe.throw(
				f"Could not verify R2 bucket '{bucket_name}': {error_code or e}.",
				frappe.ValidationError,
			)
	except (EndpointConnectionError, BotoCoreError) as e:
		frappe.throw(
			f"Could not reach Cloudflare R2 to verify bucket '{bucket_name}': {e}. "
			"Check the Account ID and network connectivity.",
			frappe.ValidationError,
		)


def build_object_key(applicant_name, category, filename):
	if category not in STORAGE_CATEGORIES:
		frappe.throw(
			f"Unknown storage category '{category}'. Expected one of: {', '.join(sorted(STORAGE_CATEGORIES))}.",
			frappe.ValidationError,
		)
	return f"agency/{applicant_name}/{category}/{filename}"


R2_REF_PREFIX = "r2:"


def is_r2_ref(value):
	"""True if `value` is this app's internal R2 reference tag, not a real URL/local file path."""
	return bool(value) and isinstance(value, str) and value.startswith(R2_REF_PREFIX)


def _key_from_ref(value):
	return value[len(R2_REF_PREFIX):]


def upload_to_r2(file_content: bytes, key: str, content_type: str | None = None) -> str:
	"""Uploads raw bytes to the configured R2 bucket at `key`. Returns an internal reference
	("r2:{key}"), deliberately NOT a public URL.

	2026-09-21 decision, reversing this function's original design: the bucket stays PRIVATE.
	A public bucket makes every object fetchable by anyone who has or guesses its URL -- and this
	app's key convention (agency/{applicant_name}/{category}/{filename}, with sequential
	Applicant names like APP-00001) is trivially enumerable. Someone could script through every
	APP-##### and scrape every passport scan/photo/receipt in the system with zero
	authentication -- there's no per-object ACL on a public R2 bucket, it's all-or-nothing.

	Callers never use this reference directly -- they resolve it only after their OWN permission
	check has already passed: resolve_r2_url() for a short-lived signed URL (redirect-to-R2
	cases, e.g. portal_api.get_candidate_photo), or get_object_bytes() to pull the raw bytes
	in-process (server-side PDF embedding, see pdf_utils.attach_datauri/embed_image_datauri).
	Ensures the bucket exists first (auto-creates on 404). Raises a clear ValidationError (not a
	crash) if Storage Settings isn't configured or the credentials/bucket can't be reached."""
	client, settings = _r2_client()
	ensure_bucket_exists(client, settings.r2_bucket_name)
	extra_args = {"ContentType": content_type} if content_type else {}
	client.put_object(Bucket=settings.r2_bucket_name, Key=key, Body=file_content, **extra_args)
	return f"{R2_REF_PREFIX}{key}"


def get_object_bytes(ref_or_key: str) -> bytes:
	"""Fetches an object's raw bytes directly from R2 (accepts an "r2:{key}" reference or a bare
	key) for a caller that needs the actual content in-process -- e.g. embedding a photo as a
	PDF data: URI -- not a URL for someone else to fetch later. This call itself IS the
	authenticated read (via the account's own R2 credentials); no presigning involved."""
	client, settings = _r2_client()
	key = _key_from_ref(ref_or_key) if is_r2_ref(ref_or_key) else ref_or_key
	obj = client.get_object(Bucket=settings.r2_bucket_name, Key=key)
	return obj["Body"].read()


def resolve_r2_url(value, expires_in=7200):
	"""If `value` is an "r2:{key}" reference, returns a signed GET URL (2 hour default --
	2026-09-21: long enough that a candidate video being watched/scrubbed doesn't have its URL
	expire mid-playback, per product decision) that's independently fetchable without going
	through this app again. Generates the
	URL only when actually needed -- this function does no permission gating itself, it trusts
	the caller already did that before calling it (e.g. portal_api._assert_can_view_candidate).
	Anything else (blank, a local Frappe file path, an old pre-2026-09-21 public R2 URL from
	before the bucket was made private) passes through unchanged."""
	if not is_r2_ref(value):
		return value
	client, settings = _r2_client()
	return client.generate_presigned_url(
		"get_object",
		Params={"Bucket": settings.r2_bucket_name, "Key": _key_from_ref(value)},
		ExpiresIn=expires_in,
	)


def resolve_r2_fields(rows, fieldnames, expires_in=7200):
	"""Bulk in-place variant of resolve_r2_url for a list of dict-like rows (e.g. a
	frappe.get_all result) -- mutates each row's given fields, replacing any R2 reference with a
	signed URL. Builds the R2 client at most once per call (skips entirely, no credentials
	round-trip at all, if nothing in `rows` actually holds an R2 reference -- keeps this a safe
	no-op for not-yet-migrated data even when Storage Settings isn't configured)."""
	if not rows:
		return rows

	def _get(row, f):
		return row.get(f) if hasattr(row, "get") else row[f]

	def _set(row, f, v):
		if hasattr(row, "__setitem__"):
			row[f] = v
		else:
			setattr(row, f, v)

	if not any(is_r2_ref(_get(row, f)) for row in rows for f in fieldnames):
		return rows

	client, settings = _r2_client()
	for row in rows:
		for fieldname in fieldnames:
			value = _get(row, fieldname)
			if is_r2_ref(value):
				_set(row, fieldname, client.generate_presigned_url(
					"get_object",
					Params={"Bucket": settings.r2_bucket_name, "Key": _key_from_ref(value)},
					ExpiresIn=expires_in,
				))
	return rows


@frappe.whitelist()
def test_storage_connection():
	"""Admin setup helper: verifies Storage Settings credentials and that the bucket is ready
	(creating it if missing), then does a tiny round-trip write/delete to confirm object-level
	access. Returns a status dict rather than throwing, so a settings-page 'Test Connection'
	button can render success/failure cleanly. Never raises into the caller."""
	frappe.only_for(("System Manager", "Administrator"), message=True)  # message: QA P7-03
	try:
		client, settings = _r2_client()
	except frappe.ValidationError as e:
		return {"status": "not_configured", "message": str(e)}

	bucket = settings.r2_bucket_name
	try:
		ensure_bucket_exists(client, bucket)
	except frappe.ValidationError as e:
		return {"status": "error", "bucket": bucket, "message": str(e)}

	# Object-level round-trip: confirms put/delete work, not just bucket existence.
	probe_key = "agency/_connection_test/.probe"
	try:
		client.put_object(Bucket=bucket, Key=probe_key, Body=b"ok", ContentType="text/plain")
		client.delete_object(Bucket=bucket, Key=probe_key)
	except Exception as e:
		return {
			"status": "bucket_ready_write_failed",
			"bucket": bucket,
			"message": f"Bucket reachable but object write failed: {e}",
		}

	return {
		"status": "success",
		"bucket": bucket,
		"private": True,
		"message": f"Connected to R2 bucket '{bucket}' and verified read/write access. "
		"Bucket is private by design -- objects are only reachable via short-lived signed URLs "
		"this app generates after its own permission checks pass, never a public bucket URL.",
	}


def migrate_attach_to_r2(doc, fieldname, category, applicant_name=None):
	"""Shared attach-upload path (2026-08-29, extended 2026-09-21 to media beyond receipts) --
	same behavior everywhere a receipt/photo/video is captured: Applicant Transaction.receipt_image,
	Injaz Attempt.receipt_photo, Clearance Step Payment.receipt_url, Applicant.photograph/
	photo_full_body/experience_video. The field itself stays a normal Frappe Attach (so the
	browser gets Frappe's native upload widget, nothing custom to build) -- this just runs on
	save, notices the value is still a *local* Frappe file, uploads it to R2, repoints the field
	at an internal "r2:{key}" reference (NOT a usable URL -- see upload_to_r2's docstring for why
	the bucket is private), and deletes the local copy so nothing is stored twice. Callers that
	need to actually serve/embed this value must resolve it first via resolve_r2_url /
	resolve_r2_fields (URL, for a redirect) or get_object_bytes (raw bytes, for PDF embedding).

	Best-effort, like every other document-generation path in this app (contract parsing, FX
	fetch): if Storage Settings isn't configured yet, or anything else goes wrong, log it and
	leave the local file in place (still viewable via Frappe's own file serving) rather than
	blocking the save. Safe to call unconditionally on every save -- a value that's already an
	R2 reference (doesn't start with /files/ or /private/files/) is a no-op.
	"""
	value = doc.get(fieldname)
	if not value or not (value.startswith("/files/") or value.startswith("/private/files/")):
		return

	try:
		file_name = frappe.db.get_value("File", {"file_url": value}, "name")
		if not file_name:
			return
		file_doc = frappe.get_doc("File", file_name)
		content = file_doc.get_content()
		key = build_object_key(applicant_name or doc.name, category, file_doc.file_name)
		# File has no content_type field (reading it raised AttributeError, which the except below
		# swallowed -- so nothing ever reached R2; QA S-03a). Derive it from the name instead.
		content_type = mimetypes.guess_type(file_doc.file_name or "")[0]
		r2_ref = upload_to_r2(content, key, content_type=content_type)
		doc.set(fieldname, r2_ref)
		frappe.delete_doc("File", file_name, ignore_permissions=True, force=True)
	except Exception:
		frappe.log_error(title="R2 receipt migration failed", message=f"{doc.doctype} {doc.name} {fieldname}")
