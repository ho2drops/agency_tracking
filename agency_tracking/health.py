# Copyright (c) 2026, Agency and contributors
# License: MIT. See LICENSE
#
# Lightweight, unauthenticated liveness/readiness probe for load balancers, uptime monitors and
# deploy smoke-tests. Deliberately cheap: one trivial DB round-trip, no business logic, no auth.

import frappe


@frappe.whitelist(allow_guest=True)
def health_check():
	"""GET/POST /api/method/agency_tracking.health.health_check

	Returns 200 with {"status": "ok"} when the web worker is up AND the database answers a trivial
	query; returns {"status": "error"} (still HTTP 200 so the payload is readable) if the DB probe
	fails. allow_guest so monitors don't need credentials -- it exposes no data beyond up/down,
	the app name/version, and server time."""
	db_ok = True
	try:
		frappe.db.sql("SELECT 1")
	except Exception:
		db_ok = False

	try:
		version = frappe.get_attr("agency_tracking.__version__")
	except (AttributeError, ImportError):
		version = None

	return {
		"status": "ok" if db_ok else "error",
		"service": "agency_tracking",
		"version": version,
		"database": "ok" if db_ok else "down",
		"time": frappe.utils.now(),
	}
