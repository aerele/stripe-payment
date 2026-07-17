# Copyright (c) 2021, Frappe Technologies Pvt. Ltd. and Contributors
# License: MIT. See LICENSE
#
# Embedded checkout page + guest APIs (PaymentIntent) and authenticated save_card.

import json

import frappe
from frappe import _
from frappe.utils import cint, fmt_money
from payment_core.utils import get_reference_amount, guard_payment_reference

from stripe_payment.gateway.references import assert_reference_payable
from stripe_payment.stripe.doctype.stripe_settings.stripe_settings import (
	get_gateway_controller,
)

no_cache = 1

expected_keys = (
	"amount",
	"title",
	"description",
	"reference_doctype",
	"reference_docname",
	"payer_name",
	"payer_email",
	"currency",
	"payment_gateway",
)


def get_context(context):  # Frappe web-page hook, called by the framework to render the page
	context.no_cache = 1

	if not (set(expected_keys) - set(list(frappe.form_dict))):
		for key in expected_keys:
			context[key] = frappe.form_dict[key]
		gateway_controller = get_gateway_controller(
			context.reference_doctype, context.reference_docname, context.payment_gateway
		)
		# Publishable key (sandbox-aware) + header image for the checkout page.
		publishable_key = frappe.db.get_value("Stripe Settings", gateway_controller, "publishable_key")
		if cint(frappe.form_dict.get("use_sandbox")):
			publishable_key = frappe.conf.sandbox_publishable_key
		context.publishable_key = publishable_key
		context.image = frappe.db.get_value("Stripe Settings", gateway_controller, "header_img")
		context["amount"] = fmt_money(amount=context["amount"], currency=context["currency"])
	else:
		frappe.redirect_to_message(
			_("Some information is missing"),
			_("Looks like someone sent you to an incomplete URL. Please ask them to look into it."),
		)
		frappe.local.flags.redirect_location = frappe.local.response.location
		raise frappe.Redirect


@frappe.whitelist(allow_guest=True)  # nosemgrep: frappe-semgrep-rules.rules.security.guest-whitelisted-method
def create_payment_intent(
	data: str,
	reference_doctype: str | None = None,
	reference_docname: str | None = None,
	payment_gateway: str | None = None,
):
	"""Embedded Elements: create an unconfirmed PaymentIntent, return its client_secret."""
	guard_payment_reference(reference_doctype, reference_docname)
	# Authenticated callers must own the reference; guests are validated for
	# existence/payability by guard_payment_reference (Payment Request grants
	# them no read perm, so has_permission would wrongly reject guest checkout).
	if frappe.session.user != "Guest":
		frappe.has_permission(reference_doctype, "read", reference_docname, throw=True)
	assert_reference_payable(reference_doctype, reference_docname)
	data = json.loads(data)
	data["reference_doctype"] = reference_doctype
	data["reference_docname"] = reference_docname
	data["amount"], currency = get_reference_amount(reference_doctype, reference_docname)
	if currency:
		data["currency"] = currency
	gateway_controller = get_gateway_controller(reference_doctype, reference_docname, payment_gateway)
	settings = frappe.get_doc("Stripe Settings", gateway_controller)
	# Frappe commits at request end, before the client secret reaches the browser.
	return settings.create_payment_intent_for_checkout(frappe._dict(data))


@frappe.whitelist()
def save_card(
	data: str,
	reference_doctype: str | None = None,
	reference_docname: str | None = None,
	payment_gateway: str | None = None,
):
	"""Create a SetupIntent so a card can be saved off-session (authenticated users only)."""
	guard_payment_reference(reference_doctype, reference_docname)
	# Authenticated-only endpoint: the caller must own the reference document.
	frappe.has_permission(reference_doctype, "read", reference_docname, throw=True)
	assert_reference_payable(reference_doctype, reference_docname)
	data = json.loads(data)
	data["reference_doctype"] = reference_doctype
	data["reference_docname"] = reference_docname
	gateway_controller = get_gateway_controller(reference_doctype, reference_docname, payment_gateway)
	settings = frappe.get_doc("Stripe Settings", gateway_controller)
	# Frappe commits the SetupIntent linkage at request end, before the client confirms.
	return settings.create_setup_intent_for_card(frappe._dict(data))


@frappe.whitelist()
def set_card_consent(
	payment_intent: str,
	client_secret: str | None = None,
	reference_doctype: str | None = None,
	reference_docname: str | None = None,
	payment_gateway: str | None = None,
):
	"""Record save-my-card consent on an unconfirmed checkout PaymentIntent.

	Authenticated-only: enabling off-session card reuse needs a user account to
	associate the saved card with, so a guest cannot opt a checkout into it.
	"""
	guard_payment_reference(reference_doctype, reference_docname)
	frappe.has_permission(reference_doctype, "read", reference_docname, throw=True)
	assert_reference_payable(reference_doctype, reference_docname)
	gateway_controller = get_gateway_controller(reference_doctype, reference_docname, payment_gateway)
	settings = frappe.get_doc("Stripe Settings", gateway_controller)
	# Frappe commits setup_future_usage at request end, before the browser confirms payment.
	return settings.enable_setup_future_usage(
		payment_intent, client_secret, reference_doctype, reference_docname
	)


@frappe.whitelist(allow_guest=True)  # nosemgrep: frappe-semgrep-rules.rules.security.guest-whitelisted-method
def make_payment(
	data: str,
	reference_doctype: str | None = None,
	reference_docname: str | None = None,
	payment_gateway: str | None = None,
	payment_intent: str | None = None,
	stripe_token_id: str | None = None,
):
	guard_payment_reference(reference_doctype, reference_docname)
	# Authenticated callers must own the reference; guests are validated for
	# existence/payability by the guards (Payment Request grants them no read perm).
	if frappe.session.user != "Guest":
		frappe.has_permission(reference_doctype, "read", reference_docname, throw=True)
	assert_reference_payable(reference_doctype, reference_docname)
	data = json.loads(data)
	data["reference_doctype"] = reference_doctype
	data["reference_docname"] = reference_docname
	data["amount"], currency = get_reference_amount(reference_doctype, reference_docname)
	if currency:
		data["currency"] = currency

	if payment_intent:
		data.update({"payment_intent": payment_intent})
	if stripe_token_id:
		data.update({"stripe_token_id": stripe_token_id})

	gateway_controller = get_gateway_controller(reference_doctype, reference_docname, payment_gateway)

	if is_a_subscription(reference_doctype, reference_docname):
		reference = frappe.get_doc(reference_doctype, reference_docname)
		data = reference.create_subscription("stripe", gateway_controller, data)
	else:
		data = frappe.get_doc("Stripe Settings", gateway_controller).create_request(data)

	# Frappe commits settlement at request end, before the redirect response is sent.
	return data


def is_a_subscription(reference_doctype, reference_docname):
	if not frappe.get_meta(reference_doctype).has_field("is_a_subscription"):
		return False
	return frappe.db.get_value(reference_doctype, reference_docname, "is_a_subscription")
