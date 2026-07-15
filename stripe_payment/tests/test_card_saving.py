# Copyright (c) Frappe Technologies Pvt. Ltd. and contributors
# License: MIT. See LICENSE
#
# Unit tests for save-card SetupIntent / consent helpers (mocked Stripe).

from unittest.mock import MagicMock, patch

import frappe
from frappe.tests.utils import FrappeTestCase

from stripe_payment.gateway.payment_intents import enable_setup_future_usage


class TestCardSaving(FrappeTestCase):
	def test_enable_setup_future_usage_requires_matching_secret(self):
		settings = MagicMock()
		client = MagicMock()
		intent = MagicMock()
		intent.client_secret = "secret_ok"
		intent.status = "requires_payment_method"
		intent.get = lambda k, d=None: {
			"customer": "cus_1",
			"metadata": {
				"reference_doctype": "Payment Request",
				"reference_docname": "PR-1",
			},
		}.get(k, d)
		intent.metadata = {
			"reference_doctype": "Payment Request",
			"reference_docname": "PR-1",
		}
		# Stripe objects use both attribute and dict-style access in our code
		intent.__getitem__ = lambda self, k: intent.metadata if k == "metadata" else None
		client.payment_intents.retrieve.return_value = intent

		with patch("stripe_payment.gateway.payment_intents.get_stripe_client", return_value=client):
			with self.assertRaises(frappe.PermissionError):
				enable_setup_future_usage(settings, "pi_1", "wrong_secret", "Payment Request", "PR-1")

	def test_enable_setup_future_usage_updates_when_valid(self):
		settings = MagicMock()
		client = MagicMock()
		intent = MagicMock()
		intent.client_secret = "secret_ok"
		intent.status = "requires_payment_method"
		intent.metadata = {
			"reference_doctype": "Payment Request",
			"reference_docname": "PR-1",
		}

		def _get(key, default=None):
			if key == "customer":
				return "cus_1"
			if key == "metadata":
				return intent.metadata
			return default

		intent.get = _get
		client.payment_intents.retrieve.return_value = intent

		with patch("stripe_payment.gateway.payment_intents.get_stripe_client", return_value=client):
			result = enable_setup_future_usage(settings, "pi_1", "secret_ok", "Payment Request", "PR-1")

		self.assertEqual(result, {"updated": True})
		client.payment_intents.update.assert_called_once_with("pi_1", {"setup_future_usage": "off_session"})

	def test_enable_setup_future_usage_skips_without_customer(self):
		settings = MagicMock()
		client = MagicMock()
		intent = MagicMock()
		intent.client_secret = "secret_ok"
		intent.status = "requires_payment_method"
		intent.metadata = {
			"reference_doctype": "Payment Request",
			"reference_docname": "PR-1",
		}
		intent.get = lambda k, d=None: intent.metadata if k == "metadata" else None
		client.payment_intents.retrieve.return_value = intent

		with patch("stripe_payment.gateway.payment_intents.get_stripe_client", return_value=client):
			result = enable_setup_future_usage(settings, "pi_1", "secret_ok", "Payment Request", "PR-1")

		self.assertEqual(result, {"updated": False})
		client.payment_intents.update.assert_not_called()
