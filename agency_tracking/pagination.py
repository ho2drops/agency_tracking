# Copyright (c) 2026, Agency and contributors
# License: MIT. See LICENSE
#
# Shared paging for the list endpoints that back frontend tables (2026-09-23). Before this, most
# of them took only limit_page_length (default 100) with no offset and no total, so rows past the
# first 100 were silently unreachable -- and limit_page_length=0 ("all") still returned 100.
#
# Contract, identical on every endpoint that uses it:
#   limit_start         offset, default 0
#   limit_page_length   page size, clamped to 1..MAX_PAGE_LENGTH; blank/0 -> the endpoint's default
#   with_total=1        return {"data": [...], "total_count": N} instead of a bare list. Off by
#                       default so existing callers keep receiving a list. total_count uses the
#                       same filters AND the same permission scope as the rows.

import frappe
from frappe.utils import cint

MAX_PAGE_LENGTH = 500


def require_list_permission(doctype):
	"""The same doctype-level check frappe.get_list runs (select or read), but refused with a
	message: get_list raises a bare PermissionError, which reached the frontend as an empty error
	(QA P7-03). Nobody is refused who wasn't already."""
	ptype = "select" if frappe.only_has_select_perm(doctype) else "read"
	if not frappe.has_permission(doctype, ptype=ptype):
		frappe.throw("Not permitted.", frappe.PermissionError)


def page_args(limit_start=None, limit_page_length=None, default=100):
	"""(start, length) for frappe.get_list/get_all: no/invalid size -> `default`, always capped at
	MAX_PAGE_LENGTH. No list returns the whole table in one call (2026-09-30: list_complaints and
	list_portal_candidates used to, via default=0)."""
	start = max(cint(limit_start), 0)
	length = cint(limit_page_length)
	if length <= 0:
		length = default
	return start, min(length, MAX_PAGE_LENGTH)


def count_rows(doctype, filters=None, ignore_permissions=False):
	"""Row count for `filters`, through frappe.get_list so permission scoping matches the rows
	(ignore_permissions=True for endpoints that fetch with get_all after their own role check)."""
	result = frappe.get_list(
		doctype,
		filters=filters,
		fields=["count(*) as total_count"],
		ignore_permissions=ignore_permissions,
		order_by=None,
	)
	return cint(result[0].total_count) if result else 0


def paged_result(rows, with_total, total_fn):
	if cint(with_total):
		return {"data": rows, "total_count": total_fn()}
	return rows
