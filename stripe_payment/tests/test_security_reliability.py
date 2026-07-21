# Copyright (c) Frappe Technologies Pvt. Ltd. and contributors
# License: MIT. See LICENSE
#
# Cross-cutting security / reliability regression tests (mocked Stripe; no live API).

from unittest.mock import MagicMock, patch

import frappe
from frappe.tests.utils import FrappeTestCase

from stripe_payment.gateway.payment_intents import (
	assert_intent_matches_reference,
	claim_integration_request,
	enable_setup_future_usage,
	finalize_payment_intent,
)


class TestSecurityReliability(FrappeTestCase):
	"""Regression coverage for amount/ownership/claim/consent guards."""

	# --- PaymentIntent ownership ---

	def test_intent_reference_mismatch_is_permission_error(self):
		"""Compares the intent's metadata reference pair against the request's reference pair and throws PermissionError when they differ."""
		settings = MagicMock()
		settings.data = frappe._dict(
			{"reference_doctype": "Payment Request", "reference_docname": "PR-EXPENSIVE"}
		)
		intent = {
			"metadata": {
				"reference_doctype": "Payment Request",
				"reference_docname": "PR-CHEAP",
			}
		}
		with self.assertRaises(frappe.PermissionError):
			assert_intent_matches_reference(settings, intent)

	def test_intent_reference_match_ok(self):
		"""Returns without raising when the intent's metadata reference pair equals the request's reference pair."""
		settings = MagicMock()
		settings.data = frappe._dict({"reference_doctype": "Payment Request", "reference_docname": "PR-1"})
		intent = {
			"metadata": {
				"reference_doctype": "Payment Request",
				"reference_docname": "PR-1",
			}
		}
		assert_intent_matches_reference(settings, intent)

	# --- Integration Request claim race ---

	def test_claim_first_caller_wins(self):
		"""Reads the Integration Request status with FOR UPDATE; returns True and flips it to Completed when Queued, returns False when already Completed."""
		settings = MagicMock()
		settings.integration_request = MagicMock()
		settings.integration_request.name = "IR-1"

		with patch("stripe_payment.gateway.payment_intents.frappe.db.get_value", return_value="Queued"):
			self.assertTrue(claim_integration_request(settings))

		with patch("stripe_payment.gateway.payment_intents.frappe.db.get_value", return_value="Completed"):
			self.assertFalse(claim_integration_request(settings))

	def test_finalize_skips_when_already_completed(self):
		"""Short-circuits with status Completed and never calls finalize_request when the Integration Request is already Completed."""
		settings = MagicMock()
		ir = MagicMock()
		ir.status = "Completed"
		ir.name = "IR-DONE"
		intent = {
			"id": "pi_1",
			"metadata": {
				"reference_doctype": "Payment Request",
				"reference_docname": "PR-1",
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

	def test_finalize_claim_loss_does_not_double_settle(self):
		"""Returns status Completed without invoking finalize_request when claim_integration_request returns False."""
		settings = MagicMock()
		ir = MagicMock()
		ir.status = "Queued"
		ir.name = "IR-RACE"
		intent = {
			"id": "pi_1",
			"metadata": {
				"reference_doctype": "Payment Request",
				"reference_docname": "PR-1",
				"integration_request": "IR-RACE",
			},
		}
		with (
			patch("stripe_payment.gateway.payment_intents.frappe.db.exists", return_value=True),
			patch("stripe_payment.gateway.payment_intents.frappe.get_doc", return_value=ir),
			patch("stripe_payment.gateway.payment_intents.claim_integration_request", return_value=False),
		):
			result = finalize_payment_intent(settings, intent)
		self.assertEqual(result["status"], "Completed")
		settings.finalize_request.assert_not_called()

	# --- Save-card consent ownership ---

	def test_setup_future_usage_rejects_wrong_client_secret(self):
		"""Retrieves the intent, compares its stored client_secret to the supplied one via hmac.compare_digest, and throws PermissionError on mismatch."""
		settings = MagicMock()
		client = MagicMock()
		intent = MagicMock()
		intent.client_secret = "real_secret"
		intent.status = "requires_payment_method"
		intent.metadata = {
			"reference_doctype": "Payment Request",
			"reference_docname": "PR-1",
		}
		intent.get = lambda k, d=None: (
			"cus_1" if k == "customer" else intent.metadata if k == "metadata" else d
		)
		client.payment_intents.retrieve.return_value = intent

		with patch("stripe_payment.gateway.payment_intents.get_stripe_client", return_value=client):
			with self.assertRaises(frappe.PermissionError):
				enable_setup_future_usage(settings, "pi_1", "forged_secret", "Payment Request", "PR-1")

	# --- Reference amount authority (payment_core) ---

	def test_get_reference_amount_prefers_grand_total(self):
		"""Picks grand_total over amount via the meta has_field check, then reads that column and currency from the reference row."""
		from payment_core.utils import get_reference_amount

		meta = MagicMock()
		meta.has_field = lambda f: f in ("grand_total", "currency")
		with (
			patch("payment_core.utils.frappe.get_meta", return_value=meta),
			patch(
				"payment_core.utils.frappe.db.get_value",
				return_value=frappe._dict(grand_total=99.5, currency="USD"),
			),
		):
			amount, currency = get_reference_amount("Payment Request", "PR-1")
		self.assertEqual(amount, 99.5)
		self.assertEqual(currency, "USD")

	def test_guard_payment_reference_rejects_missing(self):
		"""Runs frappe.db.exists on the reference doctype+docname and throws PermissionError when no row is found."""
		from payment_core.utils import guard_payment_reference

		with patch("payment_core.utils.frappe.db.exists", return_value=False):
			with self.assertRaises(frappe.PermissionError):
				guard_payment_reference("Payment Request", "NOPE")

	# --- Idempotency stability ---

	def test_idempotency_keys_stable_for_retries(self):
		"""Hashes the joined parts with sha256 so identical inputs yield identical keys and any differing input diverges."""
		from stripe_payment.gateway.client import idempotency_key

		self.assertEqual(
			idempotency_key("refund", "pi_1", "full"),
			idempotency_key("refund", "pi_1", "full"),
		)
		self.assertEqual(
			idempotency_key("pi", "PR-1", 100, "pm_x"),
			idempotency_key("pi", "PR-1", 100, "pm_x"),
		)
		self.assertNotEqual(
			idempotency_key("pi", "PR-1", 100, "pm_x"),
			idempotency_key("pi", "PR-1", 100, "pm_y"),
		)
