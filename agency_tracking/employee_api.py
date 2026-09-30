# Copyright (c) 2026, Agency and contributors
# License: MIT. See LICENSE

import frappe
from frappe.utils.password import update_password

from agency_tracking.install import ROLES as APP_ROLES

STAFF_ADMIN_ROLES = {"Admin", "Manager", "System Manager", "Administrator"}


def _display_roles(role_names):
	"""What the UI should show for a user's roles: only THIS app's own roles (never Frappe's ~30
	built-ins), and a single general "Admin" for a super-user who holds everything (avoids the
	cluttered all-17-roles list). 2026-09-05."""
	rs = set(role_names)
	if {"Admin", "System Manager"} & rs:
		return ["Admin"]
	return [r for r in APP_ROLES if r in rs]


def _check_staff_admin_perm():
	if frappe.session.user == "Administrator":
		return
	roles = set(frappe.get_roles())
	if not (STAFF_ADMIN_ROLES & roles):
		frappe.throw("Not permitted. Only system administrators and managers can manage staff.", frappe.PermissionError)


# The app-level admin roles that matter for the lockout guards below -- deliberately excludes the
# literal "Administrator" super-user account (handled separately, has no Has Role row to count).
_APP_ADMIN_ROLES = {"Admin", "Manager", "System Manager"}


def _other_enabled_staff_admins_exist(exclude_email):
	"""Is there at least one OTHER enabled account (besides exclude_email, and besides the
	built-in Administrator, which can't be disabled/deleted through this API anyway) still
	holding an app-admin role? Used to stop the second-to-last admin from disabling/demoting the
	last one and bricking staff-admin access for everybody, not just themselves."""
	holders = frappe.get_all(
		"Has Role",
		filters={"role": ["in", list(_APP_ADMIN_ROLES)], "parent": ["not in", [exclude_email, "Administrator"]]},
		pluck="parent",
	)
	if not holders:
		return False
	return frappe.db.count("User", {"name": ["in", list(set(holders))], "enabled": 1}) > 0


def _guard_against_admin_lockout(email, new_role_list=None, disabling=False):
	"""Refuse a role change / disable that would strip email's own staff-admin capability, when
	that would either lock the caller out of their own session (self-lockout) or leave the whole
	system with no staff-admin account left able to fix it back through the app (total lockout).
	Found live 2026-09-11: update_employee_roles previously let a caller wipe their own (or the
	last remaining) Admin/Manager/System Manager role with zero warning -- `user.roles = []` then
	re-appended from whatever role_list was submitted, no protection at all. A profile-edit flow
	that submitted an incomplete/empty roles list silently locked the account out: still logged
	in, but every permission-gated endpoint correctly 403s from then on since the roles are
	genuinely gone from the database, not a session/cache issue."""
	if email == "Administrator":
		return  # the built-in super-user; always has full access regardless of Has Role rows
	still_admin_capable = disabling is False and bool((new_role_list or set()) & _APP_ADMIN_ROLES)
	if still_admin_capable:
		return
	was_admin_capable = bool(set(frappe.get_roles(email)) & _APP_ADMIN_ROLES)
	if not was_admin_capable:
		return  # nothing admin-capable being lost
	if email == frappe.session.user:
		frappe.throw(
			"Refusing to remove your own Admin/Manager/System Manager access -- this would lock "
			"you out of your own session. Have another admin make this change instead.",
			frappe.ValidationError,
		)
	if not _other_enabled_staff_admins_exist(email):
		frappe.throw(
			f"Refusing: {email} is the last remaining staff-admin account. This would leave the "
			"system with nobody able to manage staff through the app.",
			frappe.ValidationError,
		)


@frappe.whitelist()
def list_employees():
	"""Returns all system users with their assigned security roles aggregated in a single query."""
	_check_staff_admin_perm()

	users = frappe.get_all(
		"User",
		filters=[
			["User", "user_type", "=", "System User"],
			["User", "name", "!=", "Guest"],
		],
		fields=[
			"name",
			"email",
			"full_name",
			"first_name",
			"last_name",
			"enabled",
			"user_type",
			"creation",
			"phone",
			"mobile_no",
		],
		order_by="creation desc",
		limit_page_length=200,
	)

	# Batch-fetch all roles for these users in one single query
	user_names = [u["name"] for u in users]
	if not user_names:
		return []

	all_roles = frappe.get_all(
		"Has Role",
		filters={"parent": ["in", user_names]},
		fields=["parent", "role"],
	)

	roles_by_user = {}
	for r in all_roles:
		if r["role"] not in ("All",):
			roles_by_user.setdefault(r["parent"], []).append(r["role"])

	for u in users:
		# Administrator has every role via the framework, not always via Has Role rows.
		raw = roles_by_user.get(u["name"], [])
		if u["name"] == "Administrator":
			raw = list(raw) + ["Admin"]
		u["roles"] = _display_roles(raw)

	return users


@frappe.whitelist()
def list_employee_roster():
	"""Lightweight staff roster (name/email/roles only, enabled accounts) for internal features
	that need to know who holds which role but aren't staff *administration* itself --
	clearance-step assignment pickers, chat participant role labels. Open to any internal staff
	role (agency_tracking.roles.INTERNAL_STAFF_ROLES), not just Admin/Manager/System Manager like
	list_employees -- a Ticketer legitimately needs to see who holds "Saudi LMIS" to assign a
	step to them. Deliberately excludes phone/mobile_no/creation/user_type/enabled-toggle --
	just enough to pick a colleague by role, nothing account-management-shaped.

	2026-09-11: added to replace the frontend's own raw frappe.client.get_list + a
	frappe.client.get PER USER on the User doctype directly, found live -- that path had no role
	gate at all (Frappe's own default User read permission let any Desk User, i.e. any employee,
	reach it), returned full User documents, and was an N+1 query besides."""
	from agency_tracking.roles import require_internal_staff

	require_internal_staff()

	users = frappe.get_all(
		"User",
		filters=[
			["User", "user_type", "=", "System User"],
			["User", "name", "!=", "Guest"],
			["User", "enabled", "=", 1],
		],
		fields=["name", "email", "full_name", "first_name", "last_name", "enabled"],
		order_by="full_name asc",
		limit_page_length=200,
	)
	user_names = [u["name"] for u in users]
	if not user_names:
		return []

	all_roles = frappe.get_all(
		"Has Role",
		filters={"parent": ["in", user_names]},
		fields=["parent", "role"],
	)
	roles_by_user = {}
	for r in all_roles:
		if r["role"] not in ("All",):
			roles_by_user.setdefault(r["parent"], []).append(r["role"])

	for u in users:
		raw = roles_by_user.get(u["name"], [])
		if u["name"] == "Administrator":
			raw = list(raw) + ["Admin"]
		u["roles"] = _display_roles(raw)

	return users


def _validate_no_conflicting_roles(role_list):
	"""Segregation of duties & multi-tenant isolation: Foreign Agency (partner portal)
	cannot be combined with internal staff roles."""
	if "Foreign Agency" in role_list:
		from agency_tracking.roles import INTERNAL_STAFF_ROLES
		conflicting = set(role_list) & INTERNAL_STAFF_ROLES
		if conflicting:
			frappe.throw(
				f"Cross-tenant role conflict: 'Foreign Agency' cannot be combined with internal staff roles ({', '.join(sorted(conflicting))}).",
				frappe.ValidationError,
			)


@frappe.whitelist()
def create_employee(email=None, first_name=None, last_name=None, phone=None, password=None, roles=None, send_welcome_email=0, **kwargs):
	"""Creates a new system employee account with role assignments."""
	_check_staff_admin_perm()

	email = (email or kwargs.get("name") or "").strip().lower()
	first_name = (first_name or "").strip()
	last_name = (last_name or "").strip()
	phone = (phone or "").strip()
	password = (password or "AgencyStaff123!").strip()

	if not email or not first_name:
		frappe.throw("Email and First Name are required.", frappe.ValidationError)

	if frappe.db.exists("User", email):
		frappe.throw(f"User '{email}' already exists.", frappe.DuplicateEntryError)

	if isinstance(roles, str):
		roles = frappe.parse_json(roles)
	role_list = set(roles or [])
	_validate_no_conflicting_roles(role_list)
	role_list.add("Desk User")

	user = frappe.get_doc(
		{
			"doctype": "User",
			"email": email,
			"first_name": first_name,
			"last_name": last_name,
			"phone": phone,
			"mobile_no": phone,
			"new_password": password,
			"send_welcome_email": 1 if send_welcome_email else 0,
			"roles": [{"role": r} for r in role_list],
		}
	).insert(ignore_permissions=True)

	return {
		"name": user.name,
		"email": user.email,
		"full_name": user.full_name or f"{first_name} {last_name}".strip(),
		"first_name": user.first_name,
		"last_name": user.last_name,
		"phone": user.phone,
		"mobile_no": user.mobile_no,
		"enabled": user.enabled,
		"user_type": user.user_type,
		"creation": str(user.creation),
		"roles": [r.role for r in user.roles if r.role != "All"],
	}


@frappe.whitelist()
def update_employee_roles(email=None, roles=None, **kwargs):
	"""Updates assigned roles for an existing employee."""
	_check_staff_admin_perm()

	email = email or kwargs.get("name")
	if not email:
		frappe.throw("Email is required.", frappe.ValidationError)

	if isinstance(roles, str):
		roles = frappe.parse_json(roles)

	role_list = set(roles or [])
	_validate_no_conflicting_roles(role_list)
	_guard_against_admin_lockout(email, new_role_list=role_list)
	role_list.add("Desk User")

	user = frappe.get_doc("User", email)
	user.roles = []
	for r in role_list:
		user.append("roles", {"role": r})
	user.save(ignore_permissions=True)

	return [r.role for r in user.roles if r.role != "All"]


@frappe.whitelist()
def reset_employee_password(email=None, new_password=None, **kwargs):
	"""Resets employee password."""
	_check_staff_admin_perm()

	email = email or kwargs.get("name")
	new_password = new_password or kwargs.get("password")
	if not email or not new_password:
		frappe.throw("Both email and new_password are required.", frappe.ValidationError)

	update_password(email, new_password)
	return {"status": "success"}


@frappe.whitelist()
def toggle_employee_status(email=None, enabled=None, **kwargs):
	"""Toggles active/inactive status."""
	_check_staff_admin_perm()

	email = email or kwargs.get("name")
	if not email:
		frappe.throw("Email is required.", frappe.ValidationError)

	val = 1 if enabled in (1, "1", True, "true", "True") else 0
	if not val:
		_guard_against_admin_lockout(email, disabling=True)
	frappe.db.set_value("User", email, "enabled", val)
	return {"status": "success", "enabled": val}


@frappe.whitelist()
def delete_employee(email=None, **kwargs):
	"""Deletes an employee account."""
	_check_staff_admin_perm()

	email = email or kwargs.get("name")
	if not email:
		frappe.throw("Email is required.", frappe.ValidationError)

	if email in ("Administrator", "admin@example.com", frappe.session.user):
		frappe.throw("Cannot delete primary system administrator or current session user.", frappe.ValidationError)

	frappe.delete_doc("User", email, ignore_permissions=True)
	return {"status": "success"}
