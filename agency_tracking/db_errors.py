# Copyright (c) 2026, Agency and contributors
# License: MIT. See LICENSE
#
# QA B-b1: best-effort blocks (log_action, transition side effects, auto-advance, stage fees)
# catch Exception so a failed side step never unwinds work that already happened. A database
# abort is different: MariaDB has already rolled the WHOLE transaction back (deadlock, lock wait
# timeout, or snapshot-isolation conflict 1020, which Frappe raises as QueryDeadlockError), so the
# "work that already happened" is gone too. Swallowing it let the request carry on and report
# success for writes that were never kept (QA P4-01). Those errors must reach the caller.

import frappe


class ConcurrentUpdateError(frappe.ValidationError):
	"""Someone else changed the same records at the same moment; nothing was saved. Retryable."""

	http_status_code = 409


def reraise_if_db_abort(exc):
	"""Call first inside a broad `except Exception as exc:`. Re-raises a transaction abort as a
	409 the client can retry; returns normally for every other error."""
	if isinstance(exc, (frappe.QueryDeadlockError, frappe.QueryTimeoutError)):
		raise ConcurrentUpdateError(
			"This record was changed by someone else at the same moment, so nothing was saved. "
			"Please try again."
		) from exc
