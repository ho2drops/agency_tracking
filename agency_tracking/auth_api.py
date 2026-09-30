# Copyright (c) 2026, Agency and contributors
# License: MIT. See LICENSE
#
# Part F: "Auth: session-cookie for everyone (staff and agencies) for now." A session-cookie
# SPA needs two small pieces of plumbing the spec doesn't spell out but implies: a way to fetch
# the CSRF token Frappe requires on every state-changing request once logged in (frappe.auth.
# validate_csrf_token), and a way to know who's logged in and what they can do without loading
# the Desk UI. Both are read-only, no-side-effect, and only meaningful to an already-
# authenticated session — not a spec violation to add, just the minimum a headless frontend
# needs against Frappe's session model.

import frappe

from agency_tracking.install import ROLES as APP_ROLES
from agency_tracking.roles import MANAGEMENT_ROLES, is_internal_staff


@frappe.whitelist(allow_guest=True)
def get_csrf_token():
	return {"csrf_token": frappe.sessions.get_csrf_token()}


@frappe.whitelist(allow_guest=True)
def get_current_user():
	"""allow_guest=True deliberately, with an explicit Guest check inside — not a security
	relaxation. Without it, an anonymous SPA's "is there already a session?" bootstrap check
	(needed so a page refresh doesn't force a fresh login) hits Frappe's own whitelist guard
	before this function's body ever runs, producing a 403 on every single cold page load.
	Returning None for Guest instead of throwing means that bootstrap check is a normal 200
	response either way — "nobody's logged in" is an expected outcome, not an error.
	"""
	user = frappe.session.user
	if user == "Guest":
		return None
	raw_roles = set(frappe.get_roles(user))
	staff = is_internal_staff(user)

	# Only expose THIS app's own roles -- never Frappe's ~30 built-ins (All, Guest, Desk User,
	# Website Manager, Blogger, ...), which are noise to the UI. And an admin / super-user holds
	# every role, so collapse that to a single general "Admin" label instead of a cluttered list of
	# all 17 (2026-09-05). Everyone else shows exactly the app roles they actually hold.
	is_admin = user == "Administrator" or bool({"Admin", "System Manager"} & raw_roles)
	roles = ["Admin"] if is_admin else [r for r in APP_ROLES if r in raw_roles]

	# Contractor context so a Foreign Agency SPA knows its own tenant identity directly from the
	# session bootstrap. Server-derived from the linked User only. None for internal staff / unlinked.
	contractor = frappe.db.get_value("Contractor", {"user": user}, "name")
	return {
		"user": user,
		"full_name": frappe.db.get_value("User", user, "full_name"),
		"roles": roles,
		"contractor": contractor,
		"is_internal_staff": staff,
	}


@frappe.whitelist()
def get_assignable_roles():
	"""The roles this app actually uses, for populating a role-assignment picker.

	Frappe's generic `/api/resource/Role` returns all ~30 built-ins (System Manager, Guest,
	All, Blogger, Website Manager, ...) that have no meaning in this app's RBAC — install.ROLES
	is the source of truth for the roles we create and check against, so we return exactly those.
	Filtered to roles that actually exist as Role docs, so the list reflects reality if a role
	hasn't been seeded yet (e.g. fresh site before after_install ran).

	Restricted to management, since assigning roles is an admin action.
	"""
	if frappe.session.user != "Administrator" and not (
		MANAGEMENT_ROLES & set(frappe.get_roles())
	):
		frappe.throw(frappe._("Not permitted"), frappe.PermissionError)

	existing = set(frappe.get_all("Role", filters={"name": ["in", APP_ROLES]}, pluck="name"))
	# Preserve install.ROLES ordering rather than set order.
	return [role for role in APP_ROLES if role in existing]
