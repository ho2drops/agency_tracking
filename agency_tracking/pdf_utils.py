# Copyright (c) 2026, Agency and contributors
# License: MIT. See LICENSE
#
# Shared PDF-rendering helpers for the app's generated documents (CV, Injaz). Kept in one place
# so every generated document resolves file attachments and renders the same way.

import base64
import io
import mimetypes
import os

import frappe
from frappe.utils.pdf import get_pdf
from agency_tracking.db_errors import reraise_if_db_abort

# Code128 module patterns (values 0-106). Each string is six digits: alternating bar/space widths
# in modules, starting with a bar. 103=Start B, 106=Stop (with its trailing bar).
_CODE128_PATTERNS = [
	"212222", "222122", "222221", "121223", "121322", "131222", "122213", "122312", "132212", "221213",
	"221312", "231212", "112232", "122132", "122231", "113222", "123122", "123221", "223211", "221132",
	"221231", "213212", "223112", "312131", "311222", "321122", "321221", "312212", "322112", "322211",
	"212123", "212321", "232121", "111323", "131123", "131321", "112313", "132113", "132311", "211313",
	"231113", "231311", "112133", "112331", "132131", "113123", "113321", "133121", "313121", "211331",
	"231131", "213113", "213311", "213131", "311123", "311321", "331121", "312113", "312311", "332111",
	"314111", "221411", "431111", "111224", "111422", "121124", "121421", "141122", "141221", "112214",
	"112412", "122114", "122411", "142112", "142211", "241211", "221114", "413111", "241112", "134111",
	"111242", "121142", "121241", "114212", "124112", "124211", "411212", "421112", "421211", "212141",
	"214121", "412121", "111143", "111341", "131141", "114113", "114311", "411113", "411311", "113141",
	"114131", "311141", "411131", "211412", "211214", "211232", "2331112",
]


def code128_b_datauri(data, module_px=2, height_px=48, quiet=10):
	"""Render an ASCII string as a Code128-B barcode PNG and return it as a data: URI.

	Pure-Pillow so it works offline with no barcode package. Returns None for empty input (the
	template then simply shows no barcode)."""
	if not data:
		return None
	from PIL import Image, ImageDraw

	values = [104]  # Start Code B
	for ch in str(data):
		code = ord(ch)
		if 32 <= code <= 126:
			values.append(code - 32)
	checksum = (values[0] + sum(v * i for i, v in enumerate(values[1:], start=1))) % 103
	values.append(checksum)
	values.append(106)  # Stop

	widths = []
	for v in values:
		for w in _CODE128_PATTERNS[v]:
			widths.append(int(w))

	total_modules = sum(widths)
	width_px = total_modules * module_px + quiet * 2 * module_px
	img = Image.new("RGB", (width_px, height_px), "white")
	draw = ImageDraw.Draw(img)
	x = quiet * module_px
	bar = True  # patterns start with a bar
	for w in widths:
		w_px = w * module_px
		if bar:
			draw.rectangle([x, 0, x + w_px - 1, height_px - 1], fill="black")
		x += w_px
		bar = not bar

	buf = io.BytesIO()
	img.save(buf, format="PNG")
	return "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode()


def asset_datauri(*path_parts):
	"""Base64 data: URI for a static image bundled in the app (e.g. the MoFA emblem). Robust for
	wkhtmltopdf, which cannot fetch app-relative URLs. Returns None if the file is missing."""
	path = frappe.get_app_path("agency_tracking", *path_parts)
	if not os.path.exists(path):
		return None
	ext = os.path.splitext(path)[1].lstrip(".").lower() or "png"
	with open(path, "rb") as f:
		return f"data:image/{ext};base64," + base64.b64encode(f.read()).decode()


def resolve_file_src(url):
	"""Turn a stored Frappe file URL into something wkhtmltopdf can actually load.

	Private files (/private/files/...) are not readable over HTTP without an authenticated
	session, and even public /files URLs are unreliable inside the headless PDF renderer. When
	the file exists on disk we hand wkhtmltopdf an absolute file:// path so the image embeds
	directly; otherwise we pass the original value through unchanged (external URLs, data URIs,
	or a not-yet-present file — the template just shows its empty-state placeholder)."""
	if not url:
		return None
	if url.startswith(("http://", "https://", "data:", "file://")):
		return url

	base = rel = None
	if url.startswith("/private/files/"):
		base, rel = "private", url[len("/private/files/") :]
	elif url.startswith("/files/"):
		base, rel = "public", url[len("/files/") :]

	if base:
		path = frappe.get_site_path(base, "files", rel)
		if os.path.exists(path):
			return "file://" + os.path.abspath(path)
	return url


def attach_datauri(url):
	"""Base64 data: URI for a stored Frappe file field (Applicant.photograph, etc.) -- the same
	technique already used for asset_datauri/code128_b_datauri, extended to user-uploaded
	attachments. Unlike resolve_file_src's file:// path, this never depends on wkhtmltopdf's own
	filesystem access at render time (it may run as a different user/sandboxed process that can't
	read the site's private/files directory even though the path is valid) or on a network fetch
	for a not-actually-local URL -- the bytes are already inline in the HTML handed to it, so
	there's nothing left for the renderer to fail to load (and nothing for it to hang retrying).
	Returns None (never raises) if the value is empty, already a data:/external URL, or the
	underlying File record can't be found -- callers already treat a missing photo as an
	empty-state, same as before."""
	if not url:
		return None
	if url.startswith("data:"):
		return url

	from agency_tracking.storage_engine import is_r2_ref, get_object_bytes

	if is_r2_ref(url):
		# 2026-09-21: R2 is a private bucket now -- fetch bytes directly with our own R2
		# credentials rather than a network HTTP fetch, same reliability rationale as the local-
		# file case below (no dependency on wkhtmltopdf being able to reach anything over HTTP).
		try:
			content = get_object_bytes(url)
		except Exception:
			frappe.log_error(title="attach_datauri: could not read R2 object", message=url)
			return None
		content_type = mimetypes.guess_type(url)[0] or "image/jpeg"
		return f"data:{content_type};base64," + base64.b64encode(content).decode()
	if url.startswith(("http://", "https://")):
		# Not one of our own R2 refs -- some other already-independently-fetchable URL.
		return url

	file_name = frappe.db.get_value("File", {"file_url": url}, "name")
	if not file_name:
		return None
	try:
		file_doc = frappe.get_doc("File", file_name)
		content = file_doc.get_content()
	except Exception as exc:
		reraise_if_db_abort(exc)
		frappe.log_error(title="attach_datauri: could not read file", message=f"{url} ({file_name})")
		return None

	# File.content_type is never a persisted field (frappe/core/doctype/file/file.py only sets
	# it as a transient in-memory attribute during the original upload hook) -- a document
	# reloaded via frappe.get_doc, as both callers here always do, never has it and raises
	# AttributeError. mimetypes.guess_type is the same fallback Frappe's own file-serving code
	# uses (frappe/utils/response.py).
	content_type = mimetypes.guess_type(file_doc.file_name)[0] or "image/jpeg"
	return f"data:{content_type};base64," + base64.b64encode(content).decode()


def embed_image_datauri(url, max_dimension=1000, jpeg_quality=82):
	"""Same file-resolution/reliability contract as attach_datauri (never raises, same None/
	passthrough rules for empty/data:/http(s) values) but downsizes the image first if it's
	larger than max_dimension on either side.

	2026-09-10: CV/Injaz PDFs embed 1-3 user-uploaded photos (phone-camera originals, routinely
	several MB / multi-thousand-px) with no resizing anywhere in the upload path -- every PDF
	render then makes wkhtmltopdf decode and downscale each one from scratch just to display it
	at a few hundred px, which is the dominant cost of those two documents (the invoice, by
	contrast, only ever embeds the agency's own small pre-sized logo/stamp -- nothing
	user-uploaded -- which is why it's fast). Re-encoding as JPEG only kicks in when a resize
	actually happened, so an already-small/optimized image is returned untouched -- no repeated
	quality loss on something that didn't need it."""
	if not url:
		return None
	if url.startswith("data:"):
		return url

	from agency_tracking.storage_engine import is_r2_ref, get_object_bytes

	source_label = url
	if is_r2_ref(url):
		# 2026-09-21: R2 is a private bucket now -- fetch bytes directly with our own R2
		# credentials, same as attach_datauri.
		try:
			content = get_object_bytes(url)
		except Exception:
			frappe.log_error(title="embed_image_datauri: could not read R2 object", message=url)
			return None
		content_type = mimetypes.guess_type(url)[0] or "image/jpeg"
	elif url.startswith(("http://", "https://")):
		return url
	else:
		file_name = frappe.db.get_value("File", {"file_url": url}, "name")
		if not file_name:
			return None
		source_label = f"{url} ({file_name})"
		try:
			file_doc = frappe.get_doc("File", file_name)
			content = file_doc.get_content()
		except Exception as exc:
			reraise_if_db_abort(exc)
			frappe.log_error(title="embed_image_datauri: could not read file", message=source_label)
			return None

		# File.content_type is never a persisted field (frappe/core/doctype/file/file.py only sets
		# it as a transient in-memory attribute during the original upload hook) -- a document
		# reloaded via frappe.get_doc, as both callers here always do, never has it and raises
		# AttributeError. mimetypes.guess_type is the same fallback Frappe's own file-serving code
		# uses (frappe/utils/response.py).
		content_type = mimetypes.guess_type(file_doc.file_name)[0] or "image/jpeg"

	try:
		from PIL import Image

		img = Image.open(io.BytesIO(content))
		if img.width > max_dimension or img.height > max_dimension:
			img.thumbnail((max_dimension, max_dimension), Image.LANCZOS)
			if img.mode not in ("RGB", "L"):
				img = img.convert("RGB")
			buf = io.BytesIO()
			img.save(buf, format="JPEG", quality=jpeg_quality, optimize=True)
			content = buf.getvalue()
			content_type = "image/jpeg"
	except Exception:
		# Best-effort -- an unresizable/corrupt image still embeds at its original size rather
		# than showing an empty-state placeholder for a file that does exist.
		frappe.log_error(title="embed_image_datauri: resize failed, embedding original", message=source_label)

	return f"data:{content_type};base64," + base64.b64encode(content).decode()


def render_pdf(template, context):
	"""Render a Jinja template path to PDF bytes via Frappe's standard wkhtmltopdf path.

	Imports get_pdf directly rather than relying on `frappe.utils.pdf` being reachable as an
	attribute of the already-imported `frappe.utils` package -- that only holds true if some
	other code path in the same process happened to import the pdf submodule first (true by
	accident in most web-request workers, false in a freshly-started RQ background worker,
	where this raised a bare AttributeError before every PDF render could even begin)."""
	html = frappe.render_template(template, context)
	return get_pdf(html)
