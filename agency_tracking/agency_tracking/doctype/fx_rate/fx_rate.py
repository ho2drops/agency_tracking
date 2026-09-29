# Copyright (c) 2026, Agency and contributors
# License: MIT. See LICENSE

import frappe
from frappe.model.document import Document


class FXRate(Document):
	def validate(self):
		from agency_tracking.finance_engine import positive_rate

		positive_rate(self.rate_to_birr)  # also covers rates entered in Desk (QA P7-02)
		existing = frappe.db.get_value(
			"FX Rate",
			{"currency": self.currency, "rate_date": self.rate_date, "name": ["!=", self.name or ""]},
			"name",
		)
		if existing:
			frappe.throw(
				f"An FX Rate for {self.currency} on {self.rate_date} already exists ({existing}).",
				frappe.DuplicateEntryError,
			)

	def on_update(self):
		# Convert anything recorded in this currency while no rate existed (finance_engine).
		from agency_tracking.finance_engine import convert_awaiting_fx

		convert_awaiting_fx(self.currency)
