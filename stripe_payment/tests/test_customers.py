# Copyright (c) Frappe Technologies Pvt. Ltd. and contributors
# License: MIT. See LICENSE
#
# Unit tests for Stripe customer resolution (mocked Stripe client).

from unittest.mock import MagicMock, patch

import frappe
from frappe.tests.utils import FrappeTestCase

from stripe_payment.gateway.customers import (
	get_or_create_customer,
	get_party_for_reference,
	resolve_stripe_customer,
)


class TestStripeCustomers(FrappeTestCase):
	def test_get_or_create_reuses_cached_id(self):
		client = MagicMock()
		client.customers.retrieve.return_value = {"id": "cus_cached", "deleted": False}

		with (
			patch("stripe_payment.gateway.customers.frappe.db.has_column", return_value=True),
			patch("stripe_payment.gateway.customers.frappe.db.get_value", return_value="cus_cached"),
		):
			cid = get_or_create_customer(client, customer="CUST-001", email="a@example.com")

		self.assertEqual(cid, "cus_cached")
		client.customers.create.assert_not_called()

	def test_get_or_create_recreates_when_stale(self):
		import stripe

		client = MagicMock()
		client.customers.retrieve.side_effect = stripe.error.InvalidRequestError(
			message="No such customer", param=None
		)
		client.customers.search.return_value = {"data": []}
		client.customers.create.return_value = MagicMock(id="cus_new")

		with (
			patch("stripe_payment.gateway.customers.frappe.db.has_column", return_value=True),
			patch("stripe_payment.gateway.customers.frappe.db.get_value", return_value="cus_stale"),
			patch("stripe_payment.gateway.customers.frappe.db.set_value") as set_value,
			patch("stripe_payment.gateway.customers.frappe.log_error"),
		):
			cid = get_or_create_customer(client, customer="CUST-001", email="a@example.com")

		self.assertEqual(cid, "cus_new")
		set_value.assert_called()
		client.customers.create.assert_called_once()

	def test_get_or_create_recovers_via_metadata_search(self):
		client = MagicMock()
		found = MagicMock(id="cus_found", deleted=False)
		found.get = lambda k, d=None: {"id": "cus_found", "deleted": False}.get(k, d)
		client.customers.search.return_value = {"data": [found]}

		with (
			patch("stripe_payment.gateway.customers.frappe.db.has_column", return_value=True),
			patch("stripe_payment.gateway.customers.frappe.db.get_value", return_value=None),
			patch("stripe_payment.gateway.customers.frappe.db.set_value") as set_value,
		):
			cid = get_or_create_customer(client, customer="CUST-001")

		self.assertEqual(cid, "cus_found")
		client.customers.create.assert_not_called()
		set_value.assert_called()

	def test_get_party_for_payment_request(self):
		with (
			patch("stripe_payment.gateway.customers.frappe.db.exists", return_value=True),
			patch(
				"stripe_payment.gateway.customers.frappe.db.get_value",
				return_value=frappe._dict(party_type="Customer", party="CUST-001"),
			),
		):
			party = get_party_for_reference(
				{"reference_doctype": "Payment Request", "reference_docname": "PR-1"}
			)
		self.assertEqual(party, "CUST-001")

	def test_resolve_stripe_customer_uses_party(self):
		client = MagicMock()
		with (
			patch("stripe_payment.gateway.customers.get_party_for_reference", return_value="CUST-001"),
			patch("stripe_payment.gateway.customers.get_or_create_customer", return_value="cus_x") as create,
		):
			cid = resolve_stripe_customer(
				client, {"payer_email": "a@example.com", "payer_name": "A", "reference_doctype": "X"}
			)
		self.assertEqual(cid, "cus_x")
		create.assert_called_once()
		self.assertEqual(create.call_args.kwargs["customer"], "CUST-001")

	def test_legacy_token_charge_does_not_pass_customer(self):
		"""Charges with tok_… must not include customer (Stripe rejects unattached tokens)."""
		from stripe_payment.stripe.doctype.stripe_settings.stripe_settings import StripeSettings

		settings = StripeSettings({"doctype": "Stripe Settings", "name": "Stripe"})
		settings.data = frappe._dict(
			amount=200,
			currency="INR",
			stripe_token_id="tok_test_abc",
			description="Payment Request for ACC-SINV-1",
			payer_email="a@example.com",
		)
		settings.integration_request = MagicMock()
		settings.flags = frappe._dict()
		settings.finalize_request = MagicMock(return_value={"status": "Completed"})

		client = MagicMock()
		charge = MagicMock(captured=True)
		client.charges.create.return_value = charge

		with (
			patch(
				"stripe_payment.gateway.client.get_stripe_client",
				return_value=client,
			),
			patch(
				"stripe_payment.gateway.client.to_minor_units",
				return_value=20000,
			),
			patch(
				"stripe_payment.gateway.customers.resolve_stripe_customer",
				return_value="cus_Un9PuBBu6urEfA",
			) as resolve,
		):
			settings.create_charge_on_stripe()

		resolve.assert_called_once()
		client.charges.create.assert_called_once()
		params = client.charges.create.call_args.args[0]
		self.assertEqual(params["source"], "tok_test_abc")
		self.assertEqual(params["amount"], 20000)
		self.assertNotIn("customer", params)
