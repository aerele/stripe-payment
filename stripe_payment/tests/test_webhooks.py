# Copyright (c) Frappe Technologies Pvt. Ltd. and contributors
# License: MIT. See LICENSE
#
# Unit tests for webhook verification / dedupe (mocked Stripe).

from unittest.mock import MagicMock, patch

import frappe
from frappe.tests.utils import FrappeTestCase

from stripe_payment.gateway.reconciliation import route_event
from stripe_payment.gateway.webhooks import construct_event, handle_event


class TestStripeWebhooks(FrappeTestCase):
	def test_construct_event_missing_signature(self):
		event, settings = construct_event(b"{}", None)
		self.assertIsNone(event)
		self.assertIsNone(settings)

	def test_construct_event_tries_secrets(self):
		import stripe

		fake_event = {"id": "evt_1", "type": "ping", "data": {"object": {}}}
		with (
			patch(
				"stripe_payment.gateway.webhooks.get_webhook_secrets",
				return_value=[("GW", "whsec_test")],
			),
			patch("stripe.Webhook.construct_event", return_value=fake_event) as construct,
			patch("stripe_payment.gateway.webhooks.frappe.get_doc", return_value=MagicMock(name="GW")),
		):
			event, _settings = construct_event(b"{}", "t=1,v1=sig")
		self.assertEqual(event["id"], "evt_1")
		construct.assert_called_once()

	def test_handle_event_duplicate(self):
		event = {"id": "evt_dup", "type": "payment_intent.succeeded", "data": {"object": {"id": "pi_1"}}}
		settings = MagicMock()
		with (
			patch("stripe_payment.gateway.webhooks.frappe.set_user"),
			patch(
				"stripe_payment.gateway.webhooks.frappe.db.get_value",
				return_value=frappe._dict(name="LOG-1", status="Processed"),
			),
		):
			result = handle_event(event, settings)
		self.assertEqual(result["status"], "duplicate")

	def test_route_event_ignored_unknown(self):
		event = {"type": "something.unknown", "data": {"object": {}}}
		result = route_event(event, MagicMock())
		self.assertEqual(result["status_label"], "Ignored")

	def test_route_event_one_off_without_reference_ignored(self):
		event = {
			"type": "payment_intent.succeeded",
			"data": {"object": {"id": "pi_1", "metadata": {}, "invoice": None}},
		}
		result = route_event(event, MagicMock())
		self.assertEqual(result["status_label"], "Ignored")
