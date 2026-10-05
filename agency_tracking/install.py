# Copyright (c) 2026, Agency and contributors
# License: MIT. See LICENSE

import frappe
from agency_tracking.db_errors import reraise_if_db_abort

# Part G RBAC table, pre-declared in full (see BUILD_LOG.md "Standing decisions") — the
# roles that don't have logic attached yet simply sit unused until their build step.
# "Communication Manager" isn't in the master spec's original Part G table — it's introduced by
# the addendum ("Agencies talk only to Communication Manager"), which explicitly overrides/
# extends Part G. Added here at Step 12 rather than pre-declared with the rest in Step 1,
# since it didn't exist in the spec version Step 1 was built against.
ROLES = [
	"Registrar",
	"Clearance Officer",
	"Ticketer",
	"Complaint Manager",
	"Finance Manager",
	"Manager",
	"Admin",
	"Foreign Agency",
	"Communication Manager",
	# 2026-08-29 additions: Contract Parser owns getting a contract/visa onto a Placement for
	# either track. The six country+step roles replace per-row ToDo assignment as the actual
	# permission gate for Clearance Step (ToDo is kept only for the notification queue) — see
	# clearance_step.py's get_permission_query_conditions and CLEARANCE_ROLE_BY_STEP_TYPE.
	"Contract Parser",
	"Saudi LMIS",
	"Saudi Taeshir",
	"Saudi Embassy",
	"Kuwait LMIS",
	"Kuwait Telesign",
	"Kuwait Embassy",
	# 2026-09-05: records the post-Selected and pre-departure medical results.
	"Medical Officer",
	# 2026-09-30: prepares and generates CVs (see roles.CV).
	"CV",
]


# Part A.3 / business-workflow-srs.md Stage 5: the first two configured corridors, proving the
# data-driven engine before anything downstream depends on it. Steps are all mandatory for now
# — the spec doesn't call out an optional step in either corridor yet.
#
# 2026-08-29 correction: the original data here was placeholder, attributed to a
# "business-workflow-srs.md" document that doesn't actually exist anywhere in this repo —
# unverifiable, not confirmed business fact. Corrected directly against the real process:
# Saudi = LMIS Clearance + Taeshir run in parallel (both sequence_order 1; Injaz is data
# captured *inside* the Taeshir Clearance Step, not its own step) -> Embassy (order 2, renamed
# from "Embassy/Wakala"; Wakala is likewise fields inside Embassy, not its own step). Kuwait =
# Kuwait LMIS (Police Ashara folded in as fields, renamed from "LMIS Police Clearance") ->
# Telesign -> Kuwait Embassy ("LMIS Work Permit" dropped entirely, wasn't real).
CORRIDORS = {
	"Saudi Arabia": [
		# LMIS Clearance and Taeshir have no dependency on each other -- both can start
		# immediately and run independently. sequence_order still must be unique
		# (Corridor Definition.validate_unique_sequence_orders), so this is display ordering
		# only, not a hard "must finish 1 before 2" gate -- nothing in the codebase enforces
		# step-to-step sequencing anyway (all_mandatory_clearance_steps_complete just checks
		# every mandatory row is done, regardless of order).
		{"step_type": "LMIS Clearance", "sequence_order": 1, "is_mandatory": 1},
		{"step_type": "Taeshir", "sequence_order": 2, "is_mandatory": 1},
		{"step_type": "Embassy", "sequence_order": 3, "is_mandatory": 1},
	],
	"Kuwait": [
		{"step_type": "Kuwait LMIS", "sequence_order": 1, "is_mandatory": 1},
		{"step_type": "Telesign", "sequence_order": 2, "is_mandatory": 1},
		{"step_type": "Kuwait Embassy", "sequence_order": 3, "is_mandatory": 1},
	],
}


def after_install():
	create_roles()
	create_corridors()
	configure_session_expiry()


def before_tests():
	"""Frappe's standard pre-test-suite hook (wired in hooks.py) — runs once before any test
	module. Without this, whichever test module happens to run first alphabetically implicitly
	determines whether seed data (roles, corridors) exists yet, which is exactly the ordering
	bug this fixes: test_clearance_engine.py < test_corridor_engine.py alphabetically, so
	clearance tests were running before corridor_engine's own tests had a chance to
	self-seed via their create_corridors() calls."""
	create_roles()
	create_corridors()


def create_roles():
	for role_name in ROLES:
		if frappe.db.exists("Role", role_name):
			continue
		# Foreign Agency is portal-only (Part G) — never gets Desk access.
		desk_access = 0 if role_name == "Foreign Agency" else 1
		frappe.get_doc(
			{
				"doctype": "Role",
				"role_name": role_name,
				"desk_access": desk_access,
			}
		).insert(ignore_permissions=True)


def create_corridors():
	"""Upsert, not skip-if-exists — CORRIDORS is the source of truth for what a corridor's
	steps *should* be, and a stale existing record (e.g. from before the 2026-08-29 step
	correction) must actually get synced, not silently left wrong forever."""
	for destination_country, steps in CORRIDORS.items():
		existing_name = frappe.db.exists("Corridor Definition", destination_country)
		if existing_name:
			doc = frappe.get_doc("Corridor Definition", existing_name)
			doc.reload() # Force reload from DB to avoid TimestampMismatchError in tests
			doc.set("steps", steps)
			doc.save(ignore_permissions=True)
		else:
			frappe.get_doc(
				{
					"doctype": "Corridor Definition",
					"destination_country": destination_country,
					"steps": steps,
				}
			).insert(ignore_permissions=True)


def configure_session_expiry():
	"""Sets idle session expiry to 24 hours ('24:00') in System Settings.

	Mirrors patches/set_default_session_expiry_24h.py, which applies the same setting
	to sites that existed before this was added to after_install().
	"""
	try:
		settings = frappe.get_doc("System Settings")
		settings.session_expiry = "24:00"
		settings.flags.ignore_mandatory = True
		settings.save(ignore_permissions=True)
	except Exception as exc:
		reraise_if_db_abort(exc)
		frappe.db.set_single_value("System Settings", "session_expiry", "24:00")
		frappe.defaults.set_global_default("session_expiry", "24:00")
	frappe.db.commit()

