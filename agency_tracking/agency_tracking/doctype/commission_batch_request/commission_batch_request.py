# Copyright (c) 2026, Agency and contributors
# License: MIT. See LICENSE

from decimal import Decimal

import frappe
from frappe.model.document import Document
from frappe.utils import flt, today


class CommissionBatchRequest(Document):
	def validate(self):
		self._apply_settlement_math()
		self._set_title()

	def _set_title(self):
		"""'{Contractor} [{batch ID}]', e.g. 'Tihamat Asir Recruitment Company [CBR-2026-00021]' --
		so this batch reads as something other than a bare sequence number everywhere it shows up
		(list views, link fields, the invoice PDF, notifications). self.name is already assigned by
		the time validate() runs (autoname happens before the before_save hooks)."""
		self.title = f"{self.contractor} [{self.name}]" if self.contractor and self.name else self.name

	def _txn_amounts(self, transaction):
		"""(original-currency amount, birr amount) for one batched commission transaction, as exact
		Decimals -- float sums drift (0.1 + 0.2 != 0.3), which kept an invoice written off to exactly
		its balance from ever reading Settled (QA P5-02)."""
		row = frappe.db.get_value(
			"Applicant Transaction", transaction, ["amount_original", "amount_birr"], as_dict=True
		)
		return _dec(row.amount_original if row else 0), _dec(row.amount_birr if row else 0)

	def paid_from_items(self):
		"""(original-currency, birr) already settled item-by-item (per-applicant marks). Excludes
		Released items."""
		original = birr = Decimal(0)
		for row in self.items or []:
			if row.status != "Paid":
				continue
			o, b = self._txn_amounts(row.transaction)
			original += o
			birr += b
		return original, birr

	def write_off_totals(self):
		"""(original-currency, birr) summed across every write-off row -- a batch can have any
		number of write-offs (agency negotiates a discount more than once), not just one. A write-off
		whose Expense transaction was voided no longer counts (QA P5-04)."""
		txns = [row.transaction for row in (self.write_offs or []) if row.transaction]
		voided = set(
			frappe.get_all(
				"Applicant Transaction", filters={"name": ["in", txns], "status": "Voided"}, pluck="name"
			)
		) if txns else set()
		live = [row for row in (self.write_offs or []) if row.transaction not in voided]
		original = sum((_dec(row.amount_original) for row in live), Decimal(0))
		birr = sum((_dec(row.amount_birr) for row in live), Decimal(0))
		return original, birr

	def _apply_settlement_math(self):
		"""Single reconciled money model (2026-09-06: currency-native invoicing, audit N-1 follow-up;
		multi-write-off, 2026-09-06). Every batch is single-currency (items are grouped by currency
		at batch creation -- see finance_engine.create_batch_request), so the agency-facing numbers
		-- total, paid, write-off, balance due -- are all tracked in that ORIGINAL currency. That's
		what drives the invoice and the settlement status below. The parallel *_birr fields are
		recomputed alongside for internal income/expense accounting only; they never drive status.

		  obligation  = sum of non-Released item amounts (in the batch's currency)
		  accounted   = paid-per-item + sum(write-offs)
		  balance_due = obligation - accounted

		2026-09-07: advance_amount_original is NOT part of "accounted" -- an advance is a loan the
		agency requests ahead of time, not a payment against this batch's own obligation, so it no
		longer offsets balance_due or counts toward Settled/Partially Settled. (2026-09-12:
		record_batch_advance, the endpoint that used to set this field, was removed as unused --
		the field itself stays, matching requested_advance_amount's own product decision that
		Advance is just a number on the invoice for now, nothing more.)

		The settlement mechanisms that DO feed this one balance (per-item Paid marks and one-or-more
		write-offs) can't over-credit each other. Settled once accounted covers the obligation; any
		partial coverage on an open batch -> Partially Settled. Never downgrades an already-Settled
		batch."""
		items = self.items or []
		# Released items were carried into a later batch -- no longer this batch's obligation.
		total_original = total_birr = Decimal(0)
		for row in items:
			if row.status == "Released":
				continue
			o, b = self._txn_amounts(row.transaction)
			total_original += o
			total_birr += b
		self.total_amount_original = total_original
		self.total_amount_birr = total_birr

		write_off_original, write_off_birr = self.write_off_totals()
		self.write_off_total_original = write_off_original
		self.write_off_total_birr = write_off_birr
		paid_original, paid_birr = self.paid_from_items()
		self.paid_amount_original = paid_original
		self.paid_amount_birr = paid_birr

		accounted_original = paid_original + write_off_original
		self.balance_due_original = max(total_original - accounted_original, Decimal(0))

		# Birr mirror, for internal accounting only. Each write-off's Birr amount is fixed at the
		# moment it's booked.
		accounted_birr = paid_birr + write_off_birr
		self.balance_due_birr = max(total_birr - accounted_birr, Decimal(0))

		# Settled = nothing left owed on THIS batch, whether paid off, written off, or every item
		# released into a later batch (total_original 0 -- the CBR-00007 "stuck at Partially
		# Settled" case, 2026-09-11). Guarded on "has ever had items", so only a brand-new, empty
		# batch can't read as Settled. 2026-09-23: this used to be "status != Draft", but nothing
		# ever moves a batch out of Draft (Draft/Sent are no longer used -- an unpaid batch is just
		# open), so a Draft batch fully paid or written off in one step stuck at Partially Settled.
		if items and accounted_original >= total_original:
			self.status = "Settled"
			if not self.settled_on:
				self.settled_on = today()
		elif self.status in ("Draft", "Sent") and accounted_original > 0:
			self.status = "Partially Settled"
		elif self.status == "Settled":
			# Only reachable when something that was counted stopped counting -- a voided write-off
			# (QA P5-04). The invoice is owed again, so it must not keep reading Settled.
			self.status = "Partially Settled" if accounted_original > 0 else "Draft"
			self.settled_on = None


def _dec(value):
	"""A stored Currency value as an exact Decimal (via str, so a float like 0.1 stays 0.1)."""
	return Decimal(str(flt(value)))


def get_permission_query_conditions(user):
	"""Same wall as Applicant Transaction — a batch request is just as sensitive as the
	transactions it groups."""
	if not user:
		user = frappe.session.user
	if {"Finance Manager", "Admin"} & set(frappe.get_roles(user)):
		return ""
	return "1=0"
