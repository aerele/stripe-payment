# Copyright (c) 2017, Frappe Technologies and contributors
# License: MIT. See LICENSE
#
# Stripe Settings controller — extracted from the monorepo payments app and
# rewired onto payment_core. Legacy Charges + card-token checkout.

from types import MappingProxyType
from urllib.parse import urlencode

import frappe
from frappe import _
from frappe.integrations.utils import create_request_log, make_get_request
from frappe.model.document import Document
from frappe.utils import call_hook_method, flt, get_url
from payment_core.api.controllers import get_gateway_controller_name
from payment_core.api.gateway import GatewayControllerMixin
from payment_core.utils import create_payment_gateway

from stripe_payment.gateway import customers

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

	def validate_stripe_credentails(self):
		if self.publishable_key and self.secret_key:
			header = {
				"Authorization": "Bearer {}".format(
					self.get_password(fieldname="secret_key", raise_exception=False)
				)
			}
			try:
				# PaymentIntents is the current API; /v1/charges is deprecated.
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

	def get_payment_url(self, **kwargs):
		return get_url(f"./stripe_checkout?{urlencode(kwargs)}")

	def create_request(self, data):
		self.data = frappe._dict(data)

		try:
			self.integration_request = create_request_log(self.data, service_name="Stripe")
			return self.create_charge_on_stripe()

		except Exception:
			frappe.log_error(frappe.get_traceback())
			return {
				"redirect_to": frappe.redirect_to_message(
					_("Server Error"),
					_(
						"It seems that there is an issue with the server's stripe configuration. In case of failure, the amount will get refunded to your account."
					),
				),
				"status": 401,
			}

	def get_party_for_reference(self, data):
		return customers.get_party_for_reference(data)

	def resolve_stripe_customer(self, stripe, data):
		return customers.resolve_stripe_customer(stripe, data)

	def create_charge_on_stripe(self):
		"""Legacy Charges API path (card token). Uses the shared Stripe client."""
		from stripe_payment.gateway.client import get_stripe_client, to_minor_units

		try:
			client = get_stripe_client(self)
			params = {
				"amount": to_minor_units(self.data.amount, self.data.currency),
				"currency": (self.data.currency or "").lower(),
				"source": self.data.stripe_token_id,
				"description": self.data.description,
				"receipt_email": self.data.payer_email,
			}
			customer_id = customers.resolve_stripe_customer(client, self.data)
			if customer_id:
				params["customer"] = customer_id
			charge = client.charges.create(params)

			if charge.captured is True:
				self.integration_request.db_set("status", "Completed", update_modified=False)
				self.flags.status_changed_to = "Completed"

			else:
				frappe.log_error(charge.failure_message, "Stripe Payment not completed")

		except Exception:
			frappe.log_error(frappe.get_traceback())

		return self.finalize_request()

	def authorize_reference(self):
		"""Settle the paid reference document.

		Custom doctypes that define ``on_payment_authorized`` keep working.
		Payment Request is settled explicitly when that hook is absent.
		"""
		ref = frappe.get_doc(self.data.reference_doctype, self.data.reference_docname)
		if hasattr(ref, "on_payment_authorized"):
			return ref.run_method("on_payment_authorized", self.flags.status_changed_to)
		if ref.doctype == "Payment Request":
			self.settle_payment_request(ref)
		return None

	def settle_payment_request(self, pr):
		"""Mark a submitted Payment Request paid and create its Payment Entry."""
		if pr.docstatus != 1 or pr.status == "Paid":
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
			return

		# Guest checkout cannot read/write Sales Invoice / PE; elevate for settlement only.
		original_user = frappe.session.user
		try:
			frappe.set_user("Administrator")  # nosemgrep
			pr.set_as_paid()
		finally:
			frappe.set_user(original_user)  # nosemgrep

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
