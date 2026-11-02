# Copyright (c) Frappe Technologies Pvt. Ltd. and contributors
# License: MIT. See LICENSE
#
# Unit tests for Hosted Checkout finalize/claim helpers (no live Stripe calls).

from unittest.mock import MagicMock, patch

import frappe
from frappe.tests.utils import FrappeTestCase

from stripe_payment.gateway.checkout import create_checkout_session, finalize_checkout_session
from stripe_payment.gateway.payment_intents import claim_integration_request
from stripe_payment.gateway.references import ensure_payment_transaction


class TestHostedCheckout(FrappeTestCase):
	def test_existing_payment_transaction_is_not_submitted_recursively(self):
		settings = MagicMock()
		settings.name = "GW-1"
		settings.gateway_name = "GW-1"
		data = {
			"reference_doctype": "Order",
			"reference_docname": "ORDER-1",
			"amount": 100,
			"currency": "USD",
			"payment_transaction": "PAY-TXN-1",
		}
		client = MagicMock()
		client.checkout.sessions.create.return_value = frappe._dict(
			{"id": "cs_test_1", "url": "https://checkout.stripe.com/c/pay/cs_test_1"}
		)
		integration_request = MagicMock()
		integration_request.name = "IR-1"

		with (
			patch("stripe_payment.gateway.checkout.guard_payment_reference"),
			patch("stripe_payment.gateway.checkout.get_reference_amount", return_value=(100, "USD")),
			patch("stripe_payment.gateway.checkout.ensure_payment_transaction"),
			patch("stripe_payment.gateway.checkout.get_stripe_client", return_value=client),
			patch("stripe_payment.gateway.checkout.create_request_log", return_value=integration_request),
			patch("stripe_payment.gateway.checkout.resolve_stripe_customer", return_value=None),
			patch("stripe_payment.gateway.checkout.get_url", return_value="https://site.test/success"),
			patch("stripe_payment.gateway.checkout.sync_payment_transaction") as sync,
			patch("stripe_payment.gateway.checkout.frappe.get_doc") as get_doc,
		):
			url = create_checkout_session(settings, data)

		self.assertEqual(url, "https://checkout.stripe.com/c/pay/cs_test_1")
		self.assertIsNone(sync.call_args.args[1])
		get_doc.assert_not_called()

	def test_ensure_payment_transaction_uses_core_open_attempt_helper(self):
		settings = MagicMock()
		settings.name = "GW-1"
		settings.gateway_name = "GW-1"
		data = frappe._dict(
			{
				"reference_doctype": "Order",
				"reference_docname": "ORDER-1",
				"amount": 100,
				"currency": "USD",
			}
		)
		with (
			patch("stripe_payment.gateway.references.frappe.db.table_exists", return_value=True),
			patch(
				"stripe_payment.gateway.references.ensure_reference_transaction",
				return_value="PAY-TXN-1",
			) as ensure,
		):
			transaction = ensure_payment_transaction(settings, data)

		self.assertEqual(transaction, "PAY-TXN-1")
		self.assertEqual(data.payment_transaction, "PAY-TXN-1")
		ensure.assert_called_once_with("Order", "ORDER-1", "Stripe-GW-1:Stripe-GW-1")

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
				"payment_transaction": "PAY-TXN-1",
			},
		}

		client = MagicMock()
		client.checkout.sessions.retrieve.return_value = session

		with (
			patch("stripe_payment.gateway.checkout.get_stripe_client", return_value=client),
			patch("stripe_payment.gateway.checkout.frappe.db.exists", return_value=True),
			patch("stripe_payment.gateway.checkout.frappe.get_doc", return_value=ir),
			patch("stripe_payment.gateway.checkout.sync_payment_transaction") as sync,
		):
			result = finalize_checkout_session(settings, "cs_test_1")

		self.assertEqual(result["status"], "Completed")
		settings.finalize_request.assert_not_called()
		self.assertEqual(sync.call_args.kwargs["provider_transaction_id"], "pi_123")

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

	def test_get_payment_url_always_routes_to_hosted_checkout(self):
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
