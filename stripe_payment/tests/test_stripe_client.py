# Copyright (c) Frappe Technologies Pvt. Ltd. and contributors
# License: MIT. See LICENSE
#
# Unit tests for shared Stripe client primitives (no live Stripe calls).

import unittest

from stripe_payment.gateway.client import from_minor_units, idempotency_key, to_minor_units
from stripe_payment.gateway.constants import STRIPE_API_VERSION, ZERO_DECIMAL_CURRENCIES


class TestStripeClientPrimitives(unittest.TestCase):
	def test_to_minor_units_decimal_currency(self):
		self.assertEqual(to_minor_units(12.50, "USD"), 1250)
		self.assertEqual(to_minor_units("10", "EUR"), 1000)

	def test_to_minor_units_zero_decimal_currency(self):
		self.assertEqual(to_minor_units(1000, "JPY"), 1000)
		self.assertEqual(to_minor_units(99.6, "JPY"), 100)

	def test_from_minor_units_round_trip(self):
		self.assertEqual(from_minor_units(1250, "USD"), 12.5)
		self.assertEqual(from_minor_units(1000, "JPY"), 1000)

	def test_zero_decimal_set_includes_jpy(self):
		self.assertIn("JPY", ZERO_DECIMAL_CURRENCIES)
		self.assertNotIn("USD", ZERO_DECIMAL_CURRENCIES)

	def test_idempotency_key_stable(self):
		a = idempotency_key("pi", "PR-0001", 100)
		b = idempotency_key("pi", "PR-0001", 100)
		c = idempotency_key("pi", "PR-0001", 101)
		self.assertEqual(a, b)
		self.assertNotEqual(a, c)
		self.assertEqual(len(a), 64)

	def test_api_version_pinned(self):
		self.assertTrue(STRIPE_API_VERSION)
