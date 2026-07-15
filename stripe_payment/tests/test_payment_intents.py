# Copyright (c) Frappe Technologies Pvt. Ltd. and contributors
# License: MIT. See LICENSE
#
# Unit tests for PaymentIntent ownership / claim helpers (no live Stripe calls).

from unittest.mock import MagicMock, patch

import frappe
from frappe.tests.utils import FrappeTestCase

from stripe_payment.gateway.payment_intents import (
	assert_intent_matches_reference,
	claim_integration_request,
	finalize_payment_intent,
)
from stripe_payment.gateway.references import assert_reference_payable


class TestPaymentIntents(FrappeTestCase):
	def test_assert_intent_matches_reference_accepts_match(self):
		settings = MagicMock()
		settings.data = frappe._dict({"reference_doctype": "Payment Request", "reference_docname": "PR-001"})
		intent = {
			"metadata": {
				"reference_doctype": "Payment Request",
				"reference_docname": "PR-001",
			}
		}
		assert_intent_matches_reference(settings, intent)

	def test_assert_intent_matches_reference_rejects_mismatch(self):
		settings = MagicMock()
		settings.data = frappe._dict({"reference_doctype": "Payment Request", "reference_docname": "PR-001"})
		intent = {
			"metadata": {
				"reference_doctype": "Payment Request",
				"reference_docname": "PR-OTHER",
			}
		}
		with self.assertRaises(frappe.PermissionError):
			assert_intent_matches_reference(settings, intent)

	def test_claim_integration_request_first_wins(self):
		settings = MagicMock()
		settings.integration_request = MagicMock()
		settings.integration_request.name = "IR-TEST"

		with patch("stripe_payment.gateway.payment_intents.frappe.db.get_value", return_value="Queued"):
			self.assertTrue(claim_integration_request(settings))
			settings.integration_request.db_set.assert_called_with(
				"status", "Completed", update_modified=False
			)

		with patch("stripe_payment.gateway.payment_intents.frappe.db.get_value", return_value="Completed"):
			self.assertFalse(claim_integration_request(settings))

	def test_finalize_payment_intent_idempotent_when_already_completed(self):
		settings = MagicMock()
		settings.finalize_request.return_value = {"status": "Completed", "redirect_to": "payment-success"}

		ir = MagicMock()
		ir.status = "Completed"
		ir.name = "IR-DONE"

		intent = {
			"id": "pi_123",
			"metadata": {
				"reference_doctype": "Payment Request",
				"reference_docname": "PR-001",
				"integration_request": "IR-DONE",
			},
		}

		with (
			patch("stripe_payment.gateway.payment_intents.frappe.db.exists", return_value=True),
			patch("stripe_payment.gateway.payment_intents.frappe.get_doc", return_value=ir),
		):
			result = finalize_payment_intent(settings, intent)

		self.assertEqual(result["status"], "Completed")
		settings.finalize_request.assert_not_called()

	def test_assert_reference_payable_blocks_cancelled(self):
		row = frappe._dict(docstatus=1, status="Cancelled")
		with (
			patch("stripe_payment.gateway.references.frappe.db.exists", return_value=True),
			patch("stripe_payment.gateway.references.frappe.get_meta") as meta,
			patch("stripe_payment.gateway.references.frappe.db.get_value", return_value=row),
		):
			meta.return_value.has_field.return_value = True
			with self.assertRaises(frappe.ValidationError) as ctx:
				assert_reference_payable("Payment Request", "PR-CANCELLED")
		self.assertIn("cancelled", str(ctx.exception).lower())

	def test_assert_reference_payable_blocks_paid(self):
		row = frappe._dict(docstatus=1, status="Paid")
		with (
			patch("stripe_payment.gateway.references.frappe.db.exists", return_value=True),
			patch("stripe_payment.gateway.references.frappe.get_meta") as meta,
			patch("stripe_payment.gateway.references.frappe.db.get_value", return_value=row),
		):
			meta.return_value.has_field.return_value = True
			with self.assertRaises(frappe.ValidationError) as ctx:
				assert_reference_payable("Payment Request", "PR-PAID")
		self.assertIn("already paid", str(ctx.exception).lower())

	def test_assert_reference_payable_allows_open_request(self):
		row = frappe._dict(docstatus=1, status="Requested")
		with (
			patch("stripe_payment.gateway.references.frappe.db.exists", return_value=True),
			patch("stripe_payment.gateway.references.frappe.get_meta") as meta,
			patch("stripe_payment.gateway.references.frappe.db.get_value", return_value=row),
		):
			meta.return_value.has_field.return_value = True
			assert_reference_payable("Payment Request", "PR-OPEN")
