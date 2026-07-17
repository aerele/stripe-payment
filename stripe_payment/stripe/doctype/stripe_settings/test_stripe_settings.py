# Copyright (c) 2018, Frappe Technologies and Contributors
# License: MIT. See LICENSE
#
# Unit tests for the Stripe Settings controller (validation + webhook helpers).

from unittest.mock import patch

import frappe
from frappe.tests.utils import FrappeTestCase

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
		from frappe.utils import get_url

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

	def test_test_connection_invokes_credential_check(self):
		"""test_connection is the on-demand credential validator (desk button)."""
		settings = frappe.new_doc("Stripe Settings")
		settings.publishable_key = "pk_test_x"
		with (
			patch.object(settings, "secret_key", "sk_test_x", create=True),
			patch.object(settings, "get_password", return_value="sk_test_x"),
			patch("stripe_payment.stripe.doctype.stripe_settings.stripe_settings.make_get_request") as m,
		):
			settings.test_connection()
		self.assertTrue(m.called)
		self.assertIn("payment_intents", m.call_args.kwargs.get("url", ""))

	def test_on_update_does_not_call_validate_credentials(self):
		"""Save path must be free of network I/O (credential check is on-demand)."""
		settings = frappe.new_doc("Stripe Settings")
		settings.gateway_name = "Test"
		with (
			patch("stripe_payment.stripe.doctype.stripe_settings.stripe_settings.create_payment_gateway"),
			patch("stripe_payment.stripe.doctype.stripe_settings.stripe_settings.call_hook_method"),
			patch.object(settings, "set_webhook_endpoint"),
			patch.object(settings, "validate_stripe_credentails") as m,
		):
			settings.on_update()
		self.assertFalse(m.called)
