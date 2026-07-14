# Copyright (c) Frappe Technologies Pvt. Ltd. and contributors
# License: MIT. See LICENSE
#
# Unit tests for Hosted Checkout finalize/claim helpers (no live Stripe calls).

from unittest.mock import MagicMock, patch

import frappe
from frappe.tests.utils import FrappeTestCase

from stripe_payment.gateway.checkout import finalize_checkout_session
from stripe_payment.gateway.payment_intents import claim_integration_request


class TestHostedCheckout(FrappeTestCase):
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

	def test_finalize_checkout_session_idempotent_when_completed(self):
		settings = MagicMock()
		settings.finalize_request.return_value = {
			"status": "Completed",
			"redirect_to": "payment-success",
		}

		ir = MagicMock()
		ir.status = "Completed"
		ir.name = "IR-DONE"

		session = {
			"id": "cs_test_1",
			"payment_status": "paid",
			"status": "complete",
			"payment_intent": "pi_123",
			"metadata": {
				"reference_doctype": "Payment Request",
				"reference_docname": "PR-001",
				"integration_request": "IR-DONE",
			},
		}

		client = MagicMock()
		client.checkout.sessions.retrieve.return_value = session

		with (
			patch("stripe_payment.gateway.checkout.get_stripe_client", return_value=client),
			patch("stripe_payment.gateway.checkout.frappe.db.exists", return_value=True),
			patch("stripe_payment.gateway.checkout.frappe.get_doc", return_value=ir),
		):
			result = finalize_checkout_session(settings, "cs_test_1")

		self.assertEqual(result["status"], "Completed")
		settings.finalize_request.assert_not_called()

	def test_finalize_checkout_session_pending_async(self):
		settings = MagicMock()
		ir = MagicMock()
		ir.status = "Queued"
		ir.name = "IR-PENDING"

		session = {
			"id": "cs_test_2",
			"payment_status": "unpaid",
			"status": "complete",
			"payment_intent": None,
			"metadata": {
				"reference_doctype": "Payment Request",
				"reference_docname": "PR-001",
				"integration_request": "IR-PENDING",
			},
		}

		client = MagicMock()
		client.checkout.sessions.retrieve.return_value = session

		with (
			patch("stripe_payment.gateway.checkout.get_stripe_client", return_value=client),
			patch("stripe_payment.gateway.checkout.frappe.db.exists", return_value=True),
			patch("stripe_payment.gateway.checkout.frappe.get_doc", return_value=ir),
		):
			result = finalize_checkout_session(settings, "cs_test_2")

		self.assertEqual(result["status"], "Pending")
		settings.finalize_request.assert_not_called()

	def test_get_payment_url_hosted_vs_embedded(self):
		from stripe_payment.gateway.checkout import get_payment_url

		settings = MagicMock()
		settings.checkout_mode = "Hosted Checkout"
		settings.name = "Test Gateway"

		with patch(
			"stripe_payment.gateway.checkout.create_checkout_session",
			return_value="https://checkout.stripe.com/x",
		) as create:
			url = get_payment_url(settings, amount=10, currency="USD")
			self.assertEqual(url, "https://checkout.stripe.com/x")
			create.assert_called_once()

		settings.checkout_mode = "Embedded Elements"
		with patch("stripe_payment.gateway.checkout.get_url", side_effect=lambda p: f"http://site{p[1:]}"):
			url = get_payment_url(settings, amount=10, currency="USD")
			self.assertIn("stripe_checkout", url)
