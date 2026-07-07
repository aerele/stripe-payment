# Copyright (c) 2017, Frappe Technologies and contributors
# License: MIT. See LICENSE
#
# Stripe Settings controller. Keeps the legacy V1 method contract; the logic lives
# in stripe_payment.gateway.* and is reached through thin delegators here.

from types import MappingProxyType
from urllib.parse import urlencode

import frappe
from frappe import _
from frappe.integrations.utils import make_get_request
from frappe.model.document import Document
from frappe.utils import call_hook_method, flt, get_url
from payment_core.api.controllers import get_gateway_controller_name
from payment_core.api.gateway import GatewayControllerMixin
from payment_core.utils import create_payment_gateway

from stripe_payment.gateway import checkout, customers, payment_intents, references, refunds

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
		if not self.flags.ignore_mandatory:
			self.validate_stripe_credentails()
		self.set_webhook_endpoint()
		clear_webhook_secret_cache()

	def on_trash(self):
		clear_webhook_secret_cache()

	def set_webhook_endpoint(self):
		"""Show the admin which URL to register as a Stripe webhook endpoint."""
		endpoint = get_url("/api/method/stripe_payment.stripe.doctype.stripe_settings.webhooks")
		if self.webhook_endpoint != endpoint:
			self.db_set("webhook_endpoint", endpoint, update_modified=False)

	def validate_stripe_credentails(self):
		if self.publishable_key and self.secret_key:
			header = {
				"Authorization": "Bearer {}".format(
					self.get_password(fieldname="secret_key", raise_exception=False)
				)
			}
			try:
				# PaymentIntents is the current API; /v1/charges is the deprecated one.
				make_get_request(url="https://api.stripe.com/v1/payment_intents?limit=1", headers=header)
			except Exception:
				frappe.throw(_("Seems Publishable Key or Secret Key is wrong !!!"))

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

	# --- V1 controller contract (delegates to stripe_payment.gateway.*) ---

	def get_payment_url(self, **kwargs):
		return checkout.get_payment_url(self, **kwargs)

	def is_subscription_reference(self, data):
		return references.is_subscription_reference(data)

	def get_stripe_metadata(self, data=None, integration_request=None):
		return references.get_stripe_metadata(self, data=data, integration_request=integration_request)

	def get_party_for_reference(self, data):
		return customers.get_party_for_reference(data)

	def resolve_stripe_customer(self, stripe, data):
		return customers.resolve_stripe_customer(stripe, data)

	def create_setup_intent_for_card(self, data):
		return payment_intents.create_setup_intent_for_card(self, data)

	def refund_intent(self, payment_intent, amount=None):
		return refunds.refund_intent(self, payment_intent, amount)

	def create_request(self, data):
		return payment_intents.create_request(self, data)

	def create_payment_intent_for_checkout(self, data):
		return payment_intents.create_payment_intent_for_checkout(self, data)

	def create_payment_intent_on_stripe(self):
		return payment_intents.create_payment_intent_on_stripe(self)

	def handle_payment_intent_status(self, intent):
		return payment_intents.handle_payment_intent_status(self, intent)

	def finalize_payment_intent_by_id(self, pi_id):
		return payment_intents.finalize_payment_intent_by_id(self, pi_id)

	def finalize_payment_intent(self, intent, integration_request=None):
		return payment_intents.finalize_payment_intent(self, intent, integration_request)

	def enable_setup_future_usage(self, payment_intent, client_secret, reference_doctype, reference_docname):
		return payment_intents.enable_setup_future_usage(
			self, payment_intent, client_secret, reference_doctype, reference_docname
		)

	def create_checkout_session(self, data):
		return checkout.create_checkout_session(self, data)

	def finalize_checkout_session(self, session_id):
		return checkout.finalize_checkout_session(self, session_id)

	def create_charge_on_stripe(self):
		# Deprecated Charges API shim; delegates to PaymentIntents.
		return payment_intents.create_payment_intent_on_stripe(self)

	# --- ERPNext settlement (stays here: service modules call back into these) ---

	def authorize_reference(self):
		"""Settle the paid reference document.

		Custom doctypes that still define the legacy ``on_payment_authorized`` hook
		keep working. Newer ERPNext removed it from Payment Request, so a Payment
		Request is settled explicitly (status -> Paid + Payment Entry).
		"""
		ref = frappe.get_doc(self.data.reference_doctype, self.data.reference_docname)
		if hasattr(ref, "on_payment_authorized"):
			return ref.run_method("on_payment_authorized", self.flags.status_changed_to)
		if ref.doctype == "Payment Request":
			self.settle_payment_request(ref)
		return None

	def settle_payment_request(self, pr):
		"""Mark a submitted Payment Request paid and create its Payment Entry.

		Idempotent: skips if the request is already paid or a Payment Entry already
		exists for the underlying invoice. The Payment Entry's submission is what
		flips the Payment Request status to "Paid" (via ERPNext's PE -> PR sync).
		"""
		# Serialize concurrent settlements of the SAME Payment Request (two tabs / a
		# retried session, each with its own Integration Request). claim_integration_request
		# only locks the per-IR row, so without this both flows could book a Payment Entry.
		locked_status = frappe.db.get_value("Payment Request", pr.name, "status", for_update=True)
		if pr.docstatus != 1 or locked_status == "Paid":
			return
		if pr.payment_channel == "Phone":
			pr.db_set({"status": "Paid", "outstanding_amount": 0})
			return

		from payment_core.utils import erpnext_app_import_guard

		with erpnext_app_import_guard():
			from erpnext.accounts.doctype.payment_request.payment_request import (
				get_existing_payment_entry,
			)

		if pr.reference_name and get_existing_payment_entry(pr.reference_name):
			return  # the invoice is already settled by a Payment Entry
		if not pr.reference_name and frappe.db.exists(
			"Payment Entry", {"reference_no": pr.name, "docstatus": 1}
		):
			return  # a reference-less request already settled (PE keyed on the request name)

		# Settle as Administrator: the Guest checkout return / webhook can't read the Sales Invoice.
		original_user = frappe.session.user
		try:
			frappe.set_user("Administrator")
			payment_entry = pr.set_as_paid()
		finally:
			frappe.set_user(original_user)

		# Stamp the PaymentIntent onto the new PE (needed for refunds).
		intent_id = getattr(getattr(self, "integration_request", None), "output", None)
		if intent_id and payment_entry and payment_entry.meta.has_field("stripe_payment_intent"):
			frappe.db.set_value(
				"Payment Entry", payment_entry.name, "stripe_payment_intent", intent_id, update_modified=False
			)

	def finalize_request(self):
		redirect_to = self.data.get("redirect_to") or None
		redirect_message = self.data.get("redirect_message") or None
		status = self.integration_request.status
		redirect_url = "payment-success"

		if self.flags.status_changed_to == "Completed":
			if self.data.reference_doctype and self.data.reference_docname:
				# Settlement errors must NOT be swallowed. On the webhook path they
				# propagate to handle_event (rollback -> Failed -> Stripe retry); the
				# browser entry points roll back the claim and show a neutral result.
				custom_redirect_to = self.authorize_reference()

				if custom_redirect_to:
					redirect_to = custom_redirect_to

				redirect_url = f"payment-success?doctype={self.data.reference_doctype}&docname={self.data.reference_docname}"

			if self.redirect_url:
				redirect_url = self.redirect_url
				redirect_to = None
		else:
			redirect_url = "payment-failed"

		if redirect_to:
			redirect_url += ("&" if "?" in redirect_url else "?") + urlencode({"redirect_to": redirect_to})

		if redirect_message:
			redirect_url += "&" + urlencode({"redirect_message": redirect_message})

		return {"redirect_to": redirect_url, "status": status}


def get_gateway_controller(doctype, docname, payment_gateway=None):
	return get_gateway_controller_name(doctype, docname, payment_gateway)


def clear_webhook_secret_cache():
	# Single source of the cache key lives in gateway.webhooks (the reader).
	from stripe_payment.gateway.webhooks import clear_cache

	clear_cache()


@frappe.whitelist()
def refund_payment_entry(payment_entry: str, amount: float | None = None):
	"""Refund a Stripe-originated Payment Entry. Books are updated by the webhook."""
	return refunds.refund_payment_entry(payment_entry, amount)


@frappe.whitelist(allow_guest=True)
def checkout_success(session_id: str, gateway: str):
	"""Return landing for Hosted Checkout — verify the session, then redirect."""
	return checkout.checkout_success(session_id, gateway)
