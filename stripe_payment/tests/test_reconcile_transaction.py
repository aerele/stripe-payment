# Copyright (c) Frappe Technologies Pvt. Ltd. and contributors
# License: MIT. See LICENSE
#
# Unit tests for the success-page reconcile entry point (no live Stripe calls).

from unittest import skipUnless
from unittest.mock import MagicMock, patch

from frappe.tests.utils import FrappeTestCase
from payment_core.mode import ledger_installed

from stripe_payment.stripe.doctype.stripe_settings.stripe_settings import StripeSettings

MODULE = "stripe_payment.stripe.doctype.stripe_settings.stripe_settings"


def _row(provider_transaction_id="", checkout_url=""):
	row = MagicMock()
	row.provider_transaction_id = provider_transaction_id
	row.checkout_url = checkout_url
	return row


@skipUnless(ledger_installed(), "ledger reconcile needs the shared Payment Transaction")
class TestReconcileTransaction(FrappeTestCase):
	def test_reconcile_uses_stored_session_id(self):
		settings = object.__new__(StripeSettings)
		with (
			patch(f"{MODULE}.frappe.db.get_value", return_value=_row("cs_test_stored")),
			patch(f"{MODULE}.checkout") as checkout_mod,
		):
			StripeSettings.reconcile_transaction(settings, "TXN-1")

		checkout_mod.finalize_checkout_session.assert_called_once()
		self.assertEqual(checkout_mod.finalize_checkout_session.call_args[0][1], "cs_test_stored")

	def test_reconcile_falls_back_to_checkout_url(self):
		settings = object.__new__(StripeSettings)
		url = "https://checkout.stripe.com/c/pay/cs_test_from_url#frag"
		with (
			patch(f"{MODULE}.frappe.db.get_value", return_value=_row("", url)),
			patch(f"{MODULE}.checkout") as checkout_mod,
		):
			StripeSettings.reconcile_transaction(settings, "TXN-2")

		self.assertEqual(checkout_mod.finalize_checkout_session.call_args[0][1], "cs_test_from_url")

	def test_reconcile_skips_without_any_session(self):
		settings = object.__new__(StripeSettings)
		with (
			patch(f"{MODULE}.frappe.db.get_value", return_value=_row()),
			patch(f"{MODULE}.checkout") as checkout_mod,
		):
			result = StripeSettings.reconcile_transaction(settings, "TXN-3")

		self.assertIsNone(result)
		checkout_mod.finalize_checkout_session.assert_not_called()

	def test_reconcile_pi_seeds_data_from_transaction(self):
		"""A pi_ transaction reconciles through the intent path, and the
		controller payment_core builds fresh has no session ``data`` — the
		intent path reads it immediately (assert_intent_matches_reference),
		so reconcile must seed it from the transaction row first.
		"""
		settings = object.__new__(StripeSettings)
		row = MagicMock()
		row.provider_transaction_id = "pi_test_seed"
		row.checkout_url = ""
		row.reference_doctype = "Note"
		row.reference_docname = "NOTE-1"
		with (
			patch(f"{MODULE}.frappe.db.get_value", return_value=row),
			patch(f"{MODULE}.payment_intents") as pi_mod,
		):
			StripeSettings.reconcile_transaction(settings, "TXN-PI")

		pi_mod.finalize_payment_intent_by_id.assert_called_once_with(settings, "pi_test_seed")
		# accessing settings.data raises AttributeError unless reconcile seeded it
		self.assertEqual(settings.data.reference_doctype, "Note")
		self.assertEqual(settings.data.reference_docname, "NOTE-1")
		self.assertEqual(settings.data.payment_transaction, "TXN-PI")
