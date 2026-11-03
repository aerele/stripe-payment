# Copyright (c) 2018, Frappe Technologies and Contributors
# License: MIT. See LICENSE
#
# Unit tests for the Stripe Settings controller (validation + webhook helpers).

from unittest.mock import MagicMock, patch

import frappe
from frappe.tests.utils import FrappeTestCase
from frappe.utils import get_url

from stripe_payment.gateway.webhooks import WEBHOOK_SECRET_CACHE_KEY
from stripe_payment.stripe.doctype.stripe_settings.stripe_settings import (
	clear_webhook_secret_cache,
)


class TestStripeSettings(FrappeTestCase):
	def test_validate_transaction_currency_rejects_unsupported(self):
		settings = frappe.new_doc("Stripe Settings")
		settings.supported_currencies = ["USD", "INR"]
		with self.assertRaises(frappe.ValidationError):
			settings.validate_transaction_currency("JPY")

	def test_validate_transaction_currency_accepts_supported(self):
		settings = frappe.new_doc("Stripe Settings")
		settings.supported_currencies = ["USD"]
		# Supported currency must not raise.
		settings.validate_transaction_currency("USD")

	def test_validate_minimum_transaction_amount_enforced(self):
		settings = frappe.new_doc("Stripe Settings")
		settings.currency_wise_minimum_charge_amount = {"USD": 5.0}
		with self.assertRaises(frappe.ValidationError):
			settings.validate_minimum_transaction_amount("USD", 1.0)
		# At/above the floor is fine, and unknown currencies are unconstrained.
		settings.validate_minimum_transaction_amount("USD", 5.0)
		settings.validate_minimum_transaction_amount("EUR", 0.1)

	def test_set_webhook_endpoint_writes_when_changed(self):
		settings = frappe.new_doc("Stripe Settings")
		settings.webhook_endpoint = None
		with patch.object(settings, "db_set") as db_set:
			settings.set_webhook_endpoint()
		db_set.assert_called_once()
		fieldname, value = db_set.call_args[0][0], db_set.call_args[0][1]
		self.assertEqual(fieldname, "webhook_endpoint")
		self.assertIn("stripe_payment.stripe.doctype.stripe_settings.webhooks", value)

	def test_set_webhook_endpoint_noop_when_unchanged(self):
		settings = frappe.new_doc("Stripe Settings")
		settings.webhook_endpoint = get_url(
			"/api/method/stripe_payment.stripe.doctype.stripe_settings.webhooks"
		)
		with patch.object(settings, "db_set") as db_set:
			settings.set_webhook_endpoint()
		db_set.assert_not_called()

	def test_clear_webhook_secret_cache(self):
		frappe.cache().set_value(WEBHOOK_SECRET_CACHE_KEY, [("GW", "whsec_test")])
		clear_webhook_secret_cache()
		self.assertIsNone(frappe.cache().get_value(WEBHOOK_SECRET_CACHE_KEY))

	def test_on_update_does_not_call_validate_credentials(self):
		"""Save path must be free of network I/O."""
		settings = frappe.new_doc("Stripe Settings")
		settings.gateway_name = "Test"
		with (
			patch("stripe_payment.stripe.doctype.stripe_settings.stripe_settings.create_payment_gateway"),
			patch("stripe_payment.stripe.doctype.stripe_settings.stripe_settings.call_hook_method"),
			patch.object(settings, "set_webhook_endpoint"),
		):
			settings.on_update()

	def test_refund_passes_idempotency_key_as_request_option(self):
		settings = frappe.new_doc("Stripe Settings")
		settings.name = "GW-1"
		client = MagicMock()
		client.refunds.create.return_value = {"id": "re_test_1"}

		with (
			patch.object(frappe.db, "table_exists", return_value=True),
			patch.object(
				frappe.db,
				"get_value",
				return_value=frappe._dict(provider_transaction_id="pi_test_1", currency="USD"),
			),
			patch(
				"stripe_payment.stripe.doctype.stripe_settings.stripe_settings.get_stripe_client",
				return_value=client,
			),
		):
			refund_id = settings.refund_transaction("TXN-1", 12.34, idempotency_key="pc-refund-TXN-1-1-12.34")

		self.assertEqual(refund_id, "re_test_1")
		client.refunds.create.assert_called_once_with(
			{"payment_intent": "pi_test_1", "amount": 1234},
			{"idempotency_key": "pc-refund-TXN-1-1-12.34"},
		)

	def test_authorize_reference_skips_when_ledger_already_authorized(self):
		settings = frappe.new_doc("Stripe Settings")
		settings.data = frappe._dict(
			{
				"reference_doctype": "Order",
				"reference_docname": "ORDER-1",
				"payment_transaction": "PAY-TXN-1",
			}
		)
		settings.flags.status_changed_to = "Completed"

		with (
			patch.object(frappe.db, "table_exists", return_value=True),
			patch.object(frappe.db, "get_value", return_value=1),
			patch(
				"stripe_payment.stripe.doctype.stripe_settings.stripe_settings.authorize_reference"
			) as authorize,
		):
			result = settings.authorize_reference()

		self.assertIsNone(result)
		authorize.assert_not_called()
