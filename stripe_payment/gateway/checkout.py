# Copyright (c) Frappe Technologies Pvt. Ltd. and contributors
# License: MIT. See LICENSE
#
# Hosted Checkout: session creation, get_payment_url routing, and the return handler.
# Payment-mode only (subscription mode is a separate feature PR).

from urllib.parse import quote, urlencode

import frappe
from frappe import _
from frappe.integrations.utils import create_request_log
from frappe.utils import get_url
from payment_core.utils import get_reference_amount, guard_payment_reference

from stripe_payment.gateway.client import get_stripe_client, to_minor_units
from stripe_payment.gateway.payment_intents import claim_integration_request
from stripe_payment.gateway.references import get_stripe_metadata, success_redirect


def get_payment_url(settings, **kwargs):
	"""Route to Hosted Checkout or on-site stripe_checkout based on settings."""
	mode = settings.checkout_mode or "Hosted Checkout"
	if mode == "Hosted Checkout":
		return create_checkout_session(settings, kwargs)
	# Embedded / legacy on-site page (upgraded by the Embedded Checkout PR).
	return get_url(f"./stripe_checkout?{urlencode(kwargs)}")


def create_checkout_session(settings, data):
	"""Hosted Checkout: build a Stripe-hosted payment page and return its URL."""
	data = frappe._dict(data)
	# Never trust a client-supplied amount/currency or an unvalidated reference.
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
			"payment_intent_data": {"metadata": metadata},
			"metadata": metadata,
		}
	)

	integration_request.db_set("output", session.id, update_modified=False)
	return session.url


def finalize_checkout_session(settings, session_id):
	"""Confirm a Hosted Checkout session and run on_payment_authorized.

	Idempotent: callable from both the success redirect and a later webhook.
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

	if session.get("payment_status") not in ("paid", "no_payment_required"):
		if session.get("status") == "complete":
			# Async method (bank debit): session complete, settlement deferred.
			return {"redirect_to": success_redirect(metadata), "status": "Pending"}
		return {"redirect_to": "payment-failed", "status": "Failed"}

	output_id = session.get("payment_intent") or session_id

	if not claim_integration_request(settings):
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
	"""Return landing for Hosted Checkout — verify the session, then redirect."""
	if not frappe.db.exists("Stripe Settings", gateway):
		frappe.local.response["type"] = "redirect"
		frappe.local.response["location"] = "/payment-failed"
		return

	result = {}
	try:
		settings = frappe.get_doc("Stripe Settings", gateway)
		result = finalize_checkout_session(settings, session_id) or {}
		# Guest return URL must persist settlement before the redirect response.
		frappe.db.commit()  # nosemgrep: frappe-semgrep-rules.rules.frappe-manual-commit
	except Exception:
		frappe.log_error(frappe.get_traceback(), "Stripe checkout return failed")

	redirect_url = result.get("redirect_to") or ("payment-success" if result else "payment-failed")
	frappe.local.response["type"] = "redirect"
	frappe.local.response["location"] = "/" + redirect_url.lstrip("/")
