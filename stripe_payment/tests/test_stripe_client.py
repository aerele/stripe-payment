# Copyright (c) Frappe Technologies Pvt. Ltd. and contributors
# License: MIT. See LICENSE
#
# Unit tests for shared Stripe client primitives (no live Stripe calls).

import unittest
from unittest.mock import MagicMock, patch

from stripe_payment.gateway.client import (
	from_minor_units,
	get_stripe_client,
	idempotency_key,
	to_minor_units,
)
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


class TestStripeClientCache(unittest.TestCase):
	"""I29: the decrypted API key is cached per settings name so the DB read +
	AES decrypt runs at most once per request/job."""

	def test_get_stripe_client_caches_api_key_across_calls(self):
		settings = MagicMock(name="SS")
		settings.name = "GW-1"

		cache = {}

		def hget(key, name):
			return cache.get(name)

		def hset(key, name, value):
			cache[name] = value

		frappe_cache = MagicMock()
		frappe_cache.hget.side_effect = hget
		frappe_cache.hset.side_effect = hset

		with (
			patch("stripe_payment.gateway.client.frappe.cache", return_value=frappe_cache),
			patch(
				"frappe.utils.password.get_decrypted_password", return_value="sk_test_cached"
			) as get_password,
		):
			get_stripe_client(settings)
			get_stripe_client(settings)
		# The decrypted-password read (DB read + decrypt) runs exactly once, not twice.
		self.assertEqual(get_password.call_count, 1)

	def test_get_stripe_client_resolves_string_name(self):
		cache = {}

		def hget(key, name):
			return cache.get(name)

		def hset(key, name, value):
			cache[name] = value

		frappe_cache = MagicMock()
		frappe_cache.hget.side_effect = hget
		frappe_cache.hset.side_effect = hset

		with (
			patch("stripe_payment.gateway.client.frappe.cache", return_value=frappe_cache),
			patch("frappe.utils.password.get_decrypted_password", return_value="sk_test_str") as get_password,
		):
			get_stripe_client("GW-2")
		# A bare docname resolves the key directly from the password store, no get_doc.
		get_password.assert_called_once_with("Stripe Settings", "GW-2", "secret_key", raise_exception=False)
