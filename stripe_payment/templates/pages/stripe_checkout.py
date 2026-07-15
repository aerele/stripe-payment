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


def get_context(context):
	context.no_cache = 1

	if not (set(expected_keys) - set(list(frappe.form_dict))):
		for key in expected_keys:
			context[key] = frappe.form_dict[key]
		gateway_controller = get_gateway_controller(
			context.reference_doctype, context.reference_docname, context.payment_gateway
		)
		context.publishable_key = get_api_key(context.reference_docname, gateway_controller)
		context.image = get_header_image(context.reference_docname, gateway_controller)
		context["amount"] = fmt_money(amount=context["amount"], currency=context["currency"])
	else:
		frappe.redirect_to_message(
			_("Some information is missing"),
			_("Looks like someone sent you to an incomplete URL. Please ask them to look into it."),
		)
		frappe.local.flags.redirect_location = frappe.local.response.location
		raise frappe.Redirect


def get_api_key(doc, gateway_controller):
	publishable_key = frappe.db.get_value("Stripe Settings", gateway_controller, "publishable_key")
	if cint(frappe.form_dict.get("use_sandbox")):
		publishable_key = frappe.conf.sandbox_publishable_key
	return publishable_key


def get_header_image(doc, gateway_controller):
	return frappe.db.get_value("Stripe Settings", gateway_controller, "header_img")


@frappe.whitelist(allow_guest=True)  # nosemgrep: frappe-semgrep-rules.rules.security.guest-whitelisted-method
def create_payment_intent(
	data: str,
	reference_doctype: str | None = None,
	reference_docname: str | None = None,
	payment_gateway: str | None = None,
):
	"""Embedded Elements: create an unconfirmed PaymentIntent, return its client_secret."""
	guard_payment_reference(reference_doctype, reference_docname)
	assert_reference_payable(reference_doctype, reference_docname)
	data = json.loads(data)
	data["reference_doctype"] = reference_doctype
	data["reference_docname"] = reference_docname
	data["amount"], currency = get_reference_amount(reference_doctype, reference_docname)
	if currency:
		data["currency"] = currency
	gateway_controller = get_gateway_controller(reference_doctype, reference_docname, payment_gateway)
	settings = frappe.get_doc("Stripe Settings", gateway_controller)
	result = settings.create_payment_intent_for_checkout(frappe._dict(data))
	# Guest request must persist the Integration Request / PI before the browser confirms.
	frappe.db.commit()  # nosemgrep: frappe-semgrep-rules.rules.frappe-manual-commit
	return result


@frappe.whitelist()
def save_card(
	data: str,
	reference_doctype: str | None = None,
	reference_docname: str | None = None,
	payment_gateway: str | None = None,
):
	"""Create a SetupIntent so a card can be saved off-session (authenticated users only)."""
	guard_payment_reference(reference_doctype, reference_docname)
	assert_reference_payable(reference_doctype, reference_docname)
	data = json.loads(data)
	data["reference_doctype"] = reference_doctype
	data["reference_docname"] = reference_docname
	gateway_controller = get_gateway_controller(reference_doctype, reference_docname, payment_gateway)
	settings = frappe.get_doc("Stripe Settings", gateway_controller)
	result = settings.create_setup_intent_for_card(frappe._dict(data))
	# Persist SetupIntent linkage before the client confirms the card.
	frappe.db.commit()  # nosemgrep: frappe-semgrep-rules.rules.frappe-manual-commit
	return result


@frappe.whitelist(allow_guest=True)  # nosemgrep: frappe-semgrep-rules.rules.security.guest-whitelisted-method
def set_card_consent(
	payment_intent: str,
	client_secret: str | None = None,
	reference_doctype: str | None = None,
	reference_docname: str | None = None,
	payment_gateway: str | None = None,
):
	"""Record save-my-card consent on an unconfirmed checkout PaymentIntent."""
	guard_payment_reference(reference_doctype, reference_docname)
	assert_reference_payable(reference_doctype, reference_docname)
	gateway_controller = get_gateway_controller(reference_doctype, reference_docname, payment_gateway)
	settings = frappe.get_doc("Stripe Settings", gateway_controller)
	result = settings.enable_setup_future_usage(
		payment_intent, client_secret, reference_doctype, reference_docname
	)
	# Persist setup_future_usage before the browser confirms payment.
	frappe.db.commit()  # nosemgrep: frappe-semgrep-rules.rules.frappe-manual-commit
	return result


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

	# Guest request must persist settlement before the redirect response is sent.
	frappe.db.commit()  # nosemgrep: frappe-semgrep-rules.rules.frappe-manual-commit
	return data


def is_a_subscription(reference_doctype, reference_docname):
	if not frappe.get_meta(reference_doctype).has_field("is_a_subscription"):
		return False
	return frappe.db.get_value(reference_doctype, reference_docname, "is_a_subscription")
