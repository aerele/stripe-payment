# Copyright (c) Frappe Technologies Pvt. Ltd. and contributors
# License: MIT. See LICENSE
#
# Unit tests for PaymentIntent ownership / claim helpers (no live Stripe calls).

import json
from unittest import skipUnless
from unittest.mock import MagicMock, patch

import frappe
from frappe.tests.utils import FrappeTestCase
from payment_core.utils import settle_payment_request

from stripe_payment.gateway.payment_intents import (
	assert_intent_matches_reference,
	claim_integration_request,
	finalize_payment_intent,
)
from stripe_payment.gateway.references import assert_reference_payable
from stripe_payment.stripe.doctype.stripe_settings.stripe_settings import StripeSettings

ERPNext_INSTALLED = "erpnext" in frappe.get_installed_apps()


def _build_settings(charged_amount=500, currency="INR", intent_id="pi_test"):
	"""Build a mock controller with an Integration Request carrying the given charge."""
	settings = MagicMock(name="SS")
	settings.name = "GW-1"
	settings.integration_request.output = intent_id
	settings.integration_request.data = json.dumps({"amount": charged_amount, "currency": currency})
	# payment_core's settlement seams: the controller reports what Stripe
	# actually charged and stamps provider fields on the Payment Entry.
	settings.get_charged_amount = lambda pr, _charged=charged_amount: _charged
	settings.annotate_payment_entry = MagicMock(name="annotate")
	return settings


def _build_pr(outstanding=1000, status="Requested", reference_doctype="Sales Invoice", reference_name="SI-1"):
	"""Build a mock Payment Request with the given outstanding amount and status."""
	pr = MagicMock(name="PR")
	pr.docstatus = 1
	pr.status = status
	pr.payment_channel = "Stripe"
	pr.reference_name = reference_name
	pr.reference_doctype = reference_doctype
	pr.outstanding_amount = outstanding
	pr.currency = "INR"
	pr.party_account_currency = "INR"
	pr.grand_total = outstanding
	pr.company = "Test Company"
	return pr


class TestPaymentIntents(FrappeTestCase):
	def _skip_without_erpnext(self):
		if not frappe.db.table_exists("Payment Request"):
			self.skipTest("settlement tests need ERPNext")

	def test_assert_intent_matches_reference_accepts_match(self):
		settings = MagicMock()
		settings.data = frappe._dict({"reference_doctype": "Payment Request", "reference_docname": "PR-001"})
		intent = {
			"metadata": {
				"reference_doctype": "Payment Request",
				"reference_docname": "PR-001",
			}
		}
		assert_intent_matches_reference(settings, intent)

	def test_assert_intent_matches_reference_rejects_mismatch(self):
		settings = MagicMock()
		settings.data = frappe._dict({"reference_doctype": "Payment Request", "reference_docname": "PR-001"})
		intent = {
			"metadata": {
				"reference_doctype": "Payment Request",
				"reference_docname": "PR-OTHER",
			}
		}
		with self.assertRaises(frappe.PermissionError):
			assert_intent_matches_reference(settings, intent)

	def test_claim_integration_request_first_wins(self):
		settings = MagicMock()
		settings.integration_request = MagicMock()
		settings.integration_request.name = "IR-TEST"

		with patch("payment_core.utils.frappe.db.get_value", return_value="Queued"):
			self.assertTrue(claim_integration_request(settings))
			settings.integration_request.db_set.assert_called_with(
				"status", "Completed", update_modified=False
			)

		with patch("payment_core.utils.frappe.db.get_value", return_value="Completed"):
			self.assertFalse(claim_integration_request(settings))

	def test_finalize_payment_intent_idempotent_when_already_completed(self):
		settings = MagicMock()
		settings.finalize_request.return_value = {"status": "Completed", "redirect_to": "payment-success"}

		ir = MagicMock()
		ir.status = "Completed"
		ir.name = "IR-DONE"

		intent = {
			"id": "pi_123",
			"metadata": {
				"reference_doctype": "Payment Request",
				"reference_docname": "PR-001",
				"integration_request": "IR-DONE",
			},
		}

		with (
			patch("payment_core.utils.frappe.db.exists", return_value=True),
			patch("payment_core.utils.frappe.get_doc", return_value=ir),
		):
			result = finalize_payment_intent(settings, intent)

		self.assertEqual(result["status"], "Completed")
		settings.finalize_request.assert_not_called()

	def test_assert_reference_payable_blocks_cancelled(self):
		row = frappe._dict(docstatus=1, status="Cancelled")
		with (
			patch("stripe_payment.gateway.references.frappe.db.exists", return_value=True),
			patch("stripe_payment.gateway.references.frappe.get_meta") as meta,
			patch("stripe_payment.gateway.references.frappe.db.get_value", return_value=row),
		):
			meta.return_value.has_field.return_value = True
			with self.assertRaises(frappe.ValidationError) as ctx:
				assert_reference_payable("Payment Request", "PR-CANCELLED")
		self.assertIn("cancelled", str(ctx.exception).lower())

	def test_assert_reference_payable_blocks_paid(self):
		row = frappe._dict(docstatus=1, status="Paid")
		with (
			patch("stripe_payment.gateway.references.frappe.db.exists", return_value=True),
			patch("stripe_payment.gateway.references.frappe.get_meta") as meta,
			patch("stripe_payment.gateway.references.frappe.db.get_value", return_value=row),
		):
			meta.return_value.has_field.return_value = True
			with self.assertRaises(frappe.ValidationError) as ctx:
				assert_reference_payable("Payment Request", "PR-PAID")
		self.assertIn("already paid", str(ctx.exception).lower())

	def test_assert_reference_payable_allows_open_request(self):
		row = frappe._dict(docstatus=1, status="Requested")
		with (
			patch("stripe_payment.gateway.references.frappe.db.exists", return_value=True),
			patch("stripe_payment.gateway.references.frappe.get_meta") as meta,
			patch("stripe_payment.gateway.references.frappe.db.get_value", return_value=row),
		):
			meta.return_value.has_field.return_value = True
			assert_reference_payable("Payment Request", "PR-OPEN")

	def test_settle_payment_request_skips_already_paid(self):
		self._skip_without_erpnext()
		"""I28: extraction preserved the early-return guards."""
		settings = MagicMock(name="SS")
		settings.name = "GW-1"
		pr = MagicMock(name="PR")
		pr.docstatus = 1
		pr.status = "Paid"
		with patch("payment_core.utils.frappe.db.set_value") as sv:
			settle_payment_request(pr, controller=settings)
		pr.set_as_paid.assert_not_called()
		sv.assert_not_called()

	@skipUnless(ERPNext_INSTALLED, "ERPNext settlement is optional for stripe_payment")
	def test_settle_payment_request_stamps_intent_and_settings(self):
		self._skip_without_erpnext()
		"""Stamps both stripe_payment_intent and stripe_settings on the PE."""
		settings = _build_settings(charged_amount=500, currency="INR")
		pr = _build_pr(outstanding=500)
		pe = MagicMock(name="PE")
		pe.name = "PE-1"
		pe.meta.has_field.return_value = True

		meta_mock = MagicMock()
		meta_mock.has_field.return_value = True

		with (
			patch("payment_core.utils.frappe.set_user"),
			# nowdate() resolves the system timezone through a System Settings DB
			# fetch on this branch; keep the db mocks from starving it.
			patch("payment_core.utils.nowdate", return_value="2026-09-18"),
			patch("payment_core.utils.frappe.get_meta", return_value=meta_mock),
			patch("payment_core.utils.frappe.db.set_value"),
			patch("payment_core.utils.frappe.db.table_exists", return_value=False),
			patch("payment_core.utils.frappe.db.get_value", return_value="INR"),
			patch(
				"erpnext.accounts.doctype.payment_entry.payment_entry.get_payment_entry",
				return_value=pe,
			),
			patch(
				"erpnext.accounts.doctype.payment_request.payment_request.get_existing_payment_entry",
				return_value=None,
			),
		):
			settle_payment_request(pr, controller=settings)
		# the controller's annotation seam is invoked with the booked PE
		settings.annotate_payment_entry.assert_called_once()
		_annotated_pe, _annotated_pr = settings.annotate_payment_entry.call_args[0]
		self.assertEqual(_annotated_pe.name, "PE-1")
		self.assertIs(_annotated_pr, pr)

	def test_controller_stamps_intent_and_settings_on_pe(self):
		self._skip_without_erpnext()
		"""StripeSettings.annotate_payment_entry stamps the provider ids for refund lookups."""
		settings = StripeSettings({"doctype": "Stripe Settings"})
		settings.name = "GW-1"
		settings.integration_request = MagicMock()
		settings.integration_request.output = "pi_test"
		pe = MagicMock(name="PE")
		pe.name = "PE-1"
		pe.meta.has_field.return_value = True

		with patch("stripe_payment.stripe.doctype.stripe_settings.stripe_settings.frappe.db.set_value") as sv:
			settings.annotate_payment_entry(pe, None)

		sv.assert_called_once_with(
			"Payment Entry",
			"PE-1",
			{"stripe_payment_intent": "pi_test", "stripe_settings": "GW-1"},
			update_modified=False,
		)

	def test_controller_get_charged_amount_reads_integration_request(self):
		self._skip_without_erpnext()
		"""StripeSettings.get_charged_amount reports the actually-charged amount."""
		settings = StripeSettings({"doctype": "Stripe Settings"})
		settings.integration_request = MagicMock()
		settings.integration_request.output = "pi_test"
		settings.integration_request.data = json.dumps({"amount": 424, "currency": "INR"})

		self.assertEqual(settings.get_charged_amount(None), 424)

	@skipUnless(ERPNext_INSTALLED, "ERPNext settlement is optional for stripe_payment")
	def test_settle_allows_second_pe_when_si_has_outstanding(self):
		self._skip_without_erpnext()
		"""Creates a second Payment Entry when a PE exists but the SI still has an outstanding balance."""
		settings = _build_settings(charged_amount=500)
		pr = _build_pr(outstanding=500)
		pe = MagicMock(name="PE")
		pe.name = "PE-2"
		pe.meta.has_field.return_value = True

		meta_mock = MagicMock()
		meta_mock.has_field.return_value = True

		# db.get_value call order: (1) SI outstanding_amount → 500.0, (2) Company currency → "INR"
		# is_multi_currency is False (both INR) so no conversion_rate / second outstanding read.
		with (
			patch("payment_core.utils.frappe.set_user"),
			# nowdate() resolves the system timezone through a System Settings DB
			# fetch on this branch; keep the db mocks from starving it.
			patch("payment_core.utils.nowdate", return_value="2026-09-18"),
			patch("payment_core.utils.frappe.get_meta", return_value=meta_mock),
			patch("payment_core.utils.frappe.db.set_value"),
			patch("payment_core.utils.frappe.db.table_exists", return_value=False),
			patch(
				"payment_core.utils.frappe.db.get_value",
				side_effect=[500.0, "INR"],
			),
			patch(
				"erpnext.accounts.doctype.payment_entry.payment_entry.get_payment_entry",
				return_value=pe,
			),
			patch(
				"erpnext.accounts.doctype.payment_request.payment_request.get_existing_payment_entry",
				return_value="PE-1",
			),
		):
			settle_payment_request(pr, controller=settings)
		pe.insert.assert_called_once()
		pe.submit.assert_called_once()

	def test_settle_blocks_duplicate_when_si_fully_paid(self):
		self._skip_without_erpnext()
		"""Blocks settlement when a PE exists and the SI outstanding is zero."""
		settings = _build_settings(charged_amount=1000)
		pr = _build_pr(outstanding=1000)

		meta_mock = MagicMock()
		meta_mock.has_field.return_value = True

		with (
			patch("payment_core.utils.frappe.set_user"),
			# nowdate() resolves the system timezone through a System Settings DB
			# fetch on this branch; keep the db mocks from starving it.
			patch("payment_core.utils.nowdate", return_value="2026-09-18"),
			patch("payment_core.utils.frappe.get_meta", return_value=meta_mock),
			patch("payment_core.utils.frappe.db.set_value"),
			patch("payment_core.utils.frappe.db.table_exists", return_value=False),
			patch(
				"payment_core.utils.frappe.db.get_value",
				return_value=0.0,
			),
			patch("erpnext.accounts.doctype.payment_entry.payment_entry.get_payment_entry") as gpe_mock,
			patch(
				"erpnext.accounts.doctype.payment_request.payment_request.get_existing_payment_entry",
				return_value="PE-1",
			),
		):
			settle_payment_request(pr, controller=settings)
		# get_payment_entry NOT called — SI is fully paid, genuine duplicate.
		gpe_mock.assert_not_called()
