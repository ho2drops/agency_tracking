# Copyright (c) 2026, Agency and contributors
# License: MIT. See LICENSE

import os
import re
import unicodedata
import datetime
import decimal

import frappe
from frappe.utils import getdate

from agency_tracking.roles import INTERNAL_STAFF_ROLES, require_internal_staff

# ─────────────────────────────────────────────────────────────────────────────
# 1. Unicode & Arabic Normalization Helpers
# ─────────────────────────────────────────────────────────────────────────────
ARABIC_PRESENTATION_MAP = {
	'\ufe80': '\u0621', '\ufe81': '\u0622', '\ufe82': '\u0622', '\ufe83': '\u0623',
	'\ufe84': '\u0623', '\ufe85': '\u0624', '\ufe86': '\u0624', '\ufe87': '\u0625',
	'\ufe88': '\u0625', '\ufe89': '\u0626', '\ufe8a': '\u0626', '\ufe8b': '\u0626',
	'\ufe8c': '\u0626', '\ufe8d': '\u0627', '\ufe8e': '\u0627', '\ufe8f': '\u0628',
	'\ufe90': '\u0628', '\ufe91': '\u0628', '\ufe92': '\u0628', '\ufe93': '\u0629',
	'\ufe94': '\u0629', '\ufe95': '\u062a', '\ufe96': '\u062a', '\ufe97': '\u062a',
	'\ufe98': '\u062a', '\ufe99': '\u062b', '\ufe9a': '\u062b', '\ufe9b': '\u062b',
	'\ufe9c': '\u062b', '\ufe9d': '\u062c', '\ufe9e': '\u062c', '\ufe9f': '\u062c',
	'\ufea0': '\u062c', '\ufea1': '\u062d', '\ufea2': '\u062d', '\ufea3': '\u062d',
	'\ufea4': '\u062d', '\ufea5': '\u062e', '\ufea6': '\u062e', '\ufea7': '\u062e',
	'\ufea8': '\u062e', '\ufea9': '\u062f', '\ufeaa': '\u062f', '\ufeab': '\u0630',
	'\ufeac': '\u0630', '\ufead': '\u0631', '\ufeae': '\u0631', '\ufeaf': '\u0632',
	'\ufeb0': '\u0632', '\ufeb1': '\u0633', '\ufeb2': '\u0633', '\ufeb3': '\u0633',
	'\ufeb4': '\u0633', '\ufeb5': '\u0634', '\ufeb6': '\u0634', '\ufeb7': '\u0634',
	'\ufeb8': '\u0634', '\ufeb9': '\u0635', '\ufeba': '\u0635', '\ufebb': '\u0635',
	'\ufebc': '\u0635', '\ufebd': '\u0636', '\ufebe': '\u0636', '\ufebf': '\u0636',
	'\ufec0': '\u0636', '\ufec1': '\u0637', '\ufec2': '\u0637', '\ufec3': '\u0637',
	'\ufec4': '\u0637', '\ufec5': '\u0638', '\ufec6': '\u0638', '\ufec7': '\u0638',
	'\ufec8': '\u0638', '\ufec9': '\u0639', '\ufeca': '\u0639', '\ufecb': '\u0639',
	'\ufecc': '\u0639', '\ufecd': '\u063a', '\ufece': '\u063a', '\ufecf': '\u063a',
	'\ufed0': '\u063a', '\ufed1': '\u0641', '\ufed2': '\u0641', '\ufed3': '\u0641',
	'\ufed4': '\u0641', '\ufed5': '\u0642', '\ufed6': '\u0642', '\ufed7': '\u0642',
	'\ufed8': '\u0642', '\ufed9': '\u0643', '\ufeda': '\u0643', '\ufedb': '\u0643',
	'\ufedc': '\u0643', '\ufedd': '\u0644', '\ufede': '\u0644', '\ufedf': '\u0644',
	'\ufee0': '\u0644', '\ufee1': '\u0645', '\ufee2': '\u0645', '\ufee3': '\u0645',
	'\ufee4': '\u0645', '\ufee5': '\u0646', '\ufee6': '\u0646', '\ufee7': '\u0646',
	'\ufee8': '\u0646', '\ufee9': '\u0647', '\ufeea': '\u0647', '\ufeeb': '\u0647',
	'\ufeec': '\u0647', '\ufeed': '\u0648', '\ufeee': '\u0648', '\ufeef': '\u0649',
	'\ufef0': '\u0649', '\ufef1': '\u064a', '\ufef2': '\u064a', '\ufef3': '\u064a',
	'\ufef4': '\u064a', '\ufef5': '\u0644\u0622', '\ufef6': '\u0644\u0622',
	'\ufef7': '\u0644\u0623', '\ufef8': '\u0644\u0623', '\ufef9': '\u0644\u0625',
	'\ufefa': '\u0644\u0625', '\ufefb': '\u0644\u0627', '\ufefc': '\u0644\u0627',
}


def normalize_text(text):
	"""
	Cleans raw text extracted from PDF:
	- Normalizes Unicode (NFKC)
	- Replaces Arabic presentation forms with base Arabic
	- Strips unmapped Private Use Area (PUA) corrupted characters (\uE000-\uF8FF, \uFFF0-\uFFFF)
	- Strips bidi / direction markers and hidden control marks
	- Normalizes multiple whitespace variants
	"""
	if not text:
		return ""

	chars = []
	for ch in str(text):
		chars.append(ARABIC_PRESENTATION_MAP.get(ch, ch))
	normalized = "".join(chars)

	normalized = unicodedata.normalize("NFKC", normalized)
	# Strip unmapped Private Use Area (PUA) glyphs and control marks
	normalized = re.sub(r'[\ue000-\uf8ff\ufff0-\uffff\u0080-\u009f]', '', normalized)
	normalized = re.sub(r'[\u200e\u200f\u202a-\u202e\u2066-\u2069\ufeff\u200b\u00ad]', '', normalized)
	normalized = re.sub(r'[\xa0\u2000-\u200a\u202f\u205f\u3000]', ' ', normalized)
	return normalized


# ─────────────────────────────────────────────────────────────────────────────
# 2. Multi-Line Text Structurizer Engine
# ─────────────────────────────────────────────────────────────────────────────
class ContractTextStructurizer:
	"""
	Parses and organizes raw PDF text blocks, multi-line strings, and tables into
	structured semantic lines, section blocks, and key-value maps.
	Handles standard bilateral Saudi Musaned employment contract layouts and Kuwait contracts.
	"""

	def __init__(self, raw_blocks_or_text):
		self.raw_input = raw_blocks_or_text
		self.clean_lines = []
		self.unified_text = ""
		self.sections = {
			"header": "",
			"employer": "",
			"recruiting_agency": "",
			"worker": "",
			"origin_agency": "",
			"financial": "",
		}
		self._process()

	def _process(self):
		raw_lines = []
		if isinstance(self.raw_input, list):
			for b in self.raw_input:
				if isinstance(b, (list, tuple)) and len(b) >= 5 and isinstance(b[4], str):
					raw_lines.extend(b[4].splitlines())
				elif isinstance(b, str):
					raw_lines.extend(b.splitlines())
		else:
			raw_lines = str(self.raw_input or "").splitlines()

		clean_lines = []
		for line in raw_lines:
			cleaned = normalize_text(line).strip()
			if cleaned:
				clean_lines.append(cleaned)

		self.clean_lines = clean_lines
		self.unified_text = "\n".join(clean_lines)
		self._partition_sections()

	def _partition_sections(self):
		"""Splits the full text into semantic contract sections."""
		current_section = "header"
		section_lines = {k: [] for k in self.sections}

		for line in self.clean_lines:
			lower = line.lower()

			# 1. Section A: Employer (First Party)
			if any(k in lower for k in [
				"a. employer", "ا. صاحب العمل", "صاحب العمل:", "بيانات صاحب العمل",
				"first party", "الطرف الأول"
			]) and not any(k in lower for k in ["hereinafter called", "represented in", "اسم صاحب", "هاتف", "signature of", "توقيع"]):
				current_section = "employer"
			# 2. Section: Saudi Recruiting Agency (Second Party)
			elif any(k in lower for k in [
				"saudi recruiting agency", "وكالة الاستقدام السعودية", "مكتب الاستقدام السعودي",
				"represented in the kingdom of saudi arabia",
				"second party", "الطرف الثاني", "مكتب الاستقدام:"
			]) and not any(k in lower for k in ["signature of", "توقيع"]):
				current_section = "recruiting_agency"
			# 3. Section B: Domestic Service Worker
			elif any(k in lower for k in [
				"b. domestic service worker", "ب. العامل المنزلي", "العامل المنزلي / العاملة المنزلية",
				"domestic service worker", "domestic worker", "بيانات العامل", "worker details"
			]) and not any(k in lower for k in ["hereinafter called dsw", "represented in his", "signature of", "توقيع"]):
				current_section = "worker"
			# 4. Section: Foreign Origin Agency (Third Party / Ethiopia Agency)
			elif any(k in lower for k in [
				"dsw represented", "represented in his", "represented in her", "her country agency",
				"وكالة الاستقدام بالخارج", "وكالة الاستقدام:", "ethiopian recruitment agency",
				"وكالة تصدير العمالة", "foreign agency", "third party", "الطرف الثالث"
			]) and not any(k in lower for k in ["signature of", "توقيع"]):
				current_section = "origin_agency"
			# 5. Financial / Wage Section
			elif any(k in lower for k in ["6. wage", "6. الأجر", "wage", "الأجور"]):
				current_section = "financial"

			section_lines[current_section].append(line)

		for k in self.sections:
			self.sections[k] = "\n".join(section_lines[k])


# ─────────────────────────────────────────────────────────────────────────────
# 3. Multi-Engine PDF Text Extraction
# ─────────────────────────────────────────────────────────────────────────────
def extract_text_from_pdf(file_path):
	"""
	Extracts text blocks/lines from PDF using available PDF engines in order of priority:
	1. PyMuPDF (fitz) - spatial block sorting
	2. pypdf - page stream extraction
	3. pdfplumber - structured text layout
	"""
	if not file_path or not os.path.exists(file_path):
		return ""

	# 1. PyMuPDF
	try:
		try:
			import pymupdf as fitz
		except ImportError:
			import fitz

		doc = fitz.open(file_path)
		blocks = []
		for page_num in range(len(doc)):
			page = doc[page_num]
			page_blocks = page.get_text("blocks")
			sorted_blocks = sorted(page_blocks, key=lambda b: (round(b[1] / 10) * 10, b[0]))
			for b in sorted_blocks:
				if len(b) >= 5 and b[4].strip():
					blocks.append(b[4].strip())
		if blocks:
			return "\n".join(blocks)
	except Exception as e:
		frappe.logger().warning(f"PyMuPDF (fitz) extraction failed: {e}")

	# 2. pypdf
	try:
		import pypdf
		reader = pypdf.PdfReader(file_path)
		lines = []
		for page in reader.pages:
			t = page.extract_text()
			if t:
				lines.extend(t.splitlines())
		if lines:
			return "\n".join(lines)
	except Exception as e:
		frappe.logger().warning(f"pypdf extraction failed: {e}")

	# 3. pdfplumber
	try:
		import pdfplumber
		lines = []
		with pdfplumber.open(file_path) as pdf:
			for page in pdf.pages:
				t = page.extract_text()
				if t:
					lines.extend(t.splitlines())
		if lines:
			return "\n".join(lines)
	except Exception as e:
		frappe.logger().warning(f"pdfplumber extraction failed: {e}")

	return ""


# ─────────────────────────────────────────────────────────────────────────────
# 4. Helper Extraction Utilities & Date Parsing
# ─────────────────────────────────────────────────────────────────────────────
GENERIC_TITLES = {
	"صاحب العمل", "الطرف الأول", "مكتب الاستقدام", "الطرف الثاني", "شركة الاستقدام",
	"الطرف الثالث", "وكالة الاستقدام بالخارج", "العامل", "العاملة", "العامل المنزلي",
	"بيانات العامل", "first party", "second party", "third party", "employer",
	"recruiting agency", "recruitment office", "foreign agency", "domestic worker",
	"domestic service worker", "saudi recruiting agency"
}


def clean_extracted_value(val):
	"""Cleans an extracted field value: removes colon prefixes, bounding quotes, extra spaces, generic headers, and bilingual label remnants."""
	if val is None:
		return None
	val_str = str(val).strip()

	# Strip any unmapped PUA characters
	val_str = re.sub(r'[\ue000-\uf8ff\ufff0-\uffff\u0080-\u009f]', '', val_str)

	# If bilingual label remnant like " / اسم العاملة: Meseret Hailemariam Desta"
	if ":" in val_str:
		prefix, suffix = val_str.split(":", 1)
		if len(prefix) < 30 and (
			prefix.startswith("/") or prefix.startswith("|") or
			any(k in prefix.lower() for k in ["اسم", "name", "worker", "employer", "agency", "office", "الطرف", "صاحب", "مكتب", "وكالة", "ة", "رقم", "street", "الشارع", "city", "المدينة"])
		):
			val_str = suffix.strip()

	val_str = re.sub(r'^[:=\-–—\s|/#]+', '', val_str)
	val_str = re.sub(r'[:=\-–—\s|/#]+$', '', val_str)
	val_str = re.sub(r'\s+', ' ', val_str).strip()

	if re.match(r'^[A-Za-z0-9@_+\s.,&\'\-\u0600-\u064A\u0660-\u0669،]+$', val_str):
		val_str = val_str.strip()
	elif re.search(r'^[A-Za-z0-9@_+\s.,&\'\-]{4,}', val_str):
		m_eng = re.search(r'^([A-Za-z0-9@_+\s.,&\'\-]{4,})', val_str)
		if m_eng:
			val_str = m_eng.group(1).strip()

	if val_str.startswith("(") and val_str.endswith(")"):
		inner = val_str[1:-1].strip()
		if inner.lower() in GENERIC_TITLES:
			return None
	if val_str.lower() in GENERIC_TITLES:
		return None
	return val_str if val_str else None


def extract_field_from_text(text_or_lines, patterns, flags=re.IGNORECASE):
	"""
	Extracts clean field value from text.
	Handles same-line and multi-line label/value patterns.
	"""
	if not text_or_lines:
		return None

	if isinstance(text_or_lines, list):
		text = "\n".join(text_or_lines)
		lines = text_or_lines
	else:
		text = str(text_or_lines)
		lines = [line.strip() for line in text.splitlines() if line.strip()]

	for pat in patterns:
		for m in re.finditer(pat, text, flags | re.MULTILINE):
			val = m.group(1).strip()
			if "\n" in val:
				val = val.split("\n")[0].strip()
			val = re.split(r'\s{2,}|\t', val)[0].strip()
			cleaned = clean_extracted_value(val)
			if cleaned:
				return cleaned

	label_patterns = []
	for pat in patterns:
		prefix_match = re.match(r'^\(?\?:?([^()]+)\)?', pat)
		if prefix_match:
			label_patterns.append(prefix_match.group(1))

	for i, line in enumerate(lines):
		line_clean = line.strip()
		for l_pat in label_patterns:
			if re.search(rf'^{l_pat}[:=\-–#]?$', line_clean, flags=flags) and i + 1 < len(lines):
				next_val = lines[i + 1].strip()
				if next_val and not any(re.search(rf'^{lp}[:=\-–#]?$', next_val, flags=flags) for lp in label_patterns):
					cleaned = clean_extracted_value(next_val)
					if cleaned:
						return cleaned

	return None


def normalize_date_string(date_str):
	"""Converts various date formats (DD/MM/YYYY, YYYY-MM-DD, etc.) to ISO YYYY-MM-DD."""
	if not date_str:
		return None
	d = str(date_str).strip()

	m = re.search(r'([0-9]{1,4}[-/.][0-9]{1,2}[-/.][0-9]{1,4})', d)
	if not m:
		return None
	raw = m.group(1).replace("/", "-").replace(".", "-")
	parts = raw.split("-")

	try:
		if len(parts) == 3:
			if len(parts[0]) == 4:
				year, month, day = int(parts[0]), int(parts[1]), int(parts[2])
			else:
				day, month, year = int(parts[0]), int(parts[1]), int(parts[2])

			dt = datetime.date(year, month, day)
			return str(dt)
	except Exception:
		pass

	try:
		return str(getdate(d))
	except Exception:
		return None


def extract_contract_signed_date(text):
	"""Best-effort extraction of the contract's own signed date from raw contract text."""
	if not text:
		return None
	patterns = [
		r'corresponding\s*to\s*\(?([0-9]{1,2}[-/.][0-9]{1,2}[-/.][0-9]{2,4})\)?',
		r'بتاريخ\s*\(?([0-9]{1,2}[-/.][0-9]{1,2}[-/.][0-9]{2,4})\)?',
		r'(?:تاريخ\s*إبرام\s*العقد|تاريخ\s*ابرام\s*العقد|تاريخ\s*العقد|تاريخ\s*توقيع\s*العقد|تاريخ\s*الاتفاقية|تاريخ\s*الإصدار|تاريخ\s*الاصدار)\s*[:=\-–]?\s*\(?([0-9]{1,4}[-/.][0-9]{1,2}[-/.][0-9]{1,4})\)?',
		r'(?:contract\s*date|agreement\s*date|date\s*of\s*agreement|signed\s*on|signing\s*date|issue\s*date|date)\s*[:=\-–]?\s*\(?([0-9]{1,4}[-/.][0-9]{1,2}[-/.][0-9]{1,4})\)?',
	]
	for pattern in patterns:
		match = re.search(pattern, text, re.IGNORECASE)
		if match:
			normalized = normalize_date_string(match.group(1))
			if normalized:
				return normalized
	return None


def calculate_contract_end_date(contract_date, duration_str="2 Years"):
	"""Computes contract end date by adding contract duration to start date."""
	if not contract_date:
		return None
	try:
		from dateutil.relativedelta import relativedelta
		start = getdate(contract_date)
		dur_str = str(duration_str or "2 Years").strip().lower()

		if "month" in dur_str or "شهر" in dur_str:
			m = re.search(r'\d+', dur_str)
			months = int(m.group(0)) if m else 24
			return str(start + relativedelta(months=months))
		elif "سنتين" in dur_str or "2" in dur_str:
			return str(start + relativedelta(years=2))
		elif "year" in dur_str or "سنة" in dur_str or "1" in dur_str:
			m = re.search(r'\d+', dur_str)
			years = int(m.group(0)) if m else 2
			return str(start + relativedelta(years=years))
		else:
			return str(start + relativedelta(years=2))
	except Exception:
		return None


def _readable_file_path(file_url):
	"""Disk path of an uploaded file, found ONLY through its File record (exact file_url) and only
	if the caller may read that File. QA S-04/P6-02: this used to accept any path -- cwd-relative,
	basename-only (public, private, or os.walk over the whole site folder) -- so any logged-in user
	could have any file on the server parsed and its contents returned. Anything else (an external
	http URL, an unknown path) resolves to nothing, i.e. an empty parse, as before."""
	if not file_url or str(file_url).startswith("http"):
		return None
	name = frappe.db.get_value("File", {"file_url": file_url}, "name")
	if not name:
		return None
	file_doc = frappe.get_doc("File", name)
	if not file_doc.has_permission("read"):
		frappe.throw("Not permitted to read this file.", frappe.PermissionError)
	path = file_doc.get_full_path()
	return path if os.path.exists(path) else None


def _search(pattern, text, flags=re.IGNORECASE):
	"""Best effort single regex search helper."""
	try:
		match = re.search(pattern, text, flags)
		return clean_extracted_value(match.group(1)) if match else None
	except Exception:
		return None


# ─────────────────────────────────────────────────────────────────────────────
# 5. Corridor-Specific Extraction Logic
# ─────────────────────────────────────────────────────────────────────────────
def extract_saudi_fields(text):
	"""Extracts Saudi Musaned contract fields."""
	if not text:
		return {}

	structurizer = ContractTextStructurizer(text)
	sec = structurizer.sections
	emp_text = sec["employer"] or text
	rec_text = sec["recruiting_agency"] or text

	data = {
		"contract_number": extract_field_from_text(
			text,
			[
				r'(?:CONTRACT\s*(?:#|NO\.?|NUMBER|ID|No:|#\s*)|رقم\s*عقد\s*خدمات\s*التوسط|رقم\s*عقد\s*التوسط|رقم\s*العقد|رقم\s*الاتفاقية|agreement\s*(?:no|number))\s*[:=\-–#]?\s*([A-Za-z0-9][A-Za-z0-9\-_/]{3,25})',
				r'CONTRACT\s*#\s*(\d+)',
			]
		),
		"visa_number": extract_field_from_text(
			text,
			[
				r'VISA\s*(?:NUMBER|NO\.?|ID)?\s*#?\s*[:=\-–]?\s*([0-9]{8,15})',
				r'(?:رقم\s*التأشيرة|رقم\s*التاشيرة|رقم\s*تأشيرة\s*العمل|رقم\s*صادر\s*التأشيرة|رقم\s*الصادر)\s*[:=\-–#]?\s*([0-9]{8,15})',
			]
		),
		"employer_name": extract_field_from_text(
			emp_text,
			[
				r'(?:A\.\s*Employer:.*?Name|Name|الاسم|اسم\s*صاحب\s*العمل|اسم\s*الكفيل)\s*[:=\-–]?\s*([A-Za-z\u0600-\u06FF\s.]+)',
				r'(?:employer\s*name|sponsor\s*name|first\s*party\s*name)\s*[:=\-–]?\s*([^\r\n]+)',
			]
		),
		"employer_national_id": extract_field_from_text(
			emp_text,
			[
				r'(?:National\s*ID\s*Number|رقم\s*الهوية\s*الوطنية|الهوية\s*الوطنية|السجل\s*المدني)\s*[:=\-–]?\s*([0-9]{9,15})',
				r'National ID Number:\s*(\d+)',
			]
		),
		"employer_address": extract_field_from_text(
			emp_text,
			[
				r'(?:Street|الشارع|الحي\s*/\s*الشارع|العنوان)\s*[:=\-–]?\s*([^\r\n]+)',
			]
		),
		"saudi_agency_name": extract_field_from_text(
			rec_text,
			[
				r'(?:Saudi Recruiting Agency:.*?Name|Name|الاسم|اسم\s*مكتب\s*الاستقدام|اسم\s*الشركة)\s*[:=\-–]?\s*([^\r\n]+)',
				r'(?:recruiting\s*agency\s*name|recruitment\s*office\s*name)\s*[:=\-–]?\s*([^\r\n]+)',
			]
		),
		"saudi_agency_license": extract_field_from_text(
			rec_text,
			[
				r'(?:License\s*no|رقم\s*الترخيص|ترخيص\s*رقم)\s*[:=\-–]?\s*([A-Za-z0-9\-_/]{2,25})',
				r'License no:\s*(\d+)',
			]
		),
	}

	# Salary
	salary_match = re.search(r'fixed\s*monthly\s*wage\s*of\s*([0-9,.]+)\s*\(([^)]+)\)', text, re.IGNORECASE)
	if not salary_match:
		salary_match = re.search(r'أجر\s*شهري\s*ثابت\s*قدره\s*([0-9,.]+)\s*\(([^)]+)\)', text, re.IGNORECASE)

	if salary_match:
		try:
			data["contract_salary_amount"] = decimal.Decimal(salary_match.group(1).replace(",", ""))
			curr = salary_match.group(2).strip().upper()
			data["contract_salary_currency"] = "SAR" if "RIYAL" in curr or "SAR" in curr or "ريال" in curr else curr
		except Exception:
			pass
	else:
		sal_val = _search(r"(?:monthly\s*salary|basic\s*salary|الراتب\s*الشهري)\s*[:=\-–]?\s*(\d+)", text)
		if sal_val:
			try:
				data["contract_salary_amount"] = decimal.Decimal(sal_val)
				data["contract_salary_currency"] = "SAR"
			except Exception:
				pass

	return {k: v for k, v in data.items() if v is not None}


def extract_kuwait_fields(text):
	"""Extracts Kuwait employment contract fields."""
	if not text:
		return {}

	data = {
		"employer_name": _search(r"Employer\s*Name\s*:?\s*([A-Z][A-Za-z\s]+?)\s*\n", text) or extract_field_from_text(
			text, [r'(?:Employer\s*Name|اسم\s*صاحب\s*العمل)\s*[:=\-–]?\s*([^\r\n]+)']
		),
		"employment_site": _search(r"Employment\s*site\s*:?\s*([A-Za-z\s]+?)\s*\n", text) or extract_field_from_text(
			text, [r'(?:Employment\s*site|مكان\s*العمل|موقع\s*العمل)\s*[:=\-–]?\s*([^\r\n]+)']
		),
		"contract_duration": _search(r"Duration\s*of\s*the\s*contract\s*:?\s*\*?([A-Za-z0-9\s]+?)\+?\s*starting", text) or extract_field_from_text(
			text, [r'(?:Duration\s*of\s*the\s*contract|مدة\s*العقد)\s*[:=\-–]?\s*([^\r\n]+)']
		),
		"contract_salary_amount": None,
		"contract_salary_currency": "KWD",
	}

	sal_amt = _search(r"Monthly\s*salary\s*:?\s*(\d+)", text) or _search(r"(?:الراتب\s*الشهري|الأجر)\s*[:=\-–]?\s*(\d+)", text)
	if sal_amt:
		try:
			data["contract_salary_amount"] = decimal.Decimal(sal_amt)
		except Exception:
			pass

	curr_match = _search(r"Monthly\s*salary\s*:?\s*\d+\s*([A-Z]{2,3})", text)
	if curr_match:
		data["contract_salary_currency"] = curr_match

	return {k: v for k, v in data.items() if v is not None}


def extract_visa_fields(text):
	"""Extracts Kuwait eVisa document fields from both visual text and embedded MRZ lines."""
	if not text:
		return {}

	# 1. Visa Number
	visa_num = _search(r"Visa\s*Number\s*[:=\s\n]*[^\d\n]*(\d{7,12})", text)
	if not visa_num:
		# Check MRZ line 2: e.g. 2864328973ETH...
		m_mrz = re.search(r'\n(\d{8,10})\d[A-Z]{3}', text)
		if m_mrz:
			visa_num = m_mrz.group(1)

	# 2. Visa Type
	visa_type = None
	if "Domestic Worker" in text or "عامل منزلي" in text or "عامل منزلى" in text:
		visa_type = "Domestic Worker Visa"
	elif "Commercial" in text or "تجارية" in text:
		visa_type = "Commercial Visa"
	elif "Work" in text or "عمل" in text:
		visa_type = "Work Visa"
	else:
		visa_type = _search(r"Visa\s*Type\s*[:=\-–]?\s*([A-Za-z\s\-]+?)\s*\n", text)

	# 3. Dates
	issue_date = normalize_date_string(
		_search(r"Issue\s*Date[^\n]*\n(?:[^\n]*\n)?([0-9]{4}-[0-9]{2}-[0-9]{2}|[0-9]{1,2}[-/.][0-9]{1,2}[-/.][0-9]{2,4})", text)
		or _search(r"Issue\s*Date\s*[:=\-–]?\s*([0-9\-/.]+)", text)
	)
	expiry_date = normalize_date_string(
		_search(r"Expiry\s*Date[^\n]*\n(?:[^\n]*\n)?([0-9]{4}-[0-9]{2}-[0-9]{2}|[0-9]{1,2}[-/.][0-9]{1,2}[-/.][0-9]{2,4})", text)
		or _search(r"Expiry\s*Date\s*[:=\-–]?\s*([0-9\-/.]+)", text)
	)

	# 4. Reference Number
	ref_num = (
		_search(r"Reference\s*[:=\-–#]?\s*(\d{7,12})", text)
		or _search(r"Reference[^\n]*\n[^\d\n]*(\d{7,12})", text)
		or _search(r"رقم\s*المرجع[^\d\n]*(\d{7,12})", text)
	)

	# 5. Sponsor Name & Civil ID
	sponsor_name = None
	sponsor_civil_id = None
	# Pattern: "فهد حامد حسين المشيعيب105709811 - الكويت-"
	m_spons = re.search(r'([^\n\d]+?)\s*(\d{8,12})\s*-\s*(?:الكويت|Kuwait|ﺍﻟﻜﻮﻳﺖ)', text)
	if m_spons:
		sponsor_name = clean_extracted_value(m_spons.group(1).strip())
		sponsor_civil_id = m_spons.group(2).strip()
	else:
		sponsor_name = _search(r"([A-Za-z\u0600-\u06FF\s]+?)\s*-\s*\d+\s*-\s*(?:Kuwait|الكويت)", text)
		sponsor_civil_id = _search(r"[A-Za-z\u0600-\u06FF\s]+?\s*-\s*(\d+)\s*-\s*(?:Kuwait|الكويت)", text)

	# 6. Kuwait Agency Name & License
	kuwait_agency_name = None
	kuwait_agency_license = None
	# Pattern: "مكتب مكاتي الريف لاستقدام العماله المنز475246 -"
	m_agency = re.search(r'([^\n\d]+?)\s*(\d{4,8})\s*-', text)
	if m_agency:
		cand_name = m_agency.group(1).strip()
		if any(kw in cand_name for kw in ["مكتب", "شركة", "استقدام", "Agency", "Recruitment", "Office", "الريف"]):
			kuwait_agency_name = clean_extracted_value(cand_name)
			kuwait_agency_license = m_agency.group(2).strip()

	if not kuwait_agency_name:
		kuwait_agency_name = _search(r"([A-Za-z\u0600-\u06FF\s]+?)\s*-\s*\d+\s*\n", text)
		kuwait_agency_license = _search(r"[A-Za-z\u0600-\u06FF\s]+?\s*-\s*(\d+)\s*\n", text)

	data = {
		"visa_number": visa_num,
		"visa_type": visa_type,
		"visa_issue_date": issue_date,
		"visa_expiry_date": expiry_date,
		"visa_reference_number": ref_num,
		"sponsor_name": sponsor_name,
		"sponsor_civil_id": sponsor_civil_id,
		"kuwait_agency_name": kuwait_agency_name,
		"kuwait_agency_license": kuwait_agency_license,
	}

	return {k: v for k, v in data.items() if v is not None}


# ─────────────────────────────────────────────────────────────────────────────
# 6. Master Parse Handlers
# ─────────────────────────────────────────────────────────────────────────────
def parse_structured_contract_text(full_text_or_blocks):
	"""
	Parses full contract text into structured dictionary matching the complete
	Saudi Musaned / Employment contract specification.
	"""
	structurizer = ContractTextStructurizer(full_text_or_blocks)
	text = structurizer.unified_text

	saudi = extract_saudi_fields(text)
	kuwait = extract_kuwait_fields(text)

	result = {
		"contract_signed_date": extract_contract_signed_date(text),
		"contract_number": saudi.get("contract_number"),
		"visa_number": saudi.get("visa_number"),
		"employer_name": saudi.get("employer_name") or kuwait.get("employer_name"),
		"employer_national_id": saudi.get("employer_national_id"),
		"employer_address": saudi.get("employer_address"),
		"saudi_agency_name": saudi.get("saudi_agency_name"),
		"saudi_agency_license": saudi.get("saudi_agency_license"),
		"employment_site": kuwait.get("employment_site"),
		"contract_duration": kuwait.get("contract_duration") or "2 Years",
		"contract_salary_amount": saudi.get("contract_salary_amount") or kuwait.get("contract_salary_amount"),
		"contract_salary_currency": saudi.get("contract_salary_currency") or kuwait.get("contract_salary_currency"),
	}
	return {k: v for k, v in result.items() if v is not None}


# Who may call the parsers directly: staff, and agencies (they preview their own uploaded
# contract before upload_contract). What they can parse is limited by _readable_file_path.
PARSER_ROLES = INTERNAL_STAFF_ROLES | {"Foreign Agency"}


def _require_parser_role():
	if frappe.session.user != "Administrator" and not (PARSER_ROLES & set(frappe.get_roles())):
		frappe.throw("Not permitted.", frappe.PermissionError)


@frappe.whitelist()
def parse_contract_file(file_url=None, destination_country=None, **kwargs):
	"""
	Given a Frappe file_url for an uploaded contract, returns a dict of Placement field updates.
	Extracts contract_signed_date, and country-specific structured fields.
	"""
	_require_parser_role()
	return _parse_contract(file_url, destination_country)


def _parse_contract(file_url, destination_country=None):
	"""parse_contract_file without the role gate, for upload_contract / create_muayena_placement /
	the background job, which have their own gates. The file-read check still applies."""
	if not file_url:
		frappe.throw("file_url is required.", frappe.ValidationError)
	file_path = _readable_file_path(file_url)
	text = extract_text_from_pdf(file_path) if file_path else ""

	result = {"contract_signed_date": extract_contract_signed_date(text)}
	if destination_country == "Saudi Arabia":
		result.update(extract_saudi_fields(text))
		result["visa_expiry_date"] = result.get("visa_expiry_date") or extract_visa_fields(text).get("visa_expiry_date")
	elif destination_country == "Kuwait":
		result.update(extract_kuwait_fields(text))
	else:
		# Auto-detect or run full parser
		result.update(parse_structured_contract_text(text))

	return {k: v for k, v in result.items() if v is not None}


@frappe.whitelist()
def parse_visa_file(file_url=None, **kwargs):
	"""Kuwait visa document parser for placement_api.upload_visa."""
	_require_parser_role()
	return _parse_visa(file_url)


def _parse_visa(file_url):
	"""parse_visa_file without the role gate (see _parse_contract); the file-read check applies."""
	if not file_url:
		frappe.throw("file_url is required.", frappe.ValidationError)
	file_path = _readable_file_path(file_url)
	text = extract_text_from_pdf(file_path) if file_path else ""
	return {k: v for k, v in extract_visa_fields(text).items() if v is not None}


@frappe.whitelist()
def enqueue_parse_contract_file(file_url=None, destination_country=None, **kwargs):
	"""Async twin of parse_contract_file -- returns a Background Job reference immediately
	instead of blocking on the parse. Poll background_jobs.get_job_status(job) for the result.
	Gated to internal staff; the job runs as the requester, so the file-read check still applies."""
	if not file_url:
		frappe.throw("file_url is required.", frappe.ValidationError)
	require_internal_staff()

	from agency_tracking.background_jobs import enqueue_job

	file_doc = frappe.db.get_value("File", {"file_url": file_url}, "name")
	job = enqueue_job(
		"Parse Contract",
		reference_doctype="File" if file_doc else None,
		reference_name=file_doc,
		file_url=file_url,
		destination_country=destination_country,
	)
	return {"job": job, "status": "Queued"}


@frappe.whitelist()
def enqueue_parse_visa_file(file_url=None, **kwargs):
	"""Async twin of parse_visa_file -- returns a Background Job reference immediately instead
	of blocking on the parse. Poll background_jobs.get_job_status(job) for the result. Gated to
	internal staff; the job runs as the requester, so the file-read check still applies."""
	if not file_url:
		frappe.throw("file_url is required.", frappe.ValidationError)
	require_internal_staff()

	from agency_tracking.background_jobs import enqueue_job

	file_doc = frappe.db.get_value("File", {"file_url": file_url}, "name")
	job = enqueue_job(
		"Parse Visa",
		reference_doctype="File" if file_doc else None,
		reference_name=file_doc,
		file_url=file_url,
	)
	return {"job": job, "status": "Queued"}
