# Copyright (c) 2017, Frappe Technologies and contributors
# License: MIT. See LICENSE
#
# Stripe Settings controller. Embedded PaymentIntents checkout lives in
# stripe_payment.gateway.payment_intents; this keeps the V1 controller surface.

from types import MappingProxyType
from urllib.parse import urlencode

import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils import call_hook_method, flt, get_url
from payment_core.api.controllers import get_gateway_controller_name
from payment_core.api.gateway import GatewayControllerMixin
from payment_core.utils import (
	authorize_reference,
	create_payment_gateway,
	elevated_user,
	settle_payment_request,
)
from payment_core.webhooks import mark_failed, mark_processed

from stripe_payment.gateway import checkout, customers, payment_intents
from stripe_payment.gateway.client import (
	clear_api_key_cache as clear_client_api_key_cache,
)
from stripe_payment.gateway.client import (
	get_stripe_client,
	to_minor_units,
)
from stripe_payment.gateway.reconciliation import route_event
from stripe_payment.gateway.webhooks import clear_cache as clear_webhook_cache

currency_wise_minimum_charge_amount = {
	"JPY": 50,
	"MXN": 10,
	"DKK": 2.50,
	"HKD": 4.00,
	"NOK": 3.00,
	"SEK": 3.00,
	"USD": 0.50,
	"AUD": 0.50,
	"BRL": 0.50,
	"CAD": 0.50,
	"CHF": 0.50,
	"EUR": 0.50,
	"GBP": 0.30,
	"NZD": 0.50,
	"SGD": 0.50,
}


class StripeSettings(GatewayControllerMixin, Document):
	supported_currencies = (
		"AED",
		"ALL",
		"ANG",
		"ARS",
		"AUD",
		"AWG",
		"BBD",
		"BDT",
		"BIF",
		"BMD",
		"BND",
		"BOB",
		"BRL",
		"BSD",
		"BWP",
		"BZD",
		"CAD",
		"CHF",
		"CLP",
		"CNY",
		"COP",
		"CRC",
		"CVE",
		"CZK",
		"DJF",
		"DKK",
		"DOP",
		"DZD",
		"EGP",
		"ETB",
		"EUR",
		"FJD",
		"FKP",
		"GBP",
		"GIP",
		"GMD",
		"GNF",
		"GTQ",
		"GYD",
		"HKD",
		"HNL",
		"HRK",
		"HTG",
		"HUF",
		"IDR",
		"ILS",
		"INR",
		"ISK",
		"JMD",
		"JPY",
		"KES",
		"KHR",
		"KMF",
		"KRW",
		"KYD",
		"KZT",
		"LAK",
		"LBP",
		"LKR",
		"LRD",
		"MAD",
		"MDL",
		"MNT",
		"MOP",
		"MRO",
		"MUR",
		"MVR",
		"MWK",
		"MXN",
		"MYR",
		"NAD",
		"NGN",
		"NIO",
		"NOK",
		"NPR",
		"NZD",
		"PAB",
		"PEN",
		"PGK",
		"PHP",
		"PKR",
		"PLN",
		"PYG",
		"QAR",
		"RUB",
		"SAR",
		"SBD",
		"SCR",
		"SEK",
		"SGD",
		"SHP",
		"SLL",
		"SOS",
		"STD",
		"SVC",
		"SZL",
		"THB",
		"TOP",
		"TTD",
		"TWD",
		"TZS",
		"UAH",
		"UGX",
		"USD",
		"UYU",
		"UZS",
		"VND",
		"VUV",
		"WST",
		"XAF",
		"XOF",
		"XPF",
		"YER",
		"ZAR",
	)

	currency_wise_minimum_charge_amount = MappingProxyType(currency_wise_minimum_charge_amount)

	def on_update(self):
		create_payment_gateway(
			"Stripe-" + self.gateway_name,
			settings="Stripe Settings",
			controller=self.gateway_name,
		)
		call_hook_method("payment_gateway_enabled", gateway="Stripe-" + self.gateway_name)
		self.set_webhook_endpoint()
		clear_webhook_secret_cache()
		clear_api_key_cache()

	def on_trash(self):
		clear_webhook_secret_cache()
		clear_api_key_cache()

	def set_webhook_endpoint(self):
		"""Show the admin which URL to register as a Stripe webhook endpoint."""
		endpoint = get_url("/api/method/stripe_payment.stripe.doctype.stripe_settings.webhooks")
		if self.webhook_endpoint != endpoint:
			self.db_set("webhook_endpoint", endpoint, update_modified=False)

	def validate_transaction_currency(self, currency):
		if currency not in self.supported_currencies:
			frappe.throw(
				_(
					"Please select another payment method. Stripe does not support transactions in currency '{0}'"
				).format(currency)
			)

	def validate_minimum_transaction_amount(self, currency, amount):
		if currency in self.currency_wise_minimum_charge_amount:
			if flt(amount) < self.currency_wise_minimum_charge_amount.get(currency, 0.0):
				frappe.throw(
					_("For currency {0}, the minimum transaction amount should be {1}").format(
						currency, self.currency_wise_minimum_charge_amount.get(currency, 0.0)
					)
				)

	def get_payment_url(self, **kwargs):
		return checkout.get_payment_url(self, **kwargs)

	def create_checkout_session(self, data):
		return checkout.create_checkout_session(self, data)

	def finalize_checkout_session(self, session_id):
		return checkout.finalize_checkout_session(self, session_id)

	def process_webhook_log(self, log_name):
		"""Re-run the processor for a Failed Payment Webhook Log row (desk retry)."""
		log = frappe.get_doc("Payment Webhook Log", log_name)
		event = frappe.parse_json(log.payload) if log.payload else None
		if not event:
			mark_failed(log_name, "empty payload")
			return
		# savepoint: route_event's partial writes must not leak into the
		# committed state when the retry fails (same pattern as handle_event)
		frappe.db.savepoint("stripe_webhook_retry")
		try:
			# elevated: the webhook and sweep paths run as Administrator, and
			# handlers insert Payment Entries etc. — a desk retry must not
			# fail on the retrying user's permissions
			with elevated_user():
				result = route_event(event, self) or {}
			mark_processed(
				log_name,
				status=result.get("status_label", "Processed"),
				reference_doctype=result.get("reference_doctype") or log.reference_doctype,
				reference_docname=result.get("reference_name") or log.reference_docname,
			)
		except Exception:
			frappe.db.rollback(save_point="stripe_webhook_retry")
			mark_failed(log_name, frappe.get_traceback())

	def get_health_warnings(self):
		"""Setup gaps that would destabilise settlement in production."""
		warnings = []
		if not self.webhook_secret:
			warnings.append(_("Webhook signing secret is not configured on {0}").format(self.name))
		if not self.webhook_endpoint:
			warnings.append(_("Webhook endpoint is not registered on {0}").format(self.name))
		return warnings

	def is_webhook_configured(self):
		"""Structured webhook signal for payment_core (language-safe).

		Consumers must not match get_health_warnings() text — it is
		translated. This reports the signing-secret fact directly; settlement
		itself still needs the endpoint registered (see health warnings).
		"""
		return bool(self.webhook_secret)

	def refund_transaction(
		self, transaction_name: str, amount: float, reason: str = "", idempotency_key: str | None = None
	):
		"""Refund a transaction's PaymentIntent at Stripe; returns the refund id.

		Ledger-only: gateway-only sites refund through their own flow.
		"""
		if not frappe.db.table_exists("Payment Transaction"):
			frappe.throw(_("Ledger refunds need the shared Payment Transaction"))
		row = frappe.db.get_value(
			"Payment Transaction", transaction_name, ["provider_transaction_id", "currency"], as_dict=True
		)
		intent_id = row.provider_transaction_id if row else None
		if not (intent_id or "").startswith("pi_"):
			frappe.throw(_("Transaction {0} has no Stripe PaymentIntent to refund").format(transaction_name))

		client = get_stripe_client(self)
		payload = {
			"payment_intent": intent_id,
			"amount": to_minor_units(flt(amount), row.currency if row else None),
		}
		request_options = {"idempotency_key": idempotency_key} if idempotency_key else {}
		refund = client.refunds.create(payload, request_options)
		return refund.get("id")

	def reconcile_transaction(self, transaction_name: str):
		"""Re-confirm an open Payment Transaction against Stripe.

		Called by payment_core when the buyer lands on the shared success page:
		the session can still read as processing at redirect time. Idempotent —
		settlement runs through the same finalize path as the webhook.
		Ledger-only: gateway-only sites settle through their own flow.
		"""
		if not frappe.db.table_exists("Payment Transaction"):
			return None
		row = frappe.db.get_value(
			"Payment Transaction",
			transaction_name,
			["provider_transaction_id", "checkout_url", "reference_doctype", "reference_docname"],
			as_dict=True,
		)
		provider_id = (row.provider_transaction_id if row else "") or ""
		if provider_id.startswith("pi_"):
			# Elements (PaymentIntent-only): reconcile via the intent path.
			# The controller payment_core builds here (get_payment_gateway_controller)
			# is fresh from the DB with no session ``data``, and the intent
			# path reads it immediately (assert_intent_matches_reference) —
			# seed it from the transaction row, the trusted record of what
			# this intent must belong to.
			self.data = frappe._dict(
				reference_doctype=row.reference_doctype if row else None,
				reference_docname=row.reference_docname if row else None,
				payment_transaction=transaction_name,
			)
			return payment_intents.finalize_payment_intent_by_id(self, provider_id)
		session_id = provider_id
		if not session_id.startswith("cs_"):
			checkout_url = (row.checkout_url if row else "") or ""
			session_id = checkout_url.split("/pay/", 1)[1].split("#")[0] if "/pay/" in checkout_url else ""
		if not session_id.startswith("cs_"):
			return None
		return checkout.finalize_checkout_session(self, session_id)

	def create_request(self, data):
		return payment_intents.create_request(self, data)

	def create_payment_intent_on_stripe(self):
		return payment_intents.create_payment_intent_on_stripe(self)

	def handle_payment_intent_status(self, intent):
		return payment_intents.handle_payment_intent_status(self, intent)

	def finalize_payment_intent_by_id(self, pi_id):
		return payment_intents.finalize_payment_intent_by_id(self, pi_id)

	def finalize_payment_intent(self, intent, integration_request=None):
		return payment_intents.finalize_payment_intent(self, intent, integration_request)

	def get_party_for_reference(self, data):
		return customers.get_party_for_reference(data)

	def resolve_stripe_customer(self, stripe, data):
		return customers.resolve_stripe_customer(stripe, data)

	def create_charge_on_stripe(self):
		# Deprecated Charges API shim; delegates to PaymentIntents (supports save_card).
		return payment_intents.create_payment_intent_on_stripe(self)

	def authorize_reference(self):
		"""Settle the paid reference via payment_core's generic dispatcher.

		Payment Request is special: it needs this controller's live
		``integration_request`` for ``get_charged_amount`` and
		``annotate_payment_entry`` (a fresh controller from the DB has no
		session context), so it routes through ``self.settle_payment_request``
		instead of the generic dispatcher.

		The ``reference_authorized`` flag is claimed only AFTER a successful
		dispatch. ``finalize_request`` swallows handler exceptions, so claiming
		it before would permanently block ``on_payment_authorized`` for a
		reference whose first settlement attempt failed; claiming it after
		gives the same exactly-once result for a successful run (payment_core's
		internal authorize, triggered later by the transaction sync, checks the
		same flag) while a failed run keeps its retry chances.
		"""
		transaction_name = self.data.get("payment_transaction")
		if (
			transaction_name
			and frappe.db.table_exists("Payment Transaction")
			and frappe.db.get_value("Payment Transaction", transaction_name, "reference_authorized")
		):
			return None
		if self.data.reference_doctype == "Payment Request" and frappe.db.table_exists("Payment Request"):
			pr = frappe.get_doc("Payment Request", self.data.reference_docname)
			if pr.docstatus == 1 and pr.status != "Paid":
				self.settle_payment_request(pr)
			result = None
		else:
			result = authorize_reference(
				self.data.reference_doctype, self.data.reference_docname, self.flags.status_changed_to
			)
		# claim the flag after the app's handler ran: payment_core's second
		# authorize (from update_payment_transaction seeing the Completed
		# sync) must not re-run on_payment_authorized on the reference document
		if transaction_name and frappe.db.table_exists("Payment Transaction"):
			frappe.db.set_value(
				"Payment Transaction", transaction_name, "reference_authorized", 1, update_modified=False
			)
		return result

	def settle_payment_request(self, pr):
		"""Mark a submitted Payment Request paid and create its Payment Entry.

		The accounting lives in payment_core (its built-in ERPNext
		settlement); this controller participates through the charged-amount
		and payment-entry annotation seams below.
		"""
		return settle_payment_request(pr, controller=self)

	def get_charged_amount(self, pr):
		"""Amount Stripe actually charged, in major units, for this settlement.

		Reads the Integration Request created for the checkout attempt, so the
		Payment Entry books the real charge even when it differs from the
		Payment Request's outstanding amount.
		"""
		intent_id = getattr(getattr(self, "integration_request", None), "output", None)
		return payment_intents._get_charged_amount(self, intent_id)

	def annotate_payment_entry(self, payment_entry, pr):
		"""Stamp the PaymentIntent and owning settings onto the PE for refund lookups."""
		intent_id = getattr(getattr(self, "integration_request", None), "output", None)
		if intent_id and payment_entry and payment_entry.meta.has_field("stripe_payment_intent"):
			updates = {"stripe_payment_intent": intent_id}
			if payment_entry.meta.has_field("stripe_settings"):
				updates["stripe_settings"] = self.name
			frappe.db.set_value("Payment Entry", payment_entry.name, updates, update_modified=False)

	def finalize_request(self):
		redirect_to = self.data.get("redirect_to") or None
		redirect_message = self.data.get("redirect_message") or None
		status = self.integration_request.status
		redirect_url = "payment-failed"

		if self.flags.status_changed_to == "Completed":
			if self.data.reference_doctype and self.data.reference_docname:
				custom_redirect_to = None
				try:
					custom_redirect_to = self.authorize_reference()
				except Exception:
					frappe.log_error(frappe.get_traceback())

				if custom_redirect_to:
					redirect_to = custom_redirect_to

				redirect_url = f"payment-success?doctype={self.data.reference_doctype}&docname={self.data.reference_docname}"

			if self.redirect_url:
				redirect_url = self.redirect_url
				redirect_to = None

		if redirect_to:
			redirect_url += ("&" if "?" in redirect_url else "?") + urlencode({"redirect_to": redirect_to})

		if redirect_message:
			redirect_url += "&" + urlencode({"redirect_message": redirect_message})

		return {"redirect_to": redirect_url, "status": status}


def get_gateway_controller(doctype, docname, payment_gateway=None):
	return get_gateway_controller_name(doctype, docname, payment_gateway)


def clear_webhook_secret_cache():
	clear_webhook_cache()


def clear_api_key_cache():
	clear_client_api_key_cache()


@frappe.whitelist(allow_guest=True)  # nosemgrep: frappe-semgrep-rules.rules.security.guest-whitelisted-method
def checkout_success(session_id: str, gateway: str):
	"""Return landing for Hosted Checkout — verify the session, then redirect."""
	return checkout.checkout_success(session_id, gateway)
