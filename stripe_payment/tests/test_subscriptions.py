# Copyright (c) Frappe Technologies Pvt. Ltd. and contributors
# License: MIT. See LICENSE
#
# Unit tests for subscription helpers (mocked Stripe / DB).

from unittest.mock import MagicMock, patch

import frappe
from frappe.tests.utils import FrappeTestCase

from stripe_payment.gateway.subscriptions import (
	apply_charge_now_defer_first_cycle,
	find_erpnext_subscription,
	get_first_cycle_trial_end,
	get_subscription_line_items,
	is_charge_now_defer_first_cycle,
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

	def test_get_payment_url_forces_hosted_for_subscription(self):
		from stripe_payment.gateway.checkout import get_payment_url

		settings = MagicMock()
		settings.checkout_mode = "Embedded Elements"
		settings.name = "GW"

		with (
			patch("stripe_payment.gateway.checkout.is_subscription_reference", return_value=True),
			patch(
				"stripe_payment.gateway.checkout.create_checkout_session",
				return_value="https://checkout.stripe.com/sub",
			) as create,
		):
			url = get_payment_url(settings, reference_doctype="Payment Request", reference_docname="PR-1")
		self.assertEqual(url, "https://checkout.stripe.com/sub")
		create.assert_called_once()

	def test_is_charge_now_defer_first_cycle(self):
		settings = frappe._dict(subscription_billing_model="Charge Now + Defer First Cycle")
		self.assertTrue(is_charge_now_defer_first_cycle(settings))
		self.assertFalse(
			is_charge_now_defer_first_cycle(frappe._dict(subscription_billing_model="Bill From Cycle One"))
		)

	def test_get_first_cycle_trial_end_month(self):
		def _get_value(doctype, name, fields=None, as_dict=False, **kwargs):
			if doctype == "Subscription Plan":
				return frappe._dict(billing_interval="Month", billing_interval_count=1)
			return None

		with (
			patch(
				"stripe_payment.gateway.subscriptions.get_subscription_plan_details",
				return_value=[frappe._dict(plan="PLAN-1", qty=1)],
			),
			patch("stripe_payment.gateway.subscriptions.frappe.db.get_value", side_effect=_get_value),
			patch(
				"stripe_payment.gateway.subscriptions.now_datetime",
				return_value=frappe.utils.get_datetime("2026-01-15 10:00:00"),
			),
		):
			ts = get_first_cycle_trial_end("Payment Request", "PR-1")
		self.assertIsInstance(ts, int)
		# ~1 month after 2026-01-15
		self.assertGreater(ts, int(frappe.utils.get_datetime("2026-02-01").timestamp()))

	def test_apply_charge_now_defer_first_cycle_checkout_params(self):
		params = {
			"mode": "subscription",
			"line_items": [{"price": "price_recurring", "quantity": 1}],
			"subscription_data": {"metadata": {"k": "v"}},
		}
		with (
			patch(
				"stripe_payment.gateway.subscriptions.get_first_cycle_trial_end",
				return_value=1_800_000_000,
			),
			patch(
				"stripe_payment.gateway.subscriptions.to_minor_units",
				return_value=15000,
			),
		):
			apply_charge_now_defer_first_cycle(
				params,
				amount=150,
				currency="INR",
				reference_doctype="Payment Request",
				reference_docname="PR-1",
				description="First cycle",
			)
		self.assertEqual(len(params["line_items"]), 2)
		one_time = params["line_items"][1]
		self.assertEqual(one_time["price_data"]["unit_amount"], 15000)
		self.assertEqual(one_time["price_data"]["currency"], "inr")
		self.assertEqual(params["subscription_data"]["trial_end"], 1_800_000_000)
		self.assertEqual(params["subscription_data"]["metadata"]["k"], "v")
