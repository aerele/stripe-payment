# Copyright (c) Frappe Technologies Pvt. Ltd. and contributors
# License: MIT. See LICENSE
#
# Unit tests for UPI payment-method parameter selection (no live Stripe).

from unittest.mock import MagicMock

from frappe.tests.utils import FrappeTestCase

from stripe_payment.gateway.payment_methods import (
	_upi_available,
	automatic_payment_methods_for,
	checkout_payment_method_types,
)


def _settings(enable_upi: int):
	"""Build a fake Stripe Settings doc with the given enable_upi flag."""
	settings = MagicMock()
	settings.enable_upi = enable_upi
	return settings


class TestStripeUPI(FrappeTestCase):
	def test_upi_available_requires_inr_and_flag(self):
		"""_upi_available is true only when the flag is on and currency is INR."""
		self.assertTrue(_upi_available(_settings(1), "INR"))
		# Flag off or non-INR currency → never available (covers both axes).
		self.assertFalse(_upi_available(_settings(0), "INR"))
		self.assertFalse(_upi_available(_settings(1), "USD"))
		# Currency match is case-insensitive (upper-cased before compare).
		self.assertTrue(_upi_available(_settings(1), "inr"))
		# Currency normalisation: None / empty treated as non-INR.
		self.assertFalse(_upi_available(_settings(1), None))
		self.assertFalse(_upi_available(_settings(1), ""))

	def test_automatic_payment_methods_allows_redirects_for_upi(self):
		self.assertEqual(
			automatic_payment_methods_for(_settings(1), "INR"),
			{"enabled": True, "allow_redirects": "always"},
		)

	def test_automatic_payment_methods_blocks_redirects_without_upi(self):
		# Flag on but non-INR — still card path.
		self.assertEqual(
			automatic_payment_methods_for(_settings(1), "USD"),
			{"enabled": True, "allow_redirects": "never"},
		)
		# INR but flag off — card path.
		self.assertEqual(
			automatic_payment_methods_for(_settings(0), "INR"),
			{"enabled": True, "allow_redirects": "never"},
		)

	def test_checkout_payment_method_types_explicit_when_upi(self):
		self.assertEqual(checkout_payment_method_types(_settings(1), "INR"), ["card", "upi"])

	def test_checkout_payment_method_types_none_when_upi_not_applicable(self):
		# None means "let Stripe Dashboard decide" — the default card path.
		self.assertIsNone(checkout_payment_method_types(_settings(1), "USD"))
		self.assertIsNone(checkout_payment_method_types(_settings(0), "INR"))
