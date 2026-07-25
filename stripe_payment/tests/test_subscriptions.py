# Copyright (c) Frappe Technologies Pvt. Ltd. and contributors
# License: MIT. See LICENSE
#
# Unit tests for subscription helpers (mocked Stripe / DB).

from unittest.mock import MagicMock, patch

import frappe
from frappe.tests.utils import FrappeTestCase

from stripe_payment.gateway.subscriptions import (
	find_erpnext_subscription,
	get_subscription_line_items,
	link_stripe_subscription,
)


class TestStripeSubscriptions(FrappeTestCase):
	def test_get_subscription_line_items_requires_synced_price(self):
		with (
			patch(
				"stripe_payment.gateway.subscriptions.get_subscription_plan_details",
				return_value=[frappe._dict(plan="PLAN-1", qty=2)],
			),
			patch(
				"stripe_payment.gateway.subscriptions.frappe.get_all",
				return_value=[["PLAN-1", None]],
			),
		):
			with self.assertRaises(frappe.ValidationError):
				get_subscription_line_items("Payment Request", "PR-1")

	def test_get_subscription_line_items_ok(self):
		with (
			patch(
				"stripe_payment.gateway.subscriptions.get_subscription_plan_details",
				return_value=[frappe._dict(plan="PLAN-1", qty=2)],
			),
			patch(
				"stripe_payment.gateway.subscriptions.frappe.get_all",
				return_value=[["PLAN-1", "price_123"]],
			),
		):
			items = get_subscription_line_items("Payment Request", "PR-1")
		self.assertEqual(items, [{"price": "price_123", "quantity": 2}])

	def test_find_erpnext_subscription_matches_plan_subset(self):
		with (
			patch("stripe_payment.gateway.subscriptions.frappe.db.exists", return_value=True),
			patch(
				"stripe_payment.gateway.subscriptions.frappe.get_all",
				side_effect=[
					["SUB-1"],  # candidates
					["PLAN-1", "PLAN-2"],  # plans on SUB-1
				],
			),
		):
			name = find_erpnext_subscription("CUST-1", ["PLAN-1"])
		self.assertEqual(name, "SUB-1")

	def test_link_stripe_subscription_sets_fields_when_present(self):
		with (
			patch("stripe_payment.gateway.subscriptions.frappe.db.has_column", return_value=True),
			patch("stripe_payment.gateway.subscriptions.frappe.db.set_value") as set_value,
		):
			link_stripe_subscription("SUB-1", "sub_abc", "cus_xyz")
		set_value.assert_called_once()
		args = set_value.call_args[0]
		self.assertEqual(args[0], "Subscription")
		self.assertEqual(args[1], "SUB-1")
		self.assertEqual(args[2]["stripe_subscription_id"], "sub_abc")
		self.assertEqual(args[2]["stripe_customer_id"], "cus_xyz")

	def test_get_payment_url_always_uses_hosted_checkout(self):
		from stripe_payment.gateway.checkout import get_payment_url

		settings = MagicMock()
		settings.name = "GW"

		with patch(
			"stripe_payment.gateway.checkout.create_checkout_session",
			return_value="https://checkout.stripe.com/pay",
		) as create:
			url = get_payment_url(settings, reference_doctype="Payment Request", reference_docname="PR-1")
		self.assertEqual(url, "https://checkout.stripe.com/pay")
		create.assert_called_once()
