# Copyright (c) Frappe Technologies Pvt. Ltd. and contributors
# License: MIT. See LICENSE
#
# Hosted Checkout: session creation, get_payment_url routing, and the return handler.

from urllib.parse import quote, urlencode

import frappe
from frappe import _
from frappe.integrations.utils import create_request_log
from frappe.utils import get_url

from stripe_payment.gateway.client import get_stripe_client, to_minor_units
from stripe_payment.gateway.customers import get_party_for_reference, resolve_stripe_customer
from stripe_payment.gateway.payment_intents import claim_integration_request
from stripe_payment.gateway.references import (
	get_stripe_metadata,
	is_subscription_reference,
	success_redirect,
)
from stripe_payment.gateway.subscriptions import find_erpnext_subscription, get_subscription_line_items


def get_payment_url(settings, **kwargs):
	# Subscriptions always use Hosted Checkout (native recurring billing).
	if is_subscription_reference(kwargs):
		return create_checkout_session(settings, kwargs)
	if (settings.checkout_mode or "Hosted Checkout") == "Hosted Checkout":
		return create_checkout_session(settings, kwargs)
	# Embedded Elements: render the on-site card form (PaymentElement).
	return get_url(f"./stripe_checkout?{urlencode(kwargs)}")


def create_checkout_session(settings, data):
	"""Hosted Checkout: build a Stripe-hosted payment page and return its URL."""
	# Local import avoids a circular import (stripe_checkout imports this module).
	from stripe_payment.templates.pages.stripe_checkout import get_reference_amount, guard_payment_reference

	data = frappe._dict(data)
	# Same guard + server-side amount the embedded path uses: never trust a
	# client-supplied amount/currency or an unvalidated reference.
	guard_payment_reference(data.reference_doctype, data.reference_docname)
	data.amount, currency = get_reference_amount(data.reference_doctype, data.reference_docname)
	if currency:
		data.currency = currency
	client = get_stripe_client(settings)
	integration_request = create_request_log(data, service_name="Stripe")

	success_url = get_url(
		"/api/method/stripe_payment.stripe.doctype.stripe_settings.stripe_settings.checkout_success"
		+ "?session_id={CHECKOUT_SESSION_ID}&gateway="
		+ quote(settings.name)
	)
	metadata = get_stripe_metadata(settings, data=data, integration_request=integration_request.name)
	customer_id = resolve_stripe_customer(client, data)

	if is_subscription_reference(data):
		session = _create_subscription_checkout(settings, client, data, customer_id, metadata, success_url)
	else:
		session = client.checkout.sessions.create(
			{
				"mode": "payment",
				"line_items": [
					{
						"price_data": {
							"currency": (data.currency or "").lower(),
							"unit_amount": to_minor_units(data.amount, data.currency),
							"product_data": {
								"name": data.get("description") or data.get("title") or _("Payment")
							},
						},
						"quantity": 1,
					}
				],
				"success_url": success_url,
				"cancel_url": get_url("payment-failed"),
				"client_reference_id": data.get("reference_docname"),
				"customer": customer_id,
				"payment_intent_data": {"metadata": metadata},
				"metadata": metadata,
				# Stripe renders its own opt-in checkbox; card saved only if the buyer ticks it.
				"saved_payment_method_options": {"payment_method_save": "enabled"},
			}
		)

	integration_request.db_set("output", session.id, update_modified=False)
	return session.url


def _create_subscription_checkout(settings, client, data, customer_id, metadata, success_url):
	"""Hosted Checkout in subscription mode (both billing models)."""
	dt, dn = data.get("reference_doctype"), data.get("reference_docname")
	line_items = get_subscription_line_items(dt, dn)

	party = get_party_for_reference(data)
	plan_names = [
		row.plan
		for row in frappe.get_all(
			"Subscription Plan Detail",
			filters={"parent": dn, "parenttype": dt},
			fields=["plan"],
		)
	]
	sub_metadata = dict(metadata)
	erpnext_sub = find_erpnext_subscription(party, plan_names)
	if erpnext_sub:
		sub_metadata["erpnext_subscription"] = erpnext_sub
	if party:
		sub_metadata["erpnext_customer"] = party

	subscription_data = {"metadata": sub_metadata}

	if (settings.subscription_billing_model or "") == "Charge Now + Defer First Cycle":
		frappe.throw(_("The 'Charge Now + Defer First Cycle' subscription billing model is not supported."))

	return client.checkout.sessions.create(
		{
			"mode": "subscription",
			"line_items": line_items,
			"success_url": success_url,
			"cancel_url": get_url("payment-failed"),
			"client_reference_id": dn,
			"customer": customer_id,
			"metadata": sub_metadata,
			"subscription_data": subscription_data,
		}
	)


def finalize_checkout_session(settings, session_id):
	"""Confirm a Hosted Checkout session and run on_payment_authorized.

	Idempotent: callable from both the success redirect and the webhook.
	"""
	client = get_stripe_client(settings)
	session = client.checkout.sessions.retrieve(session_id)
	metadata = dict(session.get("metadata") or {})

	ir_name = metadata.get("integration_request")
	if ir_name and frappe.db.exists("Integration Request", ir_name):
		settings.integration_request = frappe.get_doc("Integration Request", ir_name)
	else:
		ir = frappe.db.get_value("Integration Request", {"output": session_id}, "name")
		settings.integration_request = (
			frappe.get_doc("Integration Request", ir)
			if ir
			else create_request_log(session, service_name="Stripe")
		)

	if settings.integration_request.status == "Completed":
		return {"redirect_to": success_redirect(metadata), "status": "Completed"}

	if session.get("payment_status") != "paid":
		return {"redirect_to": "payment-failed", "status": "Failed"}

	output_id = session.get("payment_intent")
	if not output_id and session.get("subscription"):
		# Subscription-mode sessions carry no top-level PaymentIntent; stamp the
		# first invoice's PaymentIntent so the resulting Payment Entry is refundable.
		sub = client.subscriptions.retrieve(
			session.get("subscription"), {"expand": ["latest_invoice.payment_intent"]}
		)
		intent = getattr(getattr(sub, "latest_invoice", None), "payment_intent", None)
		output_id = intent.id if intent is not None else session.get("subscription")

	if not claim_integration_request(settings):
		# Lost the race to a concurrent webhook/redirect — already settled.
		return {"redirect_to": success_redirect(metadata), "status": "Completed"}

	settings.data = frappe._dict(
		{
			"reference_doctype": metadata.get("reference_doctype"),
			"reference_docname": metadata.get("reference_docname"),
		}
	)
	settings.integration_request.db_set("output", output_id, update_modified=False)
	settings.flags.status_changed_to = "Completed"
	return settings.finalize_request()


def checkout_success(session_id, gateway):
	"""Return landing for Hosted Checkout — verify the session, then redirect.

	The webhook (checkout.session.completed) is the authoritative backstop;
	finalize_checkout_session is idempotent so running both is safe.
	"""
	result = {}
	try:
		settings = frappe.get_doc("Stripe Settings", gateway)
		result = finalize_checkout_session(settings, session_id) or {}
		frappe.db.commit()
	except Exception:
		# Undo the claim so the checkout.session.completed webhook can re-settle.
		frappe.db.rollback()
		frappe.log_error(frappe.get_traceback(), "Stripe checkout return failed")

	redirect_url = result.get("redirect_to") or "payment-success"
	frappe.local.response["type"] = "redirect"
	frappe.local.response["location"] = "/" + redirect_url.lstrip("/")
