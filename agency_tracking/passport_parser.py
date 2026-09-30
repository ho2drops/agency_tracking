# Copyright (c) 2026, Agency and contributors
# License: MIT. See LICENSE

import os
import re
import io
import datetime
import unicodedata

import requests

import frappe
from frappe.utils import getdate

try:
	from passporteye import read_mrz
except ImportError:
	read_mrz = None

try:
	import pycountry
except ImportError:
	pycountry = None

# ─────────────────────────────────────────────────────────────────────────────
# 1. ISO 3166-1 Alpha-3 Country Mapping & Constants
# ─────────────────────────────────────────────────────────────────────────────
ISO_ALPHA3_TO_COUNTRY = {
	"ETH": "Ethiopia",
	"SAU": "Saudi Arabia",
	"ARE": "United Arab Emirates",
	"KWT": "Kuwait",
	"QAT": "Qatar",
	"BHR": "Bahrain",
	"OMN": "Oman",
	"JOR": "Jordan",
	"LBN": "Lebanon",
	"KEN": "Kenya",
	"UGA": "Uganda",
	"SDN": "Sudan",
	"SSD": "South Sudan",
	"SOM": "Somalia",
	"DJI": "Djibouti",
	"EGY": "Egypt",
	"ERI": "Eritrea",
	"IND": "India",
	"PAK": "Pakistan",
	"BGD": "Bangladesh",
	"PHL": "Philippines",
	"IDN": "Indonesia",
	"NPL": "Nepal",
	"LKA": "Sri Lanka",
	"GBR": "United Kingdom",
	"USA": "United States",
	"CAN": "Canada",
	"AUS": "Australia",
	"DEU": "Germany",
	"FRA": "France",
	"ITA": "Italy",
	"ESP": "Spain",
	"TUR": "Turkey",
	"CHN": "China",
	"JPN": "Japan",
	"YEM": "Yemen",
	"IRQ": "Iraq",
	"SYR": "Syrian Arab Republic",
}

MONTH_MAP = {
	"JAN": 1, "FEB": 2, "MAR": 3, "APR": 4, "MAY": 5, "JUN": 6,
	"JUL": 7, "AUG": 8, "SEP": 9, "OCT": 10, "NOV": 11, "DEC": 12
}

MRZ_SEX_TO_GENDER = {"M": "Male", "F": "Female"}

# A real single given/family name is essentially always shorter than this. Longer is a strong
# signal that an MRZ '<' filler between two separate names was OCR'd as a stray letter and the
# names got glued into one run-on token -- there's no checksum on the name field to catch this
# any other way. Used both to flag a suspicious result for review (map_mrz_fields) and to prefer
# a better-split candidate when several crops all otherwise read the MRZ correctly
# (_name_plausibility_score).
MAX_PLAUSIBLE_NAME_TOKEN_LENGTH = 14

# ─────────────────────────────────────────────────────────────────────────────
# 2. ICAO 9303 Checksum Decoder & Self-Correction Engine
# ─────────────────────────────────────────────────────────────────────────────
ICAO_WEIGHTS = [7, 3, 1]

CHAR_CONFUSIONS = {
	"O": ["0", "Q", "D", "U"],
	"0": ["O", "Q", "D", "U"],
	"I": ["1", "l", "|", "T", "J"],
	"1": ["I", "l", "|", "T", "J"],
	"S": ["5", "8", "$"],
	"5": ["S", "6"],
	"B": ["8", "6", "0", "E"],
	"8": ["B", "0", "3", "S"],
	"Z": ["2", "7"],
	"2": ["Z"],
	"G": ["6", "0", "C", "Q"],
	"6": ["G", "b", "5"],
	"D": ["0", "O", "Q"],
	"Q": ["0", "O", "G"],
	"U": ["V", "0"],
	"V": ["U", "<"],
	"K": ["<", "X"],
	"C": ["<", "G", "0"],
	"<": ["K", "C", "X", "(", " ", "_", "-"],
}


def icao_char_value(c):
	"""Returns integer value for ICAO 9303 checksum computation."""
	c = str(c).upper()
	if c.isdigit():
		return int(c)
	if 'A' <= c <= 'Z':
		return ord(c) - ord('A') + 10
	return 0


def compute_icao_checksum(text):
	"""Computes ICAO 9303 checksum digit for a given alphanumeric string."""
	total = 0
	for idx, char in enumerate(text):
		weight = ICAO_WEIGHTS[idx % 3]
		total += icao_char_value(char) * weight
	return total % 10


def verify_and_correct_checksum(data_str, expected_check_char, is_numeric=True):
	"""
	Validates data_str against expected_check_char.
	Uses OCR confusion map to find single character substitutions.
	"""
	data_clean = str(data_str).upper()
	check_char = str(expected_check_char).upper()

	if check_char in ("O", "D", "Q"):
		check_char = "0"
	elif check_char in ("I", "L", "|"):
		check_char = "1"
	elif check_char == "S":
		check_char = "5"
	elif check_char == "B":
		check_char = "8"
	elif check_char == "Z":
		check_char = "2"

	if not check_char.isdigit():
		return False, data_clean, check_char

	expected_check_val = int(check_char)
	computed = compute_icao_checksum(data_clean)

	if computed == expected_check_val and (not is_numeric or data_clean.isdigit()):
		return True, data_clean, str(expected_check_val)

	# Single-character substitution trial
	data_list = list(data_clean)
	positions = list(range(len(data_list)))
	if is_numeric:
		# Prioritize positions that currently contain non-digits
		positions.sort(key=lambda idx: 0 if not data_list[idx].isdigit() else 1)

	for pos in positions:
		ch = data_list[pos]
		confusions = CHAR_CONFUSIONS.get(ch, [])
		for alt in confusions:
			if is_numeric and not alt.isdigit():
				continue
			trial_list = list(data_list)
			trial_list[pos] = alt
			trial_str = "".join(trial_list)
			if is_numeric and not trial_str.isdigit():
				continue
			if compute_icao_checksum(trial_str) == expected_check_val:
				return True, trial_str, str(expected_check_val)

	return False, data_clean, check_char


# ─────────────────────────────────────────────────────────────────────────────
# 3. Clean and Parse MRZ Lines
# ─────────────────────────────────────────────────────────────────────────────
def clean_mrz_line(raw_line):
	"""Cleans noisy characters from an OCR'd MRZ line."""
	if not raw_line:
		return ""
	line = raw_line.strip().upper()
	line = line.replace("«", "<").replace("‹", "<").replace("(", "<").replace(")", "<")
	line = line.replace("{", "<").replace("}", "<").replace("[", "<").replace("]", "<")
	line = line.replace("_", "<").replace("-", "<").replace(" ", "")
	line = re.sub(r'[^A-Z0-9<]', '', line)
	return line


def parse_mrz_date(yymmdd_str, is_expiry=False):
	"""Converts YYMMDD string to YYYY-MM-DD."""
	if not yymmdd_str or len(yymmdd_str) < 6:
		return None
	try:
		yy = int(yymmdd_str[0:2])
		mm = int(yymmdd_str[2:4])
		dd = int(yymmdd_str[4:6])

		if mm < 1 or mm > 12 or dd < 1 or dd > 31:
			return None

		curr_year = datetime.datetime.now().year
		curr_yy = curr_year % 100

		if is_expiry:
			century = 2000 if yy <= curr_yy + 30 else 1900
		else:
			century = 1900 if yy > curr_yy else 2000

		full_year = century + yy
		return f"{full_year:04d}-{mm:02d}-{dd:02d}"
	except Exception:
		return None


def infer_passport_issue_date(passport_expiry_str):
	"""Infer passport issue date from expiry for a 5-year passport.

	Ethiopian (and ICAO-standard) passports are valid for 5 years, but the expiry printed is the
	*last valid day*, which is one day before the 5th anniversary of issue -- i.e.
	expiry = issue + 5 years - 1 day. So to recover the issue date we invert that exactly:
	issue = expiry - 5 years + 1 day. Example: expiry 2029-07-10 -> issue 2024-07-11 (matches the
	real passport, whereas a plain -5-years gave 2024-07-10, the "1-day variation" bug).

	This is still a derivation, not a read of the passport's printed issue date (the MRZ does not
	carry an issue date at all -- ICAO 9303 encodes only DOB and expiry). It is exact for the
	standard 5-years-minus-a-day passports; keep the field editable for manual correction.
	"""
	if not passport_expiry_str:
		return None
	try:
		from dateutil.relativedelta import relativedelta
		exp_date = getdate(passport_expiry_str)
		return str(exp_date - relativedelta(years=5) + relativedelta(days=1))
	except Exception:
		return None


def _looks_like_name_token(token):
	"""A real human name, transliterated to Latin script, always has at least one vowel and
	isn't just a couple of letters repeated over and over -- both true regardless of length, and
	both reliably broken by misread MRZ '<' padding that happened to OCR as letters instead of
	literal '<'. Confirmed live: a low-resolution scan turned the trailing filler after a
	genuinely correct name into strings like "CCCCCLLCLLLCLCLLCLLKL" -- no vowels, only three
	distinct letters -- short enough to dodge a pure length check but still obvious noise once
	looked at this way. The diversity check is ratio-based, not a fixed minimum count, so it
	doesn't reject a genuinely short, repetitive real name like "Anna" (2 distinct letters in 4,
	a healthy 0.5 ratio) while still catching a long low-diversity run (0.1-0.15 in the examples
	actually seen)."""
	if not token:
		return False
	upper = token.upper()
	if not any(c in "AEIOU" for c in upper):
		return False
	if len(set(upper)) / len(upper) < 0.25:
		return False
	if _has_repeated_run(upper):
		return False
	return True


def _has_repeated_run(upper_token, min_run=3):
	"""True if the same character repeats `min_run`-or-more times in a row. A real human name,
	transliterated to Latin script, essentially never does this -- confirmed live on an ICAO
	specimen document: a misread MRZ filler run OCR'd as "...LKKKKLCCLELCE", which has a vowel
	and clears the diversity-ratio check above (4 distinct letters across 13), so it slipped past
	that gate entirely and still needed a targeted catch."""
	run = 1
	for i in range(1, len(upper_token)):
		run = run + 1 if upper_token[i] == upper_token[i - 1] else 1
		if run >= min_run:
			return True
	return False


# Digits that a name token should never contain -- real given/surname text has none, so any
# digit found inside one is always an OCR misread of its similar-looking letter, never genuine
# data. Confirmed live on two separate ICAO specimen reads: MRZ "MEI" -> "ME1" and "OLIVIA" ->
# "0LIVIA", both slipping past the composite checksum gate (names carry no checksum of their own)
# and past the old length-only plausibility check, since neither is unusually long. Applying this
# BEFORE the vowel/diversity filter above recovers the real name instead of just discarding the
# whole token as unrecoverable garbage. Deliberately limited to the confusions CHAR_CONFUSIONS
# already treats as common OCR misreads of these exact letters (not every digit -- 3/4/7/9 have
# no correspondingly common letter confusion and guessing wrong would do more harm than leaving
# them, which _looks_like_name_token's own checks will then correctly reject as garbage anyway).
_DIGIT_TO_LETTER_CONFUSION = {"0": "O", "1": "I", "2": "Z", "5": "S", "6": "G", "8": "B"}


def _fix_digit_letter_confusion(token):
	if not token or not any(c.isdigit() for c in token):
		return token
	return "".join(_DIGIT_TO_LETTER_CONFUSION.get(c, c) for c in token)


def _strip_shared_leading_noise(tokens):
	"""If 2+ given-name tokens ALL start with the exact same character, and stripping it from
	every one of them still leaves real-looking names, strip it -- that character is standing in
	for a misread delimiter, not a genuine shared initial.

	Confirmed live on an ICAO specimen document: "<<ANNA<MARIA<<<..." OCR'd as
	"<K<KANNA<KMARIA<...", turning both given names into "Kanna"/"Kmaria". A single name
	starting with a letter like K/C/X is completely ordinary (plenty of real names do), so this
	deliberately does NOT touch a lone token, or when tokens disagree on their leading
	character -- only when EVERY token in the field shares the identical anomaly, which is a
	shared-corruption-source signal a coincidence can't easily produce, is it safe to strip."""
	if len(tokens) < 2:
		return tokens
	leading = {t[0] for t in tokens if t}
	if len(leading) != 1:
		return tokens
	stripped = [t[1:] for t in tokens]
	if all(_looks_like_name_token(t) for t in stripped):
		return stripped
	return tokens


def split_name_parts(surname, given_names):
	"""Split a passport name into (first, middle, last) using Ethiopian / ICAO ordering.

	Ethiopian names have no family surname: the sequence is own-name, father's name, grandfather's
	name -- and the passport puts the own name(s) in the "given names" field and the ancestral
	name(s) in the "surname" field. So the true ordered full name is given_names followed by
	surname. We rebuild that whole sequence and then take first = token[0], last = token[-1],
	middle = everything in between. This stops the middle (father's) name being mistaken for the
	last name when the surname field carries more than one token, and never drops the middle name.

	Tokens that don't look name-shaped (see _looks_like_name_token) are dropped before any of
	this positional assignment happens -- filtering garbage out up front, rather than assembling
	it into first/middle/last and only flagging the damage afterward, so a spurious extra token
	(misread trailing padding) can't shift which real token ends up as the surname.

	Returns (first, middle, last) with middle/last possibly None. Casing is left to the caller.
	"""
	given_tokens = [t for t in re.split(r"\s+", (given_names or "").replace("<", " ").strip()) if t]
	surname_tokens = [t for t in re.split(r"\s+", (surname or "").replace("<", " ").strip()) if t]
	# Recover an OCR digit-for-letter misread (MEI -> ME1, OLIVIA -> 0LIVIA) before the vowel/
	# diversity filter below, so a single wrong character doesn't cost the whole token.
	given_tokens = [_fix_digit_letter_confusion(t) for t in given_tokens]
	surname_tokens = [_fix_digit_letter_confusion(t) for t in surname_tokens]
	# Garbage padding-turned-letters (no vowel) must be dropped BEFORE checking for a shared
	# leading character below -- otherwise a real "KANNA"/"KMARIA" pair sharing a spurious 'K'
	# never gets caught because a third, unrelated garbage token in the same list ("LLLL...",
	# itself already rejected by _looks_like_name_token on its own merits) doesn't share it,
	# breaking the "every token agrees" requirement that makes stripping safe.
	given_tokens = [t for t in given_tokens if _looks_like_name_token(t)]
	surname_tokens = [t for t in surname_tokens if _looks_like_name_token(t)]
	given_tokens = _strip_shared_leading_noise(given_tokens)
	seq = given_tokens + surname_tokens
	if not seq:
		return "", None, None
	first = seq[0]
	last = seq[-1] if len(seq) >= 2 else None
	middle = " ".join(seq[1:-1]) if len(seq) > 2 else None
	return first, middle, last


# Characters CHAR_CONFUSIONS already lists as common OCR misreads of '<' -- reused here (see
# _split_surname_given) to recognize a "<<" delimiter even when one of its two characters landed
# as a stray letter instead, rather than needing a fresh confusion table for the same glyph.
_DOUBLE_DELIM_CONFUSABLES = set(CHAR_CONFUSIONS["<"])


def _split_surname_given(name_field):
	"""Splits an MRZ name field into (surname, given_names) on the double-'<' delimiter,
	tolerating ONE of the two '<' characters being misread as a common confusable.

	Confirmed live, two distinct failures this was built to catch:
	1. "<<" OCR'd as "C<" ("BEYENEC<BEGONET..." instead of "BEYENE<<BEGONET...") -- no literal
	   "<<" at the real boundary at all, so a plain str.split("<<") silently collapsed the whole
	   field into one bucket with given_names empty, scrambling first/middle/last even though
	   every individual name token had actually been read correctly.
	2. Naively using `str.find("<<")` for a tolerant search STILL picked the wrong spot on that
	   same real case: the trailing '<' padding after the field's real content (always present,
	   to fill the line to 44 characters) contains a genuine literal "<<" run of its own, further
	   right in the string -- `find` returned that first if the real boundary earlier on was only
	   a confusable-tolerant match, not an exact one, since it was never given the chance to
	   compare positions. Scanning left-to-right and taking whichever kind of match -- exact or
	   tolerant -- comes FIRST positionally fixes both: the real boundary is always well before
	   the padding tail, so the earliest match is always the right one.

	Falls back to no split at all (given_names empty) only when nothing plausible is found."""
	for i in range(len(name_field) - 1):
		a, b = name_field[i], name_field[i + 1]
		if a == "<" and (b == "<" or b in _DOUBLE_DELIM_CONFUSABLES):
			return name_field[:i], name_field[i + 2 :]
		if b == "<" and a in _DOUBLE_DELIM_CONFUSABLES:
			return name_field[:i], name_field[i + 2 :]
	return name_field, ""


def _locate_country_code(line1_prefix):
	"""Finds a known ISO alpha-3 country code within the first several characters of an MRZ
	line, tolerating OCR inserting or dropping a character before it in the document-code area.

	Confirmed live: an inserted stray letter before the country code ("PAQETH..." instead of
	"PQETH...") shifted the fixed-position name field (line1[5:44]) one character early, prefixing
	the surname with the country code's own trailing letter ("Hwachamo" instead of "Wachamo") --
	even though the surname/given-names themselves were read correctly. The document-number/DOB/
	expiry checksums on line 2 can't catch this at all, since it's a line-1-only, pre-name-field
	problem. Scanning outward from the canonical position (2) for a real country code recovers
	the true field boundary instead of trusting a position that OCR may have shifted.

	Returns (code, end_index) for the first match found, or (None, None) if nothing recognizable
	turns up in this short window (falls back to the fixed position 2:5)."""
	for offset in (0, -1, 1, -2, 2):
		start = 2 + offset
		if start < 0:
			continue
		candidate = line1_prefix[start : start + 3]
		if candidate in ISO_ALPHA3_TO_COUNTRY:
			return candidate, start + 3
	return None, None


def parse_mrz_td3(line1, line2):
	"""
	Parses standard Type 3 (TD3) Passport MRZ (2 lines x 44 characters).
	Example:
	Line 1: PQETHWACHAMO<<ASNEKECH<TEDESSE<<<<<<<<<<<<<<<<
	Line 2: EQ25760963ETH0012027F30051210<<<<<<<<<<<<<<04
	"""
	result = {
		"format": "TD3",
		"doc_type": "Passport",
		"raw_line1": line1,
		"raw_line2": line2,
		"is_valid": True,
		"checksum_validation": {},
	}

	line1 = (line1 + "<" * 44)[:44]
	line2 = (line2 + "<" * 44)[:44]

	# --- Line 1 Breakdown ---
	doc_code = line1[0:2].replace("<", "")
	name_field_start = 5
	located_code, located_end = _locate_country_code(line1[:10])
	if located_code:
		issuing_country_code = located_code
		name_field_start = located_end
	else:
		issuing_country_code = line1[2:5].replace("<", "")
	name_field = line1[name_field_start:44]

	surname_raw, given_raw = _split_surname_given(name_field)
	surname = surname_raw.replace("<", " ").strip()
	given_names = given_raw.replace("<", " ").strip()

	# Ordered split across BOTH fields (given names + surname) so the middle name is never lost
	# nor merged into the last name -- see split_name_parts.
	first_name, middle_name, last_name = split_name_parts(surname, given_names)

	# --- Line 2 Breakdown ---
	raw_doc_num = line2[0:9]
	raw_doc_check = line2[9]
	nationality_code = line2[10:13].replace("<", "")
	raw_dob = line2[13:19]
	raw_dob_check = line2[19]
	sex_char = line2[20].upper()
	raw_expiry = line2[21:27]
	raw_expiry_check = line2[27]
	raw_optional = line2[28:42]

	val_doc, corr_doc_num, corr_doc_check = verify_and_correct_checksum(raw_doc_num, raw_doc_check, is_numeric=False)
	clean_passport_num = corr_doc_num.replace("<", "").strip()
	result["checksum_validation"]["passport_number"] = {
		"valid": val_doc, "raw": raw_doc_num, "clean": clean_passport_num, "check": corr_doc_check
	}

	val_dob, corr_dob, corr_dob_check = verify_and_correct_checksum(raw_dob, raw_dob_check, is_numeric=True)
	result["checksum_validation"]["date_of_birth"] = {
		"valid": val_dob, "raw": raw_dob, "corrected": corr_dob, "check": corr_dob_check
	}

	val_exp, corr_exp, corr_exp_check = verify_and_correct_checksum(raw_expiry, raw_expiry_check, is_numeric=True)
	result["checksum_validation"]["expiry_date"] = {
		"valid": val_exp, "raw": raw_expiry, "corrected": corr_exp, "check": corr_exp_check
	}

	# No silent placeholders (audit G-002): emit None for anything the scan didn't actually yield --
	# never "Applicant"/"Ethiopia"/"Female". map_mrz_fields skips None, so blanks stay blank for a
	# human to fill rather than being seeded with fabricated data.
	result["passport_number"] = clean_passport_num or None
	result["first_name"] = first_name.upper() if first_name else None
	result["middle_name"] = middle_name.upper() if middle_name else None
	result["last_name"] = last_name.upper() if last_name else None

	parts = [result["first_name"], result["middle_name"], result["last_name"]]
	result["full_name"] = " ".join([p for p in parts if p]).strip() or None

	result["nationality"] = _resolve_country_name(nationality_code) or ISO_ALPHA3_TO_COUNTRY.get(nationality_code)
	result["place_of_issue"] = _resolve_country_name(issuing_country_code) or ISO_ALPHA3_TO_COUNTRY.get(issuing_country_code)

	result["date_of_birth"] = parse_mrz_date(corr_dob, is_expiry=False)
	result["passport_expiry"] = parse_mrz_date(corr_exp, is_expiry=True)
	result["passport_expiry_date"] = result["passport_expiry"]
	result["passport_issue_date"] = infer_passport_issue_date(result["passport_expiry"])

	result["gender"] = MRZ_SEX_TO_GENDER.get(sex_char)

	clean_opt = raw_optional.replace("<", "").strip()
	if clean_opt:
		result["national_id"] = clean_opt

	return result


def parse_mrz_td1(line1, line2, line3):
	"""Parses Type 1 (TD1) ID / Travel Card MRZ (3 lines x 30 characters)."""
	line1 = (line1 + "<" * 30)[:30]
	line2 = (line2 + "<" * 30)[:30]
	line3 = (line3 + "<" * 30)[:30]

	issuing_country_code = line1[2:5].replace("<", "")
	raw_doc_num = line1[5:14]
	raw_doc_check = line1[14]

	raw_dob = line2[0:6]
	raw_dob_check = line2[6]
	sex_char = line2[7].upper()
	raw_expiry = line2[8:14]
	raw_expiry_check = line2[14]
	nationality_code = line2[15:18].replace("<", "")

	name_parts = line3.split("<<")
	surname = name_parts[0].replace("<", " ").strip()
	given_names = name_parts[1].replace("<", " ").strip() if len(name_parts) > 1 else ""
	first_name, middle_name, last_name = split_name_parts(surname, given_names)

	val_doc, corr_doc_num, _ = verify_and_correct_checksum(raw_doc_num, raw_doc_check, is_numeric=False)
	val_dob, corr_dob, _ = verify_and_correct_checksum(raw_dob, raw_dob_check)
	val_exp, corr_exp, _ = verify_and_correct_checksum(raw_expiry, raw_expiry_check)

	exp_date = parse_mrz_date(corr_exp, is_expiry=True)

	# No silent placeholders (audit G-002) -- None for anything not actually read.
	return {
		"format": "TD1",
		"doc_type": "Identity Card",
		"checksum_validation": {
			"passport_number": {"valid": val_doc},
			"date_of_birth": {"valid": val_dob},
			"expiry_date": {"valid": val_exp},
		},
		"passport_number": corr_doc_num.replace("<", "").strip() or None,
		"first_name": first_name.upper() if first_name else None,
		"middle_name": middle_name.upper() if middle_name else None,
		"last_name": last_name.upper() if last_name else None,
		"full_name": " ".join(filter(None, [first_name, middle_name, last_name])).upper() or None,
		"nationality": _resolve_country_name(nationality_code) or ISO_ALPHA3_TO_COUNTRY.get(nationality_code),
		"place_of_issue": _resolve_country_name(issuing_country_code) or ISO_ALPHA3_TO_COUNTRY.get(issuing_country_code),
		"date_of_birth": parse_mrz_date(corr_dob, is_expiry=False),
		"passport_expiry": exp_date,
		"passport_expiry_date": exp_date,
		"passport_issue_date": infer_passport_issue_date(exp_date),
		"gender": MRZ_SEX_TO_GENDER.get(sex_char),
	}


def extract_mrz_from_raw_text(raw_text):
	"""Searches OCR text streams for MRZ lines or fallback visual passport data."""
	if not raw_text:
		return None

	raw_lines = [l.strip() for l in raw_text.splitlines() if l.strip()]
	lines = [clean_mrz_line(l) for l in raw_lines]
	lines = [l for l in lines if len(l) >= 20]

	# Printed issue date and place of birth off the same text we already have (MRZ carries neither)
	# — applied to whichever MRZ result we return below.
	printed_issue = find_printed_issue_date(raw_text)
	printed_pob = find_printed_place_of_birth(raw_text)

	# 1. Look for TD3 lines (starts with P, PQ, PA, PB, etc. or contains <<)
	for i in range(len(lines)):
		l1 = lines[i]
		# "<" in l1 required on the startswith("P")/ETH branches too -- confirmed live: a cloud
		# OCR API's raw text interleaves visual page text (labels, printed field values) with the
		# actual MRZ, unlike the Tesseract fallback below which only ever sees a pre-cropped MRZ
		# band. A printed header like "PASPOORT KONINKRUK DER NEDERLANDEN" starts with "P" and
		# clears the length floor after whitespace-stripping, but a real MRZ line1 always carries
		# "<" filler (a name essentially never fills the full 39-character field exactly) -- this
		# was matching the header as line1 and the REAL line1 as line2, scrambling the whole read
		# even though the correct MRZ was sitting right there in the same text.
		is_l1_mrz = (
			(l1.startswith("P") and len(l1) >= 28 and "<" in l1) or
			("<<" in l1 and len(l1) >= 28) or
			("ETH" in l1[:8] and len(l1) >= 28 and "<" in l1)
		)
		if is_l1_mrz and (i + 1 < len(lines)):
			l2 = lines[i + 1]
			if len(l2) >= 28:
				return _apply_printed_fields(parse_mrz_td3(l1, l2), printed_issue, printed_pob)

	# 2. Look for any adjacent lines with << or passport numbers
	for i in range(len(lines) - 1):
		l1 = lines[i]
		l2 = lines[i + 1]
		if (len(l1) >= 30 and len(l2) >= 30) and ("<" in l1 or "<" in l2):
			return _apply_printed_fields(parse_mrz_td3(l1, l2), printed_issue, printed_pob)

	# 3. Look for TD1 (3 lines)
	for i in range(len(lines) - 2):
		l1, l2, l3 = lines[i], lines[i + 1], lines[i + 2]
		if 25 <= len(l1) <= 35 and 25 <= len(l2) <= 35 and 25 <= len(l3) <= 35:
			return _apply_printed_fields(parse_mrz_td1(l1, l2, l3), printed_issue, printed_pob)

	return extract_visual_passport_data(raw_text)


def _parse_visual_date(date_str):
	"""Parses visual passport dates like '02 DEC 00' or '13 MAY 25' or '12 MAY 2030'."""
	if not date_str:
		return None
	m = re.search(r'([0-9]{1,2})\s*([A-Za-z]{3})\s*([0-9]{2,4})', date_str)
	if m:
		dd = int(m.group(1))
		mon_str = m.group(2).upper()
		yy_str = m.group(3)
		mm = MONTH_MAP.get(mon_str, 1)
		if len(yy_str) == 2:
			yy = int(yy_str)
			curr_yy = datetime.datetime.now().year % 100
			century = 2000 if yy <= curr_yy + 30 else 1900
			full_year = century + yy
		else:
			full_year = int(yy_str)
		return f"{full_year:04d}-{mm:02d}-{dd:02d}"
	return normalize_date_string(date_str)


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
			return str(datetime.date(year, month, day))
	except Exception:
		pass
	try:
		return str(getdate(d))
	except Exception:
		return None


def find_printed_issue_date(raw_text):
	"""Read the printed 'Date of Issue' from the passport's visual zone, off text we ALREADY
	extracted (PDF text stream or the OCR the MRZ step already ran) -- no extra OCR pass. The MRZ
	itself carries no issue date (ICAO 9303), so when the passport page prints one this is the
	accurate source; callers fall back to the expiry-based derivation only when it's absent.
	Returns ISO YYYY-MM-DD or None."""
	if not raw_text:
		return None
	date_token = r"([0-9]{1,2}\s*[A-Za-z]{3}\s*[0-9]{2,4}|[0-9]{1,4}[-/.][0-9]{1,2}[-/.][0-9]{1,4})"
	# "Date of issue"/"Issue date" — but never "Date of expiry", which is matched by excluding a
	# following "exp"/"expiry" between the label and the date.
	pattern = re.compile(r"(?:Date\s*of\s*Issue|Issue\s*Date)\s*[:=]?\s*" + date_token, re.I)
	for line in raw_text.splitlines():
		if re.search(r"expir", line, re.I) and not re.search(r"issue", line, re.I):
			continue
		m = pattern.search(line)
		if m:
			iso = _parse_visual_date(m.group(1))
			if iso:
				return iso
	return None


def find_printed_place_of_birth(raw_text):
	"""Read the printed 'Place of Birth' from the passport's visual zone, off text we ALREADY
	extracted (PDF text stream or the OCR the MRZ step already ran) -- no extra OCR pass. Like the
	issue date, the MRZ itself has no place-of-birth field at all (ICAO 9303), so this visual-zone
	label is the only source there is, independent of whether the MRZ read validated. Returns the
	value upper-cased and trimmed, or None."""
	if not raw_text:
		return None
	lines = [l.strip() for l in raw_text.splitlines() if l.strip()]
	# 2026-09-19 fix: live-tested against two real Ethiopian passports, this label came back
	# OCR-mangled a different way on each one -- "Place uf Birth" (of -> uf) on one, "Place of
	# Binh" (Birth -> Binh, rt collapsed to n) on the other. The old exact-match "Place of Birth"
	# missed BOTH. `[A-Za-z]{0,3}` tolerates a garbled "of"; `B\w{2,4}h` tolerates a garbled
	# "Birth" (matches Birth/Binh/Brith/etc -- any B...h word of plausible length) without
	# matching unrelated labels: "Date of Birth" ("Dale of Binh" in the same OCR pass) is still
	# excluded because it has no "Place" immediately before the B...h token.
	pattern = re.compile(r"(?:Place\s*[A-Za-z]{0,3}\s*B\w{2,4}h|Birth\s*Place|POB)\s*[:=]?\s*(.*)", re.I)
	for i, line in enumerate(lines):
		m = pattern.search(line)
		if not m:
			continue
		val = m.group(1).strip()
		# A label sitting right next to another printed field on the same OCR'd line (e.g. "Place
		# of Birth ADDIS ABABA Sex F") shouldn't swallow that next field as part of the value.
		val = re.sub(r"\s*(?:Sex|Gender|Nationality|Date\s*of\s*(?:Issue|Birth|Expiry)|Authority)\b.*$", "", val, flags=re.I).strip()
		if val and not re.search(r"Passport|Country|Date", val, re.I):
			return val.upper()
		# Label with no usable value on the same line -- the value is often printed on the next
		# line instead (same pattern passport-number extraction already relies on).
		if not val and i + 1 < len(lines):
			nxt = lines[i + 1].strip()
			if nxt and not re.search(r"Passport|Country|Sex|Gender|Date", nxt, re.I):
				return nxt.upper()
	return None


def _apply_printed_fields(result, printed_issue, printed_pob=None):
	"""Adds visual-zone-only fields the MRZ itself can't carry (ICAO 9303 has neither an issue
	date nor a place-of-birth field) onto an MRZ parser result -- overriding the issue date's own
	derived guess, and filling place_of_birth which MRZ parsing never sets at all."""
	if not result:
		return result
	if printed_issue:
		result["passport_issue_date"] = printed_issue
	if printed_pob:
		result["place_of_birth"] = printed_pob
	return result


def extract_visual_passport_data(raw_text):
	"""Fallback visual label-based passport field extractor."""
	if not raw_text:
		return None

	lines = [l.strip() for l in raw_text.splitlines() if l.strip()]
	printed_issue = find_printed_issue_date(raw_text)
	printed_pob = find_printed_place_of_birth(raw_text)
	# Visual (non-MRZ) extraction is the low-confidence last resort -- no fabricated defaults
	# (audit G-002) and the whole result is flagged needs_review in map_mrz_fields.
	data = {
		"format": "Visual",
		"doc_type": "Passport",
		"passport_number": None,
		"first_name": None,
		"middle_name": None,
		"last_name": None,
		"full_name": None,
		"nationality": None,
		"place_of_issue": None,
		"place_of_birth": printed_pob,
		"date_of_birth": None,
		"passport_issue_date": None,
		"passport_expiry": None,
		"passport_expiry_date": None,
		"gender": None,
	}

	for i, line in enumerate(lines):
		# Passport number: e.g. Passport No: EQ2576096 or EP1234567
		if re.search(r'(?:Passport\s*No|Passport\s*Number|Doc\s*No)', line, re.I):
			m = re.search(r'\b([A-Z]{1,2}[0-9]{6,9})\b', line)
			if m:
				data["passport_number"] = m.group(1)
			elif i + 1 < len(lines):
				m2 = re.search(r'\b([A-Z]{1,2}[0-9]{6,9})\b', lines[i + 1])
				if m2:
					data["passport_number"] = m2.group(1)

		# Given Names
		if re.search(r'(?:Given\s*Names?|First\s*Name)', line, re.I):
			val = re.sub(r'^(?:Given\s*Names?|First\s*Name)[:=\s]+', '', line, flags=re.I).strip()
			if val and not re.search(r'Passport|Country|Sex|Date', val, re.I):
				parts = val.split()
				if parts:
					data["first_name"] = parts[0].upper()
					if len(parts) > 1:
						data["middle_name"] = " ".join(parts[1:]).upper()

		# Surname
		if re.search(r'(?:Surname|Last\s*Name)', line, re.I):
			val = re.sub(r'^(?:Surname|Last\s*Name)[:=\s]+', '', line, flags=re.I).strip()
			if val and not re.search(r'Passport|Country|Sex|Date', val, re.I):
				data["last_name"] = val.upper()

		# Date of birth
		if re.search(r'(?:Date\s*of\s*birth|DOB|Birth\s*Date)', line, re.I):
			val = re.sub(r'^(?:Date\s*of\s*birth|DOB|Birth\s*Date)[:=\s]+', '', line, flags=re.I).strip()
			parsed_d = _parse_visual_date(val)
			if parsed_d:
				data["date_of_birth"] = parsed_d

		# Expiry date
		if re.search(r'(?:Date\s*of\s*expiry|Expiry\s*Date|Expiration)', line, re.I):
			val = re.sub(r'^(?:Date\s*of\s*expiry|Expiry\s*Date|Expiration)[:=\s]+', '', line, flags=re.I).strip()
			parsed_e = _parse_visual_date(val)
			if parsed_e:
				data["passport_expiry"] = parsed_e
				data["passport_expiry_date"] = parsed_e
				# Printed issue date wins; derive only when the page didn't print one.
				data["passport_issue_date"] = printed_issue or infer_passport_issue_date(parsed_e)

		# Sex / Gender
		if re.search(r'\b(?:Sex|Gender)\b', line, re.I):
			if re.search(r'\b(?:M|Male)\b', line, re.I):
				data["gender"] = "Male"
			elif re.search(r'\b(?:F|Female)\b', line, re.I):
				data["gender"] = "Female"

	# Printed issue date even when no expiry line was found on the page.
	if printed_issue and not data.get("passport_issue_date"):
		data["passport_issue_date"] = printed_issue

	# Re-split across given + surname so a multi-token surname doesn't swallow the middle name.
	if data.get("first_name") or data.get("last_name"):
		given = " ".join(filter(None, [data.get("first_name"), data.get("middle_name")]))
		first, middle, last = split_name_parts(data.get("last_name"), given)
		data["first_name"] = first.upper() if first else data.get("first_name")
		data["middle_name"] = middle.upper() if middle else None
		data["last_name"] = last.upper() if last else None

	if data.get("passport_number") or (data.get("first_name") and data.get("date_of_birth")):
		return data
	return None


# ─────────────────────────────────────────────────────────────────────────────
# 4. Helper Resolution & Pure Mapping
# ─────────────────────────────────────────────────────────────────────────────
def _resolve_country_name(alpha_3: str) -> str | None:
	"""ISO alpha-3 (MRZ) -> Frappe's own Country doctype name."""
	if not alpha_3:
		return None
	clean = alpha_3.strip().upper()
	if clean in ISO_ALPHA3_TO_COUNTRY:
		candidate = ISO_ALPHA3_TO_COUNTRY[clean]
		try:
			if getattr(frappe, "db", None) and frappe.db and frappe.db.exists("Country", candidate):
				return candidate
		except Exception:
			pass
		return candidate

	if pycountry:
		try:
			country = pycountry.countries.get(alpha_3=clean)
			if country:
				try:
					if getattr(frappe, "db", None) and frappe.db:
						name = frappe.db.get_value("Country", {"code": country.alpha_2.lower()}, "name")
						if name:
							return name
				except Exception:
					pass
				return country.name
		except Exception:
			pass

	try:
		if getattr(frappe, "db", None) and frappe.db:
			return frappe.db.get_value("Country", {"name": ["like", f"{clean}%"]}, "name")
	except Exception:
		pass

	return None


def _mrz_date_to_iso(mrz_date: str) -> str | None:
	"""MRZ dates are YYMMDD."""
	return parse_mrz_date(mrz_date, is_expiry=False)


def _normalize_mrz_date(value, is_expiry=False) -> str | None:
	"""Coerce any date shape a parser might hand us into ISO YYYY-MM-DD.

	Handles: already-ISO strings (pass through), raw 6-digit MRZ YYMMDD (PassportEye), and the
	usual DD/MM/YYYY-style visual dates. is_expiry drives the century window for 2-digit years
	(expiry rolls forward, birth rolls back)."""
	if not value:
		return None
	s = str(value).strip()
	if re.fullmatch(r"\d{4}-\d{2}-\d{2}", s):
		return s
	if re.fullmatch(r"\d{6}", s):
		return parse_mrz_date(s, is_expiry=is_expiry)
	return normalize_date_string(s)


def map_mrz_fields(mrz_dict: dict) -> dict:
	"""Pure mapping from MRZ dictionary to Applicant fieldnames.

	Audit G-001/G-002: a field whose ICAO checksum could NOT be validated/self-corrected is dropped
	(not auto-filled) and the whole result is flagged `needs_passport_review` so staff verify it by
	hand -- rather than writing an unverified passport number/DOB/expiry as if it were confirmed.
	Visual (non-MRZ) extractions are always flagged for review. Fields the scan didn't yield are
	simply absent (no fabricated Ethiopia/Female/"Applicant" defaults)."""
	fields = {}
	cv = mrz_dict.get("checksum_validation") or {}

	def _ok(key):
		entry = cv.get(key)
		# Unknown (e.g. a raw PassportEye dict with no checksum info) -> don't drop; only a proven
		# False suppresses the field.
		return entry.get("valid", True) if isinstance(entry, dict) else True

	needs_review = mrz_dict.get("format") == "Visual"

	doc_num = (mrz_dict.get("number") or mrz_dict.get("passport_number") or "").strip()
	if doc_num and _ok("passport_number"):
		fields["passport_number"] = doc_num
	elif doc_num:
		needs_review = True

	# Expiry: our own parsers already give ISO; PassportEye gives raw YYMMDD under "expiration_date"
	# (must use is_expiry=True so 29 -> 2029, not 1929).
	exp_date = (
		mrz_dict.get("passport_expiry_date")
		or mrz_dict.get("passport_expiry")
		or _normalize_mrz_date(mrz_dict.get("expiration_date"), is_expiry=True)
	)
	if exp_date and _ok("expiry_date"):
		fields["passport_expiry_date"] = exp_date
		# Prefer a printed/carried-through issue date; derive from expiry only when none was found.
		issue_date = _normalize_mrz_date(mrz_dict.get("passport_issue_date")) or infer_passport_issue_date(exp_date)
		if issue_date:
			fields["passport_issue_date"] = issue_date
	elif exp_date:
		needs_review = True

	# DOB: always normalise to ISO before returning it, so the endpoint JSON never carries a raw
	# YYMMDD (which the frontend can't use and which reads as "no DOB").
	dob = _normalize_mrz_date(mrz_dict.get("date_of_birth"), is_expiry=False)
	if dob and _ok("date_of_birth"):
		fields["date_of_birth"] = dob
	elif dob:
		needs_review = True

	sex = (mrz_dict.get("gender") or mrz_dict.get("sex") or "").strip().upper()
	if sex in ("M", "MALE"):
		fields["gender"] = "Male"
	elif sex in ("F", "FEMALE"):
		fields["gender"] = "Female"

	# Names carry NO checksum at all in the MRZ standard (ICAO 9303 only checksums the document
	# number, DOB, expiry, and an optional personal-number field) -- so if the OCR locked onto the
	# wrong region of the image (a real risk on a full, uncropped passport photo: a security
	# pattern, the photo border, other printed text can all look like a plausible text band), a
	# garbled "name" has nothing checking it and sails straight through. The composite check
	# below is the only real defense: only trust a name when EVERY field that *does* have a
	# checksum agrees. `cv` empty (e.g. a raw, unvalidated dict with no checksum_validation at
	# all) is treated as untrusted, not "nothing to check" -- see _ok()'s own docstring for why an
	# unknown entry there defaults True, which is correct for individual fields but wrong here.
	composite_ok = bool(cv) and _ok("passport_number") and _ok("date_of_birth") and _ok("expiry_date")
	if not composite_ok:
		needs_review = True

	first = mrz_dict.get("first_name")
	middle = mrz_dict.get("middle_name")
	last = mrz_dict.get("last_name")
	if not (first or last):
		first, middle, last = split_name_parts(mrz_dict.get("surname"), mrz_dict.get("names"))
	if composite_ok:
		# The given-names field separates individual names with a single '<' filler -- OCR
		# misreading that one character as a stray letter (confirmed live: a '<' read as 'S')
		# glues two names into one run-on token with no delimiter left to split on. Nothing
		# checksums the name field, so this can't be caught the way a digit field would be; a
		# token well beyond any real single name's length is the only signal available, and it's
		# still surfaced (not blanked) since a visibly-too-long guess is faster for staff to fix
		# than an empty field, but it's flagged so it doesn't look like a clean, confirmed read.
		if any(len(n) > MAX_PLAUSIBLE_NAME_TOKEN_LENGTH for n in (first, middle, last) if n):
			needs_review = True
		if first:
			fields["first_name"] = str(first).upper()
		if middle:
			fields["middle_name"] = str(middle).upper()
		if last:
			fields["last_name"] = str(last).upper()

	nat = mrz_dict.get("nationality")
	if not nat:
		nat_alpha3 = (mrz_dict.get("nationality_code") or mrz_dict.get("country") or "").strip().upper()
		nat = _resolve_country_name(nat_alpha3) or ISO_ALPHA3_TO_COUNTRY.get(nat_alpha3)
	if nat:
		fields["nationality"] = nat

	issue_place = mrz_dict.get("place_of_issue")
	if issue_place:
		fields["passport_issue_place"] = issue_place

	pob = mrz_dict.get("place_of_birth")
	if pob:
		fields["place_of_birth"] = pob

	if needs_review:
		fields["needs_passport_review"] = 1

	return fields


# ─────────────────────────────────────────────────────────────────────────────
# 5. OCR.space -- the primary OCR engine
# ─────────────────────────────────────────────────────────────────────────────
# 2026-09-16: replaced PaddleOCR (tried first 2026-09-16, briefly) with this cloud call.
# PaddleOCR's own text detection was genuinely more capable in some cases than the Tesseract
# pipeline below (see removed section history), but PaddlePaddle's runtime carries a ~660-700MB
# process baseline just to be loaded at all -- confirmed unworkable on Railway's free/hobby tier
# alongside gunicorn+worker+scheduler+redis already sharing the one container. A side-by-side
# test against 22 public ICAO specimens plus 2 real passports found OCR.space and the self-hosted
# Tesseract pipeline fail on genuinely DIFFERENT images (neither one is a strict upgrade) -- so
# this is kept as the FIRST strategy tried, with the entire Tesseract/PassportEye pipeline below
# kept intact as a fallback for whatever OCR.space misses or when no API key is configured, not a
# replacement for it.
#
# Needs `ocrspace_api_key` set in site_config.json (bench set-config ocrspace_api_key <key>, or
# via the OCRSPACE_API_KEY env var on Railway -- see docker/entrypoint.sh). Free tier is 25,000
# requests/month, rate-limited to 500/day per IP; comfortably covers one call per onboarded
# worker at this app's actual volume.
_OCRSPACE_ENDPOINT = "https://api.ocr.space/parse/image"
_OCRSPACE_MAX_UPLOAD_BYTES = 1_000_000  # free-tier file size cap


def _shrink_for_ocrspace_upload(pil_img, max_bytes=_OCRSPACE_MAX_UPLOAD_BYTES):
	"""Re-encodes as JPEG under OCR.space's free-tier file size cap. Quality reduction first --
	MRZ text is bold, high-contrast, monospace, and survives fairly aggressive JPEG compression
	still legible -- dimension downscaling only as a last resort since that hits small printed
	text harder than compression does."""
	from PIL import Image

	img = pil_img.convert("RGB")
	for quality in (85, 70, 55, 40, 30):
		buf = io.BytesIO()
		img.save(buf, format="JPEG", quality=quality)
		if buf.tell() <= max_bytes:
			return buf.getvalue()
	w, h = img.size
	img = img.resize((int(w * 0.6), int(h * 0.6)), Image.Resampling.LANCZOS)
	buf = io.BytesIO()
	img.save(buf, format="JPEG", quality=60)
	return buf.getvalue()


def _ocrspace_full_text(file_path):
	"""Sends the full (orientation-corrected) image to OCR.space and returns its raw recognized
	text, the same shape extract_mrz_from_raw_text already expects and already knows how to
	search for MRZ-shaped lines within (it doesn't need to be handed only the MRZ; other
	recognized page text is just harmless noise it already searches past).

	Deliberately NOT cropped to a bottom MRZ band before sending -- confirmed live: cropping
	first (copying the pattern the removed PaddleOCR call used) measurably hurt accuracy here,
	regressing several images that read correctly uncropped. OCR.space's own text detection
	evidently wants the surrounding page context that a tight crop throws away; unlike the
	Tesseract fallback below, this engine was never validated against a cropped input, only a
	full one, in the side-by-side test that justified adding it.

	`file_path` itself is only ever read, never modified, so the original upload (needed in full
	for the CV/photo record) is completely unaffected by this. Returns None if no API key is
	configured, the request fails, or OCR.space itself reports a processing error -- the caller
	falls back to the rest of the pipeline either way."""
	api_key = frappe.conf.get("ocrspace_api_key")
	if not api_key:
		return None
	try:
		img = _load_upright_image(file_path)
		payload = _shrink_for_ocrspace_upload(img)

		resp = requests.post(
			_OCRSPACE_ENDPOINT,
			files={"file": ("passport.jpg", payload, "image/jpeg")},
			data={"apikey": api_key, "OCREngine": 2, "language": "eng", "scale": "true", "isOverlayRequired": "false"},
			timeout=30,
		)
		if resp.status_code != 200:
			return None
		data = resp.json()
		if data.get("IsErroredOnProcessing"):
			return None
		results = data.get("ParsedResults") or []
		return results[0].get("ParsedText") if results else None
	except Exception:
		frappe.log_error(title="OCR.space passport read failed", message=frappe.get_traceback())
		return None


# ─────────────────────────────────────────────────────────────────────────────
# 6. Robust image normalization + candidate MRZ localization (Tesseract fallback)
# ─────────────────────────────────────────────────────────────────────────────
# Why this exists: PassportEye locates the MRZ by scanning the whole image for a "long, thin,
# high-contrast horizontal text band" (a contour heuristic). On a clean, pre-cropped MRZ strip
# that's trivially the only thing in frame. On a real, full, uncropped passport photo (angled,
# background in frame, glare, the biometric photo/hologram/other printed text also being
# high-contrast) it can lock onto the WRONG region entirely -- and unlike the document-number/
# DOB/expiry fields, an MRZ "name" carries no checksum at all in the ICAO 9303 standard, so
# garbage OCR'd off the wrong region used to sail straight into first_name/last_name with nothing
# to catch it. The functions below give the parser other, more reliable ways to find the real MRZ
# band even without a pre-crop, and map_mrz_fields' own composite check (above) refuses to trust
# a name at all unless every field that DOES have a checksum agrees.


def _mrz_score(parsed):
	"""How many of the three checksummed MRZ fields (passport number, DOB, expiry) validated --
	3 means fully trustworthy (composite_ok in map_mrz_fields), lower scores are still useful as
	a "best guess so far" ranking across multiple OCR attempts on different crops/strategies."""
	if not parsed:
		return -1
	cv = parsed.get("checksum_validation") or {}

	def ok(key):
		entry = cv.get(key)
		return bool(isinstance(entry, dict) and entry.get("valid"))

	return sum([ok("passport_number"), ok("date_of_birth"), ok("expiry_date")])


# checksum_validation stores the corrected value under "clean" for passport_number but
# "corrected" for the two date fields -- an inherited inconsistency from parse_mrz_td3/parse_mrz_td1,
# not something to unify here (out of scope for a comparison helper).
_CHECKSUM_VALUE_KEYS = {"passport_number": "clean", "date_of_birth": "corrected", "expiry_date": "corrected"}


def _checksummed_values_conflict(a, b):
	"""True if two independently fully-checksum-valid MRZ reads disagree on any of the three
	checksummed field VALUES (not just whether each individually passed). Both having passed
	only means each satisfies its own checksum equation -- verify_and_correct_checksum's single-
	character substitution is a guess that happens to balance the equation, not a guarantee it
	recovered the real digit, so two attempts CAN both "validate" while reporting different
	values for the same real-world field. See _consider's own comment for how this is used."""
	cv_a = (a or {}).get("checksum_validation") or {}
	cv_b = (b or {}).get("checksum_validation") or {}
	for field, value_key in _CHECKSUM_VALUE_KEYS.items():
		entry_a, entry_b = cv_a.get(field), cv_b.get(field)
		if not (isinstance(entry_a, dict) and isinstance(entry_b, dict)):
			continue
		val_a, val_b = entry_a.get(value_key), entry_b.get(value_key)
		if val_a and val_b and val_a != val_b:
			return True
	return False


# Confirmed live on a real, genuinely upright passport scan: Tesseract's OSD suggested a 180-
# degree rotation at confidence 0.40-1.52 (and misidentified the script as Bengali at a similarly
# low confidence) -- a passport bio page is mostly decorative script, security-pattern watermarks,
# and a small dense MRZ block, exactly the kind of page OSD has little real signal to work with.
# Applying its guess unconditionally flipped a correct image upside down, which sent every
# bottom-band MRZ crop looking at the wrong half of the page entirely. This floor is set well
# above the observed noise range; a genuine sideways/upside-down photo reports much higher.
MIN_OSD_ROTATION_CONFIDENCE = 3.0


def _load_upright_image(file_path):
	"""Corrects orientation two independent ways, since a wrong-way-up image defeats MRZ box
	detection just as badly as a wrong crop does: (1) EXIF says "rotate on display" but the raw
	pixels are stored sideways -- extremely common straight off a phone camera, and silently
	ignored by naive loaders (skimage's `imread` among them, which is what PassportEye uses
	internally) -- corrected via PIL's own EXIF-aware transpose; (2) no usable EXIF at all but the
	photo is genuinely sideways/upside-down (a scan, or EXIF stripped by an upload pipeline) --
	caught by Tesseract's own orientation/script-detection (OSD) pass instead, only trusted above
	MIN_OSD_ROTATION_CONFIDENCE (see its own comment for why a low-confidence guess is dangerous
	here specifically)."""
	from PIL import Image, ImageOps

	img = Image.open(file_path)
	img = ImageOps.exif_transpose(img)

	try:
		import pytesseract

		osd = pytesseract.image_to_osd(img, output_type=pytesseract.Output.DICT)
		rotate_by = osd.get("rotate") or 0
		if rotate_by and osd.get("orientation_conf", 0) >= MIN_OSD_ROTATION_CONFIDENCE:
			img = img.rotate(-rotate_by, expand=True)
	except Exception:
		pass  # OSD needs a reasonable amount of real text on the page -- fine to skip, not fatal

	return img.convert("RGB")


def _preprocess_variants(pil_img):
	"""Yields (label, processed image) pairs -- more than one preprocessing recipe tried per
	crop, since a weaker/older OCR engine build (confirmed live: production's Tesseract 5.3.0
	read this pipeline's own passport-number/expiry fields correctly but missed the DOB digits
	that a newer local build read fine -- packaged OCR engine/model versions genuinely vary
	across environments and this can't be pinned to one exact combination) can respond
	differently to each: plain grayscale+autocontrast is gentler and preserves anti-aliased glyph
	edges an LSTM model often reads well, while a hard black/white threshold strips out low-
	contrast background bleed-through (a printed guilloche/security pattern sitting directly
	under the MRZ text, as on a real passport bio page) that can otherwise confuse a less capable
	engine. Upscaled well above Tesseract's effective minimum glyph height either way -- a tight
	crop off a modest-resolution source can otherwise end up too small to read reliably."""
	from PIL import Image, ImageOps

	gray = pil_img.convert("L")
	gray = ImageOps.autocontrast(gray, cutoff=1)
	target_width = 1600
	if gray.width < target_width:
		scale = target_width / gray.width
		gray = gray.resize((int(gray.width * scale), int(gray.height * scale)), Image.Resampling.LANCZOS)
	yield "gray", gray

	binarized = gray.point(lambda p: 255 if p > 140 else 0)
	yield "binarized", binarized


def _candidate_ocr_texts(pil_img):
	"""Every (preprocessing variant x page-segmentation mode) combination for one crop, most-
	likely-useful first. PSM 6 (uniform block of text) suits a crop with both MRZ lines still
	together; PSM 7 (single text line) can do better on a crop tight enough to isolate just one
	line. More attempts than any single one needs, but OCR here only runs once per passport
	upload (with an async job-queue alternative already available for exactly this reason), not
	a request-latency-sensitive hot path -- so trading a handful of extra, cheap Tesseract calls
	for a real shot at reading a borderline character correctly is a good trade."""
	import pytesseract

	for variant_label, processed in _preprocess_variants(pil_img):
		for psm in (6, 7):
			config = f"--psm {psm} -c tessedit_char_whitelist=ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789<"
			yield f"{variant_label}/psm{psm}", pytesseract.image_to_string(processed, config=config)


def _candidate_mrz_crops(pil_img):
	"""Yields (label, PIL image) candidates most likely to contain ONLY the MRZ band, most-likely
	framing first. ICAO 9303 fixes the MRZ at the very bottom of a passport's bio-data page, so
	"the bottom slice of whatever was uploaded" is a strong, dependency-free prior regardless of
	how the rest of the page is framed -- unlike PassportEye's contour-based locator, this can't
	be fooled by a high-contrast pattern elsewhere in the photo, because it never looks anywhere
	else. Multiple ratios cover both a tight photo of just the bio page and a wider one that
	includes surrounding table/background -- and, incidentally, each crop gets a different
	effective upscale factor in _preprocess_for_ocr (narrower crop -> more upscaling), so they can
	genuinely OCR the same physical line differently, which is exploited below to recover a
	cleaner name split even after one crop already reads the checksummed fields correctly."""
	w, h = pil_img.size
	yield "full", pil_img
	for ratio in (0.15, 0.20, 0.25, 0.30, 0.35):
		top = int(h * (1 - ratio))
		yield f"bottom-{int(ratio * 100)}pct", pil_img.crop((0, top, w, h))


def _name_plausibility_score(parsed):
	"""1 if the given/surname split looks like real, separate names; 0 if any token is
	suspiciously long (see MAX_PLAUSIBLE_NAME_TOKEN_LENGTH) or fails the same shape check
	(_looks_like_name_token) split_name_parts already uses to filter garbage tokens up front.
	That second check matters here too: a short-or-medium-length token can still be pure OCR
	noise ("Lkkkklcclelce" is 13 characters, under the length cap, but no real name repeats a
	character 4 times running) -- confirmed live, this previously scored 1 and stopped the search
	before a cleaner crop/attempt ever got a chance. Checksums can't tell two equally "fully
	valid" MRZ reads apart when they disagree only on how the name field was split -- this is the
	tiebreaker used to prefer whichever crop/attempt happened to OCR the '<' filler between given
	names cleanly."""
	if not parsed:
		return -1
	names = [n for n in (parsed.get("first_name"), parsed.get("middle_name"), parsed.get("last_name")) if n]
	if any(len(n) > MAX_PLAUSIBLE_NAME_TOKEN_LENGTH for n in names):
		return 0
	if any(not _looks_like_name_token(n) for n in names):
		return 0
	return 1


# ─────────────────────────────────────────────────────────────────────────────
# 7. Master Passport MRZ File Parser
# ─────────────────────────────────────────────────────────────────────────────
def parse_passport_mrz(file_path: str) -> dict:
	"""
	Given a filesystem path to a passport scan/photo/PDF, extracts MRZ and returns
	Applicant field updates. Tries strategies in order of accuracy/cost:
	1. Text stream extraction (PyMuPDF / pypdf) -- a real text layer needs no OCR guessing at all.
	2. OCR.space on the full (orientation-corrected) image -- the primary OCR engine (see its
	   own section comment for why it isn't pre-cropped).
	3. PassportEye's own MRZ box locator, when it finds one.
	4. Our own Tesseract candidate-crop pipeline (orientation-corrected full image, then several
	   MRZ-band crops) -- fallback of last resort, only reached when steps 2/3 didn't already
	   produce a clean, confident hit.
	Stops as soon as a result is BOTH fully checksum-valid (passport number, DOB, expiry all
	agree) AND has a plausible name split; a fully-valid-but-implausible-name result keeps trying
	further candidates first, since a name typo can't be caught by checksums the way a digit
	field can -- only a differently-OCR'd attempt of the same line can fix it. Two independently
	checksum-valid reads that disagree on the actual digits (each strategy runs its own single-
	character checksum-correction guess, so two attempts CAN both "validate" while reading
	different values) force needs_passport_review rather than silently trusting whichever ran
	last -- deliberately NOT extended to comparing name values the same way, since that produced
	false positives on real, correctly-read photos once step 4's dozen-or-so attempts are in play
	(see _consider's own comment).
	If nothing validates fully, returns the best partial match found (map_mrz_fields flags it
	needs_passport_review and withholds the name fields, which have no checksum of their own).
	Never raises exceptions — gracefully logs and returns empty dict on failure.
	"""
	if not file_path or not os.path.exists(file_path):
		return {}

	best_result, best_score = None, -1  # best PARTIAL match (score < 3), for the final fallback
	best_valid, best_valid_names = None, -1  # best FULLY checksum-valid match, by name plausibility
	saw_conflict = False  # two independently "valid" attempts disagreed on a checksummed value

	def _consider(parsed):
		"""Records `parsed` as a candidate. Returns True once it's good enough (fully checksum-
		valid AND a plausible name split) that trying further candidates isn't worth it."""
		nonlocal best_result, best_score, best_valid, best_valid_names, saw_conflict
		if not parsed:
			return False
		score = _mrz_score(parsed)
		if score == 3:
			names_score = _name_plausibility_score(parsed)
			if best_valid is not None and _checksummed_values_conflict(best_valid, parsed):
				# Both attempts satisfy the ICAO checksum, but disagree on the actual digits --
				# verify_and_correct_checksum's single-character substitution only guarantees the
				# checksum equation balances, not that the guessed correction is the real one, so
				# two attempts CAN both pass while reading different underlying values. A nicer-
				# looking name on the later attempt isn't good evidence its digits are the correct
				# ones -- flagging beats silently trusting whichever attempt happened to run last.
				#
				# Deliberately NOT extended to compare NAME values the same way (tried live,
				# reverted): once step 4 below runs unconditionally, a real, correctly-read photo
				# can still turn up a spurious-but-checksum-valid misread on SOME crop out of the
				# ~12 tried (more attempts = more chances for one fluke to validate), which then
				# "conflicts" with the correct answer and forces an unwarranted review flag on a
				# passport that was actually read correctly the first time. A digit conflict is
				# rare and meaningful; a name-shape disagreement across a dozen brute-force OCR
				# attempts is common enough to be noise, not signal.
				saw_conflict = True
			if names_score > best_valid_names:
				best_valid, best_valid_names = parsed, names_score
			return names_score == 1
		if score > best_score:
			best_result, best_score = parsed, score
		return False

	def _finalize_valid():
		"""Maps best_valid to Applicant fields, forcing needs_passport_review when a conflicting
		checksum-valid reading was seen anywhere in the search -- see _consider's own comment."""
		result = map_mrz_fields(best_valid)
		if saw_conflict:
			result["needs_passport_review"] = 1
		return result

	# 1. If PDF document, extract text stream directly.
	if file_path.lower().endswith(".pdf"):
		try:
			from agency_tracking.contract_parser import extract_text_from_pdf

			raw_text = extract_text_from_pdf(file_path)
			if raw_text and _consider(extract_mrz_from_raw_text(raw_text)):
				return _finalize_valid()
		except Exception:
			pass

	# 2. OCR.space on the full image -- see its section comment above. Deliberately does NOT
	# short-circuit here (see its own section comment). Tried making this ALWAYS fall through to
	# the Tesseract corroboration in step 4 even after a clean hit here (reverted, 2026-09-16):
	# across step 4's ~12 crop/preprocessing/PSM combinations, a real, correctly-photographed
	# passport can still turn up an occasional spurious-but-checksum-valid misread on SOME
	# attempt out of that many -- confirmed live, this forced an unwarranted needs_passport_review
	# on a real Ethiopian passport photo that OCR.space had already read perfectly. A short-
	# circuit on a clean single-shot success stays the right default; see _checksummed_values_conflict
	# for the narrower, still-active safety net that catches disagreement in the cases where step 4
	# genuinely does still run (steps 2/3 didn't get a clean hit on their own).
	ocrspace_text = _ocrspace_full_text(file_path)
	if ocrspace_text and _consider(extract_mrz_from_raw_text(ocrspace_text)):
		return _finalize_valid()

	# 3. PassportEye's own locator -- re-run its raw OCR text through our own checksum-aware
	# parser rather than trusting its dict directly (it has no equivalent composite gate, and
	# would otherwise hand back an un-validated guess as if it were confirmed).
	if read_mrz:
		try:
			mrz = read_mrz(file_path)
			raw_text = mrz.to_dict().get("raw_text") if mrz else None
			if raw_text and _consider(extract_mrz_from_raw_text(raw_text)):
				return _finalize_valid()
		except Exception:
			pass

	# 4. Our own Tesseract candidate-crop pipeline -- keeps going even after a fully-valid read,
	# as long as its name split still looks implausible, because a different crop/scale/
	# preprocessing/PSM combination can OCR the exact same line more cleanly and recover a
	# correct name split or a checksummed field an earlier attempt missed. Only reached at all
	# when steps 2/3 didn't already produce a clean, confident hit.
	try:
		img = _load_upright_image(file_path)
		done = False
		for _crop_label, crop in _candidate_mrz_crops(img):
			for _attempt_label, ocr_text in _candidate_ocr_texts(crop):
				if _consider(extract_mrz_from_raw_text(ocr_text)):
					done = True
					break
			if done:
				break
	except Exception:
		frappe.log_error(title="Passport MRZ candidate-crop pipeline failed", message=frappe.get_traceback())

	if best_valid:
		result = _finalize_valid()
		if best_valid_names == 0:
			# Every checksummed field agrees, but no attempt produced a plausible name split --
			# still the most useful result available (names visibly need a quick fix, everything
			# else is confirmed correct), so surface it rather than discarding a mostly-good read.
			result["needs_passport_review"] = 1
		return result

	# Nothing validated fully across every strategy -- return the best (but unverified) guess,
	# still flagged needs_passport_review with names withheld by map_mrz_fields' own gate. Better
	# than blank for the fields that DO have partial checksum support; staff re-key the rest.
	if best_result:
		return map_mrz_fields(best_result)

	return {}


@frappe.whitelist()
def parse_passport_file(file_url: str = None, **kwargs) -> dict:
	"""Whitelisted endpoint to parse an uploaded passport scan. Internal staff only (audit G-004:
	was ungated) and it resolves ONLY a real uploaded File record -- it never treats the argument
	as a raw filesystem path, closing the arbitrary-local-file-read hole."""
	from agency_tracking.roles import require_internal_staff

	require_internal_staff()
	file_doc = frappe.db.get_value("File", {"file_url": file_url}, "name")
	if not file_doc:
		frappe.throw("A valid uploaded File is required.", frappe.ValidationError)
	file_path = frappe.get_doc("File", file_doc).get_full_path()
	return parse_passport_mrz(file_path)


@frappe.whitelist()
def enqueue_parse_passport_file(file_url: str = None, **kwargs):
	"""Async twin of parse_passport_file -- same permission gate and File-resolution check, but
	returns a Background Job reference immediately instead of blocking on the OCR. Poll
	background_jobs.get_job_status(job) for the result."""
	from agency_tracking.roles import require_internal_staff

	require_internal_staff()
	file_doc = frappe.db.get_value("File", {"file_url": file_url}, "name")
	if not file_doc:
		frappe.throw("A valid uploaded File is required.", frappe.ValidationError)

	from agency_tracking.background_jobs import enqueue_job

	job = enqueue_job(
		"Parse Passport",
		reference_doctype="File",
		reference_name=file_doc,
		file_url=file_url,
	)
	return {"job": job, "status": "Queued"}


