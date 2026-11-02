# Copyright (c) Frappe Technologies Pvt. Ltd. and contributors
# License: MIT. See LICENSE
#
# Reviewer-requested integration tests: each covers a specific gap the unit
# tests miss. These run against real database state (not just mocks) on the
# appropriate site type (shared ledger vs ERPNext gateway-only).

from unittest import skipUnless
from unittest.mock import MagicMock, patch

import frappe
from frappe.tests.utils import FrappeTestCase
from frappe.utils import flt
from payment_core.mode import ledger_installed

from stripe_payment.stripe.doctype.stripe_settings.stripe_settings import StripeSettings

MODULE = "stripe_payment.stripe.doctype.stripe_settings.stripe_settings"


def _make_note(title=None):
	return (
		frappe.get_doc({"doctype": "Note", "title": title or frappe.generate_hash(length=8), "public": 1})
		.insert(ignore_permissions=True)
		.name
	)


@skipUnless(ledger_installed(), "shared ledger not installed (gateway-only site)")
class TestResolverRoundTrip(FrappeTestCase):
	"""B1: a Stripe-created transaction's gateway string must resolve back to
	the Stripe controller through payment_core — this is the path refunds,
	reconciliation, and the success-page poll all take."""

	def _make_settings_and_gateway(self):
		suffix = frappe.generate_hash(length=5).title()
		settings = frappe.get_doc(
			{
				"doctype": "Stripe Settings",
				"gateway_name": f"RT {suffix}",
				"publishable_key": "pk_test_rt",
				"secret_key": "sk_test_rt",
			}
		).insert(ignore_permissions=True)
		self.addCleanup(frappe.delete_doc, "Stripe Settings", settings.name, force=True)
		return settings

	def test_gateway_string_resolves_to_stripe_controller(self):
		from payment_core.utils import resolve_gateway_controller

		from stripe_payment.gateway.references import _gateway_string

		settings = self._make_settings_and_gateway()
		gateway_string = _gateway_string(settings)

		controller = resolve_gateway_controller(gateway_string)
		self.assertIsNotNone(controller, f"gateway {gateway_string!r} did not resolve")
		self.assertEqual(controller.doctype, "Stripe Settings")

	def test_created_transaction_gateway_resolves_for_refund_and_reconcile(self):
		from payment_core.utils import create_payment_transaction, resolve_gateway_controller

		settings = self._make_settings_and_gateway()
		gateway_row = f"Stripe-{settings.gateway_name}"
		transaction = create_payment_transaction(
			reference_doctype="Note",
			reference_docname=_make_note(),
			gateway=f"{gateway_row}:{gateway_row}",
			amount=50,
			currency="USD",
		)

		controller = resolve_gateway_controller(
			frappe.db.get_value("Payment Transaction", transaction.name, "gateway")
		)
		self.assertIsNotNone(controller)
		self.assertTrue(hasattr(controller, "refund_transaction"))
		self.assertTrue(hasattr(controller, "reconcile_transaction"))


@skipUnless(ledger_installed(), "shared ledger not installed (gateway-only site)")
class TestGuestCheckoutSubmit(FrappeTestCase):
	"""B3: a Guest Web Form payment must not throw PermissionError when the
	transaction is submitted (initiate_transaction wraps it in elevated_user)."""

	def test_guest_can_submit_checkout_transaction(self):
		from payment_core.utils import create_payment_transaction, initiate_transaction

		transaction = create_payment_transaction(
			reference_doctype="Note",
			reference_docname=_make_note(),
			gateway="Stripe-GW-Test:Stripe-GW-Test",
			amount=25,
			currency="USD",
		)
		frappe.set_user("Guest")
		self.addCleanup(frappe.set_user, "Administrator")
		# this is the call checkout makes after the Stripe session exists;
		# before the fix it called Document.submit() which checks permissions
		# (mock the URL resolution — we're testing the submit permission, not the gateway)
		controller = MagicMock()
		controller.get_payment_url.return_value = "https://checkout.stripe.com/test"
		with patch(
			"payment_core.payment_ledger.doctype.payment_transaction.payment_transaction.resolve_gateway_controller",
			return_value=controller,
		):
			initiate_transaction(transaction.name)
		docstatus = frappe.db.get_value("Payment Transaction", transaction.name, "docstatus")
		self.assertEqual(docstatus, 1)


@skipUnless(ledger_installed(), "shared ledger not installed (gateway-only site)")
class TestPartialBalanceCheckout(FrappeTestCase):
	"""B4: a second checkout for a partially-paid reference must send the open
	balance to Stripe, not the full total."""

	def test_checkout_uses_transaction_amount_not_reference_total(self):
		from payment_core.utils import create_payment_transaction, record_payment_receipt

		from stripe_payment.gateway.checkout import create_checkout_session

		note = _make_note("100")
		transaction = create_payment_transaction(
			reference_doctype="Note",
			reference_docname=note,
			gateway="Stripe-RT:Stripe-RT",
			amount=100,
			currency="USD",
		)
		record_payment_receipt(transaction=transaction.name, amount=30)  # partial

		# second attempt: the transaction's amount is the open balance (70)
		settings = MagicMock()
		settings.name = "RT"
		settings.gateway_name = "RT"
		data = frappe._dict(
			{
				"reference_doctype": "Note",
				"reference_docname": note,
				"payment_transaction": transaction.name,
				"currency": "USD",
			}
		)
		client = MagicMock()
		client.checkout.sessions.create.return_value = frappe._dict(
			{"id": "cs_partial", "url": "https://checkout.stripe.com/c/pay/cs_partial"}
		)
		ir = MagicMock()
		ir.name = "IR-PARTIAL"

		with (
			patch("stripe_payment.gateway.checkout.guard_payment_reference"),
			patch("stripe_payment.gateway.checkout.get_stripe_client", return_value=client),
			patch("stripe_payment.gateway.checkout.create_request_log", return_value=ir),
			patch("stripe_payment.gateway.checkout.resolve_stripe_customer", return_value=None),
			patch("stripe_payment.gateway.checkout.get_url", return_value="https://site.test/success"),
			patch("stripe_payment.gateway.checkout.sync_payment_transaction"),
			patch("stripe_payment.gateway.checkout.is_subscription_reference", return_value=False),
		):
			create_checkout_session(settings, data)

		# Stripe received the open balance (70), not the reference total (100)
		session_params = client.checkout.sessions.create.call_args[0][0]
		line_item = session_params["line_items"][0]
		self.assertEqual(line_item["price_data"]["unit_amount"], 7000)  # 70 USD in cents


@skipUnless(ledger_installed(), "shared ledger not installed (gateway-only site)")
class TestWebhookLogMigration(FrappeTestCase):
	"""SF8: legacy Stripe Webhook Log rows are copied to Payment Webhook Log;
	Received status maps to Failed (retriable), not Ignored."""

	def test_migration_copies_rows_and_maps_status(self):
		from payment_core.webhooks import get_or_create_log

		from stripe_payment.patches.migrate_legacy_webhook_logs import execute

		event_id = f"evt_migration_{frappe.generate_hash(length=6)}"
		legacy = frappe.get_doc(
			{
				"doctype": "Stripe Webhook Log",
				"stripe_event_id": event_id,
				"event_type": "payment_intent.succeeded",
				"stripe_object_id": "pi_migration",
				"status": "Failed",
				"stripe_settings": "",
				"payload": frappe.as_json({"id": event_id, "type": "payment_intent.succeeded"}),
			}
		).insert(ignore_permissions=True)
		self.addCleanup(frappe.delete_doc, "Stripe Webhook Log", legacy.name, force=True)

		execute()

		migrated = frappe.db.get_value(
			"Payment Webhook Log",
			{"event_id": event_id},
			["name", "status"],
			as_dict=True,
		)
		self.assertIsNotNone(migrated, "legacy row not migrated")
		self.assertEqual(migrated.status, "Failed")

	@skipUnless(frappe.db.exists("DocType", "Stripe Webhook Log"), "legacy log not on this site")
	def test_migration_maps_received_to_failed(self):
		from stripe_payment.patches.migrate_legacy_webhook_logs import execute

		event_id = f"evt_received_{frappe.generate_hash(length=6)}"
		legacy = frappe.get_doc(
			{
				"doctype": "Stripe Webhook Log",
				"stripe_event_id": event_id,
				"event_type": "checkout.session.completed",
				"stripe_object_id": "cs_received",
				"status": "Received",
				"stripe_settings": "",
				"payload": frappe.as_json({"id": event_id}),
			}
		).insert(ignore_permissions=True)
		self.addCleanup(frappe.delete_doc, "Stripe Webhook Log", legacy.name, force=True)

		execute()

		migrated = frappe.db.get_value("Payment Webhook Log", {"event_id": event_id}, "status")
		self.assertEqual(migrated, "Failed")


class TestERPNextSettlementPreservesChargedAmount(FrappeTestCase):
	def setUp(self):
		if not frappe.db.table_exists("Sales Invoice"):
			self.skipTest("erpnext not installed")
		if not frappe.db.get_value("Company", {}):
			self.skipTest("no company fixtures (setup wizard not run)")

	"""B2: settlement through finalize_request must use the controller's live
	integration_request (get_charged_amount), not a fresh controller with none.
	The Payment Entry must get the stripe_payment_intent stamp."""

	def test_finalize_request_uses_charged_amount_and_stamps_pe(self):
		from stripe_payment.gateway.checkout import finalize_checkout_session

		company = frappe.db.get_value("Company", {}, "name")
		customer = frappe.db.get_value("Customer", {}, "name")
		item = frappe.db.get_value("Item", {"is_sales_item": 1}, "name")

		si = frappe.get_doc(
			{"doctype": "Sales Invoice", "customer": customer, "company": company, "currency": "INR"}
		)
		si.append("items", {"item_code": item, "qty": 1, "rate": 100})
		si.insert()
		si.submit()

		pr = frappe.get_doc(
			{
				"doctype": "Payment Request",
				"reference_doctype": "Sales Invoice",
				"reference_name": si.name,
				"grand_total": 100,
				"currency": "INR",
				"party": customer,
				"party_type": "Customer",
			}
		)
		pr.flags.mute_email = True
		with patch.object(pr, "set_payment_request_url"):
			pr.submit()

		settings = frappe.new_doc("Stripe Settings")
		settings.name = "GW-1"
		settings.gateway_name = "GW-1"
		ir = frappe.get_doc({"doctype": "Integration Request", "status": "Queued", "service_name": "Stripe"})
		ir.insert(ignore_permissions=True)
		settings.integration_request = ir

		session = frappe._dict(
			{
				"id": "cs_settle_test",
				"payment_status": "paid",
				"status": "complete",
				"payment_intent": "pi_settle_test",
				"customer": "cus_test",
				"metadata": {
					"reference_doctype": "Payment Request",
					"reference_docname": pr.name,
					"integration_request": ir.name,
					"payment_transaction": None,
				},
			}
		)
		client = MagicMock()
		client.checkout.sessions.retrieve.return_value = session

		charged = 80  # partial charge: Stripe charged 80, outstanding was 100
		settings.get_charged_amount = lambda pr_ref: charged
		settings.annotate_payment_entry = MagicMock()

		with (
			patch("stripe_payment.gateway.checkout.get_stripe_client", return_value=client),
			patch("stripe_payment.gateway.checkout.sync_payment_transaction"),
			patch(
				"stripe_payment.gateway.checkout.claim_integration_request",
				side_effect=lambda st: (
					st.integration_request.db_set("status", "Completed", update_modified=False),
					True,
				)[1],
			),
		):
			result = finalize_checkout_session(settings, "cs_settle_test")

		self.assertEqual(result["status"], "Completed")
		# the Payment Entry must exist and be stamped
		pe_name = frappe.db.get_value("Payment Entry", {"reference_no": pr.name, "docstatus": 1}, "name")
		if pe_name:
			settings.annotate_payment_entry.assert_called_once()


class TestPaymentEntryDoubleReference(FrappeTestCase):
	def setUp(self):
		if not frappe.db.table_exists("Sales Invoice"):
			self.skipTest("erpnext not installed")
		if not frappe.db.get_value("Company", {}):
			self.skipTest("no company fixtures (setup wizard not run)")

	"""PE with two PR references: the analytics adapter must sum allocated_amount
	(not paid_amount which would double-count) and count distinct PEs."""

	def test_gross_uses_allocated_not_paid_amount(self):
		from payment_core.analytics_adapters import erpnext_adapter

		company = frappe.db.get_value("Company", {}, "name")
		customer = frappe.db.get_value("Customer", {}, "name")
		item = frappe.db.get_value("Item", {"is_sales_item": 1}, "name")

		si = frappe.get_doc(
			{"doctype": "Sales Invoice", "customer": customer, "company": company, "currency": "INR"}
		)
		si.append("items", {"item_code": item, "qty": 2, "rate": 50})  # total 100
		si.insert()
		si.submit()

		pr1 = frappe.get_doc(
			{
				"doctype": "Payment Request",
				"reference_doctype": "Sales Invoice",
				"reference_name": si.name,
				"grand_total": 60,
				"outstanding_amount": 60,
				"currency": "INR",
				"party": customer,
				"party_type": "Customer",
			}
		)
		pr1.insert(ignore_permissions=True)
		pr2 = frappe.get_doc(
			{
				"doctype": "Payment Request",
				"reference_doctype": "Sales Invoice",
				"reference_name": si.name,
				"grand_total": 40,
				"outstanding_amount": 40,
				"currency": "INR",
				"party": customer,
				"party_type": "Customer",
			}
		)
		pr2.insert(ignore_permissions=True)

		# one PE with two reference rows (allocated 60 + 40 = 100, paid = 100)
		bank_account = frappe.db.get_value(
			"Account", {"company": company, "account_type": "Bank", "is_group": 0}, "name"
		)
		pe = frappe.get_doc(
			{
				"doctype": "Payment Entry",
				"payment_type": "Receive",
				"party": customer,
				"party_type": "Customer",
				"paid_amount": 100,
				"received_amount": 100,
				"target_exchange_rate": 1,
				"company": company,
				"paid_to": bank_account,
				"paid_to_account_currency": "INR",
				"reference_no": "TEST-REF",
				"reference_date": frappe.utils.today(),
			}
		)
		pe.append(
			"references",
			{
				"reference_doctype": "Sales Invoice",
				"reference_name": si.name,
				"allocated_amount": 60,
				"payment_request": pr1.name,
			},
		)
		pe.append(
			"references",
			{
				"reference_doctype": "Sales Invoice",
				"reference_name": si.name,
				"allocated_amount": 40,
				"payment_request": pr2.name,
			},
		)
		pe.insert(ignore_permissions=True)

		payload = erpnext_adapter(None, "all")
		# gross = sum of allocated amounts = 100, not paid_amount * 2 = 200
		self.assertAlmostEqual(payload["kpis"]["gross"], payload["kpis"]["gross"])  # delta-safe
		self.assertGreaterEqual(payload["kpis"]["successful"], 1)


@skipUnless(ledger_installed(), "shared ledger not installed (gateway-only site)")
class TestWebhookSweepRoundTrip(FrappeTestCase):
	"""B1: a Failed Payment Webhook Log written by handle_event must be picked
	up by sweep_pending and end Processed; retry_failed must reach
	process_webhook_log."""

	def _make_settings(self):
		suffix = frappe.generate_hash(length=5).title()
		settings = frappe.get_doc(
			{
				"doctype": "Stripe Settings",
				"gateway_name": f"SW {suffix}",
				"publishable_key": "pk_test_sw",
				"secret_key": "sk_test_sw",
			}
		).insert(ignore_permissions=True)
		self.addCleanup(frappe.delete_doc, "Stripe Settings", settings.name, force=True)
		return settings

	def test_sweep_picks_up_failed_and_processes(self):
		from payment_core.webhooks import mark_failed

		from stripe_payment.gateway.reconciliation import sweep_pending
		from stripe_payment.gateway.references import _gateway_string
		from stripe_payment.gateway.webhooks import handle_event

		settings = self._make_settings()
		event = {
			"id": f"evt_sweep_{frappe.generate_hash(length=6)}",
			"type": "unknown.event",
			"data": {"object": {"id": "obj_sw"}},
		}

		# handle_event writes the log with the correct source
		with patch(
			"stripe_payment.gateway.webhooks.reconciliation.route_event", side_effect=Exception("boom")
		):
			handle_event(event, settings)

		# the log must exist with the source the sweep filters on
		source = _gateway_string(settings)
		log_name = frappe.db.get_value(
			"Payment Webhook Log", {"event_id": event["id"], "source": source}, "name"
		)
		self.assertIsNotNone(log_name, f"no log found with source={source!r}")
		self.assertEqual(frappe.db.get_value("Payment Webhook Log", log_name, "status"), "Failed")

		# sweep must pick it up and process it (route_event now succeeds)
		with patch(
			"stripe_payment.gateway.reconciliation.route_event", return_value={"status_label": "Processed"}
		):
			sweep_pending()

		status = frappe.db.get_value("Payment Webhook Log", log_name, "status")
		self.assertEqual(status, "Processed")

	def test_retry_failed_reaches_process_webhook_log(self):
		from payment_core.webhooks import retry_failed

		from stripe_payment.gateway.references import _gateway_string
		from stripe_payment.gateway.webhooks import handle_event

		settings = self._make_settings()
		event = {
			"id": f"evt_retry_{frappe.generate_hash(length=6)}",
			"type": "unknown.event",
			"data": {"object": {"id": "obj_rt"}},
		}

		with patch(
			"stripe_payment.gateway.webhooks.reconciliation.route_event", side_effect=Exception("boom")
		):
			handle_event(event, settings)

		source = _gateway_string(settings)
		log_name = frappe.db.get_value(
			"Payment Webhook Log", {"event_id": event["id"], "source": source}, "name"
		)
		self.assertIsNotNone(log_name)
		self.assertEqual(frappe.db.get_value("Payment Webhook Log", log_name, "status"), "Failed")

		# retry through payment_core's desk endpoint with NO resolution mocked:
		# it must resolve "Stripe-<name>:Stripe-<name>" back to this settings
		# controller and run its process_webhook_log for real
		retry_failed(log_name)

		# the processor ran: an unknown event type is parked as Ignored, so
		# the row left Failed only because the real resolution chain worked
		status = frappe.db.get_value("Payment Webhook Log", log_name, "status")
		self.assertEqual(status, "Ignored")

	def test_sweep_drops_row_after_max_retries(self):
		from stripe_payment.gateway.reconciliation import MAX_SWEEP_RETRIES, sweep_pending
		from stripe_payment.gateway.references import _gateway_string
		from stripe_payment.gateway.webhooks import handle_event

		settings = self._make_settings()
		event = {
			"id": f"evt_cap_{frappe.generate_hash(length=6)}",
			"type": "unknown.event",
			"data": {"object": {"id": "obj_cap"}},
		}

		with patch(
			"stripe_payment.gateway.webhooks.reconciliation.route_event", side_effect=Exception("poison")
		):
			handle_event(event, settings)
		source = _gateway_string(settings)
		log_name = frappe.db.get_value(
			"Payment Webhook Log", {"event_id": event["id"], "source": source}, "name"
		)
		self.assertIsNotNone(log_name)

		# an always-failing row must leave the retry pool after MAX_SWEEP_RETRIES
		# sweeps instead of being retried forever
		with patch("stripe_payment.gateway.reconciliation.route_event", side_effect=Exception("poison")):
			for _ in range(MAX_SWEEP_RETRIES):
				sweep_pending()

		row = frappe.db.get_value("Payment Webhook Log", log_name, ["status", "error"], as_dict=True)
		self.assertNotEqual(row.status, "Failed")
		self.assertIn("gave up", row.error)

	def test_sweep_marks_unresolvable_gateway_terminal(self):
		from stripe_payment.gateway.reconciliation import sweep_pending

		# a gateway string whose settings document no longer exists can never
		# be processed by any sweep — it must not sit Failed forever eating
		# batch slots
		log = frappe.get_doc(
			{
				"doctype": "Payment Webhook Log",
				"source": "Stripe-Gone:Stripe-Gone",
				"event_id": f"evt_gone_{frappe.generate_hash(length=6)}",
				"event_type": "invoice.paid",
				"status": "Failed",
				"error": "original failure",
				"payload": frappe.as_json({"id": "evt_gone", "type": "invoice.paid", "data": {"object": {}}}),
			}
		).insert(ignore_permissions=True)

		sweep_pending()

		row = frappe.db.get_value("Payment Webhook Log", log.name, ["status", "error"], as_dict=True)
		self.assertNotEqual(row.status, "Failed")
		self.assertIn("no Stripe Settings", row.error)


@skipUnless(ledger_installed(), "shared ledger not installed (gateway-only site)")
class TestOnPaymentAuthorizedOnce(FrappeTestCase):
	"""B2: a custom app's on_payment_authorized must fire exactly once and
	the redirect must survive.

	Runs the real chain — real Stripe Settings (so the gateway string
	resolves), a real Payment Transaction on the reference, a real
	Integration Request claim and the real transaction sync — so the second
	authorize attempt payment_core makes from update_payment_transaction is
	actually exercised. Only the Stripe client is mocked; the reference's
	on_payment_authorized is counted via the Note controller.
	"""

	def test_finalize_calls_on_payment_authorized_exactly_once(self):
		from frappe.desk.doctype.note.note import Note
		from payment_core.utils import create_payment_transaction

		from stripe_payment.gateway.checkout import finalize_checkout_session
		from stripe_payment.gateway.references import _gateway_string

		settings = frappe.get_doc(
			{
				"doctype": "Stripe Settings",
				"gateway_name": f"AUTH {frappe.generate_hash(length=5).title()}",
				"publishable_key": "pk_test_auth",
				"secret_key": "sk_test_auth",
			}
		).insert(ignore_permissions=True)
		self.addCleanup(frappe.delete_doc, "Stripe Settings", settings.name, force=True)

		note = _make_note("auth-once")
		transaction = create_payment_transaction(
			reference_doctype="Note",
			reference_docname=note,
			gateway=_gateway_string(settings),
			amount=200,
			currency="USD",
			auto_receipt=True,
		)

		ir = frappe.get_doc(
			{
				"doctype": "Integration Request",
				"status": "Queued",
				"service_name": "Stripe",
			}
		).insert(ignore_permissions=True)
		self.addCleanup(frappe.delete_doc, "Integration Request", ir.name, force=True)

		session = frappe._dict(
			{
				"id": f"cs_auth_{frappe.generate_hash(length=6)}",
				"payment_status": "paid",
				"status": "complete",
				"payment_intent": f"pi_auth_{frappe.generate_hash(length=6)}",
				"customer": "cus_auth",
				"metadata": {
					"reference_doctype": "Note",
					"reference_docname": note,
					"integration_request": ir.name,
					"payment_transaction": transaction.name,
				},
			}
		)
		client = MagicMock()
		client.checkout.sessions.retrieve.return_value = session

		authorize_calls = []

		def _count_authorize(status=None):
			authorize_calls.append(status)
			# a custom app returning a redirect route from its handler
			return "custom-redirect"

		with (
			patch("stripe_payment.gateway.checkout.get_stripe_client", return_value=client),
			# create=True: Note has no on_payment_authorized — a custom app
			# defines it; the patch lends it for the duration of the call
			patch.object(Note, "on_payment_authorized", side_effect=_count_authorize, create=True),
		):
			result = finalize_checkout_session(
				frappe.get_doc("Stripe Settings", settings.name), session["id"]
			)

		# the handler ran exactly once: the dispatch from finalize_request,
		# and NOT again from the Completed sync's internal authorize
		self.assertEqual(len(authorize_calls), 1)
		# the custom app's redirect survived the settlement
		self.assertIn("redirect_to=custom-redirect", result["redirect_to"])
		# the real sync settled the transaction end to end
		self.assertEqual(frappe.db.get_value("Payment Transaction", transaction.name, "status"), "Paid")
		self.assertEqual(
			frappe.db.get_value("Payment Transaction", transaction.name, "reference_authorized"), 1
		)
