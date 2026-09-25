# Copyright (c) 2026, Agency and contributors
# License: MIT. See LICENSE

import frappe
from frappe.model.document import Document
from agency_tracking.state_machine import guard_status_write


class Complaint(Document):
	def validate(self):
		guard_status_write(self, ("New",))  # QA A1: status only via the app's actions
		if self.status == "Dismissed" and not self.resolution_notes:
			frappe.throw(
				"A written reason is required to dismiss a complaint (business-workflow-srs.md).",
				frappe.ValidationError,
			)
		self._set_display_no()

	def _set_display_no(self):
		"""'#1', '#2', ... instead of CMP-00001 wherever a complaint is shown to a user -- derived
		from name's own numeric suffix (autoname 'CMP-.#####' already hands out a unique,
		monotonic sequence; no separate counter needed). self.name is already assigned by the time
		validate() runs (autoname happens before the before_save hooks -- same pattern as
		commission_batch_request.py/_set_title). Set once and never recomputed, so it can't shift
		under an existing complaint."""
		if self.display_no or not self.name:
			return
		try:
			self.display_no = int(self.name.rsplit("-", 1)[-1])
		except ValueError:
			pass
