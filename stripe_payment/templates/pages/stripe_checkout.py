# Copyright (c) 2021, Frappe Technologies Pvt. Ltd. and Contributors
# License: MIT. See LICENSE
import json

import frappe
from frappe import _
from frappe.utils import cint, fmt_money
from payment_core.utils import get_reference_amount, guard_payment_reference

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

	# all these keys exist in form_dict
	if not (set(expected_keys) - set(list(frappe.form_dict))):
		for key in expected_keys:
			context[key] = frappe.form_dict[key]
		gateway_controller = get_gateway_controller(
			context.reference_doctype, context.reference_docname, context.payment_gateway
		)
		context.publishable_key = get_api_key(context.reference_docname, gateway_controller)
		context.image = get_header_image(context.reference_docname, gateway_controller)

		context["amount"] = fmt_money(amount=context["amount"], currency=context["currency"])

		if is_a_subscription(context.reference_doctype, context.reference_docname):
			# Read plans from the same source the payment flow charges against
			# (Payment Request.subscription_plans) so the displayed recurrence
			# can't diverge from what is actually billed.
			plans = frappe.get_all(
				"Subscription Plan Detail",
				filters={
					"parent": context.reference_docname,
					"parenttype": context.reference_doctype,
					"parentfield": "subscription_plans",
				},
				pluck="plan",
			)
			if plans:
				# The amount covers every plan, so only append a recurrence label when
				# all plans share one cadence; a single label can't honestly describe
				# a mixed-interval total. One query for every plan's cadence.
				cadences = {
					(row.billing_interval, cint(row.billing_interval_count) or 1)
					for row in frappe.get_all(
						"Subscription Plan",
						filters={"name": ("in", plans)},
						fields=["billing_interval", "billing_interval_count"],
					)
				}
				if len(cadences) == 1:
					billing_interval, billing_interval_count = cadences.pop()
					singular = {
						"Day": _("daily"),
						"Week": _("weekly"),
						"Month": _("monthly"),
						"Year": _("yearly"),
					}
					plural = {"Day": _("days"), "Week": _("weeks"), "Month": _("months"), "Year": _("years")}
					if billing_interval_count == 1:
						recurrence = singular.get(billing_interval, "")
					else:
						recurrence = _("every {0} {1}").format(
							billing_interval_count, plural.get(billing_interval, "")
						)

					if recurrence:
						context["amount"] = context["amount"] + " " + recurrence
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
	"""Embedded Elements: create an unconfirmed PaymentIntent, return its client_secret.

	The PaymentElement confirms it client-side (handling 3DS); make_payment then
	verifies it server-side.
	"""
	guard_payment_reference(reference_doctype, reference_docname)
	data = json.loads(data)
	# Amount/reference are authoritative server-side and bind the intent to this order.
	data["reference_doctype"] = reference_doctype
	data["reference_docname"] = reference_docname
	data["amount"], currency = get_reference_amount(reference_doctype, reference_docname)
	if currency:
		data["currency"] = currency
	gateway_controller = get_gateway_controller(reference_doctype, reference_docname, payment_gateway)
	settings = frappe.get_doc("Stripe Settings", gateway_controller)
	result = settings.create_payment_intent_for_checkout(frappe._dict(data))
	frappe.db.commit()
	return result


@frappe.whitelist()
def save_card(
	data: str,
	reference_doctype: str | None = None,
	reference_docname: str | None = None,
	payment_gateway: str | None = None,
):
	"""Create a SetupIntent so a card can be saved off-session for later reuse.

	Unlike the one-off checkout (which guests legitimately complete), storing a
	card is a standing capability to charge later, so this requires an authenticated
	user. Omitting allow_guest makes the framework reject Guest callers; the guard
	below then enforces read access on the reference for that logged-in user.
	"""
	guard_payment_reference(reference_doctype, reference_docname)
	data = json.loads(data)
	gateway_controller = get_gateway_controller(reference_doctype, reference_docname, payment_gateway)
	settings = frappe.get_doc("Stripe Settings", gateway_controller)
	result = settings.create_setup_intent_for_card(frappe._dict(data))
	frappe.db.commit()
	return result


@frappe.whitelist(allow_guest=True)  # nosemgrep: frappe-semgrep-rules.rules.security.guest-whitelisted-method
def set_card_consent(
	payment_intent: str,
	client_secret: str | None = None,
	reference_doctype: str | None = None,
	reference_docname: str | None = None,
	payment_gateway: str | None = None,
):
	"""Record "save my card" consent on the checkout PaymentIntent (embedded flow)."""
	guard_payment_reference(reference_doctype, reference_docname)
	gateway_controller = get_gateway_controller(reference_doctype, reference_docname, payment_gateway)
	settings = frappe.get_doc("Stripe Settings", gateway_controller)
	result = settings.enable_setup_future_usage(
		payment_intent, client_secret, reference_doctype, reference_docname
	)
	frappe.db.commit()
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
	data = json.loads(data)
	# Reference + amount/currency are authoritative server-side, never the client.
	data["reference_doctype"] = reference_doctype
	data["reference_docname"] = reference_docname
	data["amount"], currency = get_reference_amount(reference_doctype, reference_docname)
	if currency:
		data["currency"] = currency

	if payment_intent:
		data.update({"payment_intent": payment_intent})
	if stripe_token_id:
		# Backward-compat with the pre-PaymentIntents checkout JS.
		data.update({"stripe_token_id": stripe_token_id})

	gateway_controller = get_gateway_controller(reference_doctype, reference_docname, payment_gateway)

	if is_a_subscription(reference_doctype, reference_docname):
		reference = frappe.get_doc(reference_doctype, reference_docname)
		data = reference.create_subscription("stripe", gateway_controller, data)
	else:
		data = frappe.get_doc("Stripe Settings", gateway_controller).create_request(data)

	frappe.db.commit()
	return data


def is_a_subscription(reference_doctype, reference_docname):
	if not frappe.get_meta(reference_doctype).has_field("is_a_subscription"):
		return False
	return frappe.db.get_value(reference_doctype, reference_docname, "is_a_subscription")
