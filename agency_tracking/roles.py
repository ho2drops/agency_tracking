# Copyright (c) 2026, Agency and contributors
# License: MIT. See LICENSE
#
# Named constants for every custom role this app defines (agency_tracking/install.py's ROLES
# list is the source of truth for what actually gets created) -- not Frappe's ~30 built-in
# roles (System Manager, Website Manager, etc.), which have no reason to appear in this app's
# own permission checks. One place to see "every role this app defines" without cross-
# referencing Frappe's full role list, and a shared source for the common role-set groupings
# that were previously scattered as ad-hoc {...} literals across the API modules.

import frappe

REGISTRAR = "Registrar"
MANAGER = "Manager"
ADMIN = "Admin"
CLEARANCE_OFFICER = "Clearance Officer"
TICKETER = "Ticketer"
COMPLAINT_MANAGER = "Complaint Manager"
FINANCE_MANAGER = "Finance Manager"
FOREIGN_AGENCY = "Foreign Agency"
COMMUNICATION_MANAGER = "Communication Manager"
CONTRACT_PARSER = "Contract Parser"
SAUDI_LMIS = "Saudi LMIS"
SAUDI_TAESHIR = "Saudi Taeshir"
SAUDI_EMBASSY = "Saudi Embassy"
KUWAIT_LMIS = "Kuwait LMIS"
KUWAIT_TELESIGN = "Kuwait Telesign"
KUWAIT_EMBASSY = "Kuwait Embassy"
# Records the post-Selected and pre-departure medical results (placement_api.record_*_medical_result).
MEDICAL_OFFICER = "Medical Officer"

CLEARANCE_COUNTRY_ROLES = {SAUDI_LMIS, SAUDI_TAESHIR, SAUDI_EMBASSY, KUWAIT_LMIS, KUWAIT_TELESIGN, KUWAIT_EMBASSY}

# Every role that represents an actual employee of the agency (excludes Foreign Agency, which
# is portal-only/external). Used wherever an action is open to "any internal staff" rather than
# a specific role -- e.g. finance_api.log_stage_expense/log_stage_income.
INTERNAL_STAFF_ROLES = {
	REGISTRAR,
	CLEARANCE_OFFICER,
	TICKETER,
	COMPLAINT_MANAGER,
	FINANCE_MANAGER,
	COMMUNICATION_MANAGER,
	MANAGER,
	ADMIN,
	CONTRACT_PARSER,
	MEDICAL_OFFICER,
	"Administrator",
	"System Manager",
} | CLEARANCE_COUNTRY_ROLES

# Internal staff who may create a Muayena placement (placement_api.create_muayena_placement).
MUAYENA_PLACEMENT_ROLES = {REGISTRAR, MANAGER, ADMIN, CONTRACT_PARSER, "System Manager"}

# Roles that see cross-cutting management reports (report_api.py's Manager-tier functions).
MANAGEMENT_ROLES = {MANAGER, ADMIN, "Administrator", "System Manager", FINANCE_MANAGER}


def is_internal_staff(user=None):
	"""True when the user holds any INTERNAL_STAFF_ROLES role (Administrator holds every role)."""
	return bool(INTERNAL_STAFF_ROLES & set(frappe.get_roles(user)))


def require_internal_staff():
	"""Refuse the current session user unless they are internal staff."""
	if not is_internal_staff():
		frappe.throw("Not permitted.", frappe.PermissionError)
