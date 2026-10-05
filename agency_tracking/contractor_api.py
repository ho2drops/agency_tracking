# Copyright (c) 2026, Agency and contributors
# License: MIT. See LICENSE
#
# Part F: module-scoped whitelisted functions, no raw /api/resource/* exposure.
#
# backend-issues #07: Contractor had no whitelisted create/list surface anywhere. A Registrar
# creating a Muayena placement (the documented "contract in hand, no portal" flow) had no
# sanctioned way to pick or register the foreign agency the worker is going to --
# create_muayena_placement's contractor_name is a strict Link to an existing Contractor record.

import frappe

from agency_tracking.pagination import count_rows, page_args, paged_result
from agency_tracking.state_machine import log_action

CONTRACTOR_MANAGE_ROLES = {"Manager", "Admin", "Finance Manager", "Registrar", "Communication Manager", "System Manager"}


@frappe.whitelist()
def create_contractor(contractor_name=None, country=None, user_email=None, user_first_name=None, communication_manager=None, **kwargs):
	"""Registers a new foreign agency and its portal login in one step."""
	if not (CONTRACTOR_MANAGE_ROLES & set(frappe.get_roles())):
		frappe.throw("Not permitted.", frappe.PermissionError)

	contractor_name = contractor_name or kwargs.get("name") or f"Agency {frappe.generate_hash(length=5)}"
	country = country or kwargs.get("destination_country")
	if not country:
		# An agency works one country; guessing one (it used to become Saudi Arabia) mis-files it (S-25).
		frappe.throw("country is required.", frappe.ValidationError)
	user_email = user_email or kwargs.get("email") or kwargs.get("user") or f"agency.{frappe.generate_hash(length=5)}@example.local"
	user_first_name = user_first_name or kwargs.get("first_name") or contractor_name

	if not frappe.db.exists("User", user_email):
		user = frappe.get_doc(
			{
				"doctype": "User",
				"email": user_email,
				"first_name": user_first_name,
				"send_welcome_email": 0,
				"roles": [{"role": "Foreign Agency"}],
			}
		).insert(ignore_permissions=True)
		user_name = user.name
	else:
		user_name = user_email

	contractor = frappe.get_doc(
		{
			"doctype": "Contractor",
			"contractor_name": contractor_name,
			"country": country,
			"user": user_name,
			"communication_manager": communication_manager,
		}
	).insert(ignore_permissions=True)
	return contractor.as_dict()


@frappe.whitelist()
def list_contractors(filters=None, limit_page_length=100, limit_start=0, with_total=0):
	"""Read surface for picking an existing agency (create_muayena_placement's contractor_name,
	Finance's batching/rate lookups). Same role gate as create_contractor -- the doctype's own
	permlevel-0 grants don't cover Registrar/Finance Manager at all, so this uses an explicit
	role check plus frappe.get_all (skips doctype permission checks) rather than frappe.get_list,
	same pattern as finance_api._log_stage_transaction's "permissive write, gated read" shape."""
	if not (CONTRACTOR_MANAGE_ROLES & set(frappe.get_roles())):
		frappe.throw("Not permitted.", frappe.PermissionError)
	if isinstance(filters, str):
		filters = frappe.parse_json(filters)
	start, length = page_args(limit_start, limit_page_length)
	rows = frappe.get_all(
		"Contractor",
		filters=filters,
		fields=["name", "contractor_name", "country", "user", "communication_manager"],
		limit_start=start,
		limit_page_length=length,
		order_by="modified desc",
	)
	return paged_result(rows, with_total, lambda: count_rows("Contractor", filters, ignore_permissions=True))


# Contractor edit is a management action (matches create_contractor's gate).
CONTRACTOR_UPDATE_ROLES = {"Admin", "Manager", "System Manager"}
# Only these fields are hand-editable via update_contractor. The linked `user` is deliberately NOT
# here (re-pointing an agency's login is an account action, not a routine profile edit), and the
# default_commission_rates table is managed through set_commission_rates.
CONTRACTOR_EDITABLE_FIELDS = ("contractor_name", "country", "communication_manager", "batch_mode", "batch_threshold")


@frappe.whitelist()
def update_contractor(contractor=None, **data):
	"""Edit an existing agency's profile fields (Admin/Manager). Only CONTRACTOR_EDITABLE_FIELDS
	are accepted; the login user and the commission-rate table are managed elsewhere. Lifecycle-
	safe: there's no status field on Contractor to move, so this is a plain profile edit."""
	contractor = contractor or data.pop("name", None)
	if not (CONTRACTOR_UPDATE_ROLES & set(frappe.get_roles())):
		frappe.throw("Not permitted.", frappe.PermissionError)
	if not contractor or not frappe.db.exists("Contractor", contractor):
		frappe.throw("A valid contractor is required.", frappe.ValidationError)

	updates = {k: v for k, v in data.items() if k in CONTRACTOR_EDITABLE_FIELDS}
	if not updates:
		frappe.throw(
			"No editable field supplied. Allowed: " + ", ".join(CONTRACTOR_EDITABLE_FIELDS),
			frappe.ValidationError,
		)
	# contractor_name is the Contractor's autoname (field:contractor_name), so changing it is a
	# rename (cascades to every link), not a plain field write.
	changes = [f"{k}={v}" for k, v in updates.items()]
	new_name = updates.pop("contractor_name", None)
	if new_name and new_name != contractor:
		if frappe.db.exists("Contractor", new_name):
			frappe.throw(f"A Contractor named '{new_name}' already exists.", frappe.ValidationError)
		frappe.rename_doc("Contractor", contractor, new_name)
		contractor = new_name

	doc = frappe.get_doc("Contractor", contractor)
	if updates:
		doc.update(updates)
		doc.save(ignore_permissions=True)
	log_action("Contractor", doc.name, "Profile updated: " + ("; ".join(changes) if changes else "(no changes)"))
	return doc.as_dict()


_RATE_TRACKS = {"Standard", "Muayena"}
_RATE_GENDERS = {"Male", "Female"}
_RATE_CURRENCIES = {"SAR", "KWD", "USD", "ETB", "AED", "QAR"}


@frappe.whitelist()
def get_commission_rates(contractor=None, **kwargs):
	"""The agency's configured default commission rates -- one per destination country x entry
	track (Standard/Muayena) x gender (Male/Female)."""
	contractor = contractor or kwargs.get("name") or kwargs.get("contractor_name")
	if not (CONTRACTOR_MANAGE_ROLES & set(frappe.get_roles())):
		frappe.throw("Not permitted.", frappe.PermissionError)
	if not contractor:
		frappe.throw("contractor is required.", frappe.ValidationError)
	return frappe.get_all(
		"Contractor Commission Rate",
		filters={"parent": contractor, "parenttype": "Contractor"},
		fields=["destination_country", "entry_track", "gender", "rate", "currency"],
		order_by="destination_country, entry_track, gender",
	)


@frappe.whitelist()
def set_commission_rates(contractor=None, rates=None, **kwargs):
	"""Replace an agency's default commission-rate table. Each rate is
	{destination_country, entry_track, gender, rate, currency} -- so you set a per-male and
	per-female default for both the Standard and Muayena tracks (per destination country). These
	are what accrual falls back to when a placement has no manual commission override."""
	contractor = contractor or kwargs.get("name") or kwargs.get("contractor_name")
	if not (CONTRACTOR_MANAGE_ROLES & set(frappe.get_roles())):
		frappe.throw("Not permitted.", frappe.PermissionError)
	if not contractor:
		frappe.throw("contractor is required.", frappe.ValidationError)
	rates = rates if rates is not None else kwargs.get("default_commission_rates")
	if isinstance(rates, str):
		rates = frappe.parse_json(rates)
	if not rates:
		frappe.throw("rates is required (a list of rate rows).", frappe.ValidationError)

	doc = frappe.get_doc("Contractor", contractor)
	rows = []
	for r in rates:
		track = r.get("entry_track")
		gender = r.get("gender")
		currency = r.get("currency")
		if track not in _RATE_TRACKS:
			frappe.throw(f"entry_track must be one of {sorted(_RATE_TRACKS)}.", frappe.ValidationError)
		if gender not in _RATE_GENDERS:
			frappe.throw(f"gender must be one of {sorted(_RATE_GENDERS)}.", frappe.ValidationError)
		if currency not in _RATE_CURRENCIES:
			frappe.throw(f"currency must be one of {sorted(_RATE_CURRENCIES)}.", frappe.ValidationError)
		if not r.get("destination_country"):
			frappe.throw("destination_country is required on every rate row.", frappe.ValidationError)
		if r.get("rate") in (None, ""):
			frappe.throw("rate is required on every rate row.", frappe.ValidationError)
		rows.append(
			{
				"destination_country": r.get("destination_country"),
				"entry_track": track,
				"gender": gender,
				"rate": r.get("rate"),
				"currency": currency,
			}
		)

	doc.set("default_commission_rates", rows)
	doc.save(ignore_permissions=True)
	return get_commission_rates(contractor)
