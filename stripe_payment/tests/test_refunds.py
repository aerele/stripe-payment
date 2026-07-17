# Copyright (c) Frappe Technologies Pvt. Ltd. and contributors
# License: MIT. See LICENSE
#
# Unit tests for Stripe refund helpers (mocked Stripe).

from unittest.mock import MagicMock, patch

import frappe
from frappe.tests.utils import FrappeTestCase

from stripe_payment.gateway.refunds import refund_intent, refund_payment_entry


class TestStripeRefunds(FrappeTestCase):
	def test_refund_intent_full(self):
		settings = MagicMock()
		client = MagicMock()
		client.refunds.create.return_value = MagicMock(id="re_1", status="succeeded")
		with patch("stripe_payment.gateway.refunds.get_stripe_client", return_value=client):
			result = refund_intent(settings, "pi_abc")
		self.assertEqual(result["refund"], "re_1")
		self.assertEqual(result["status"], "succeeded")
		params = client.refunds.create.call_args[0][0]
		self.assertEqual(params["payment_intent"], "pi_abc")
		self.assertNotIn("amount", params)

	def test_refund_intent_partial_converts_minor_units(self):
		settings = MagicMock()
		client = MagicMock()
		client.payment_intents.retrieve.return_value = MagicMock(currency="usd")
		client.refunds.create.return_value = MagicMock(id="re_2", status="succeeded")
		with patch("stripe_payment.gateway.refunds.get_stripe_client", return_value=client):
			refund_intent(settings, "pi_abc", amount=12.5)
		params = client.refunds.create.call_args[0][0]
		self.assertEqual(params["amount"], 1250)

	def test_refund_payment_entry_requires_pi_field(self):
		meta = MagicMock()
		meta.has_field.return_value = False  # no stripe_settings column on this PE
		with (
			patch("stripe_payment.gateway.refunds.frappe.has_permission"),
			patch("stripe_payment.gateway.refunds.frappe.db.get_value", return_value=None),
			patch("stripe_payment.gateway.refunds.frappe.get_meta", return_value=meta),
			patch("stripe_payment.gateway.refunds.frappe.get_all", return_value=[]),
		):
			with self.assertRaises(frappe.ValidationError):
				refund_payment_entry("PE-1")

	def test_refund_payment_entry_permission_checked(self):
		with patch(
			"stripe_payment.gateway.refunds.frappe.has_permission",
			side_effect=frappe.PermissionError,
		):
			with self.assertRaises(frappe.PermissionError):
				refund_payment_entry("PE-1")

	def test_refund_payment_entry_resolves_settings_from_column(self):
		"""When stripe_settings is stamped on the PE, no Stripe API probe runs."""
		settings_doc = MagicMock(name="StripeSettingsDoc")
		client = MagicMock()
		client.refunds.create.return_value = MagicMock(id="re_3", status="succeeded")
		meta = MagicMock()
		# First has_field('stripe_settings') True for the PE; later True for the
		# stripe_payment_intent guard inside refund_intent's caller path.
		meta.has_field.side_effect = lambda field: field == "stripe_settings"

		def get_value(doctype, name, field):
			if field == "stripe_payment_intent":
				return "pi_abc"
			if field == "stripe_settings":
				return "GW-1"

		with (
			patch("stripe_payment.gateway.refunds.frappe.has_permission"),
			patch("stripe_payment.gateway.refunds.frappe.get_meta", return_value=meta),
			patch("stripe_payment.gateway.refunds.frappe.db.get_value", side_effect=get_value),
			patch("stripe_payment.gateway.refunds.frappe.get_doc", return_value=settings_doc),
			patch("stripe_payment.gateway.refunds.get_stripe_client", return_value=client),
		):
			result = refund_payment_entry("PE-1")
		self.assertEqual(result["refund"], "re_3")
		# Single get_doc for the Settings, no get_all probe.
		self.assertEqual(settings_doc.refund_intent.call_count, 0)

	def test_idempotency_key_stable(self):
		from stripe_payment.gateway.client import idempotency_key

		a = idempotency_key("refund", "pi_1", "full")
		b = idempotency_key("refund", "pi_1", "full")
		c = idempotency_key("refund", "pi_1", 10)
		self.assertEqual(a, b)
		self.assertNotEqual(a, c)
