# Copyright (c) Frappe Technologies Pvt. Ltd. and contributors
# License: MIT. See LICENSE
#
# PaymentIntent create + finalize logic (Embedded Elements + server-confirm flows).
# Card-saving (SetupIntent / setup_future_usage) lands in a separate feature PR.

import frappe
import stripe
from frappe import _
from frappe.integrations.utils import create_request_log

from stripe_payment.gateway.client import get_stripe_client, idempotency_key, to_minor_units
from stripe_payment.gateway.references import (
	assert_reference_payable,
	get_stripe_metadata,
	success_redirect,
)


def create_request(settings, data):
	try:
		settings.data = frappe._dict(data)
		settings.stripe = get_stripe_client(settings)

		assert_reference_payable(
			settings.data.get("reference_doctype"), settings.data.get("reference_docname")
		)
		if settings.data.get("payment_intent"):
			# Embedded flow: reuse the intent's Integration Request to avoid double-settling.
			return finalize_payment_intent_by_id(settings, settings.data.get("payment_intent"))

		settings.integration_request = create_request_log(settings.data, service_name="Stripe")
		return create_payment_intent_on_stripe(settings)

	except frappe.ValidationError:
		# Paid / cancelled / ownership — surface the message, do not mask as config failure.
		raise
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


def create_payment_intent_for_checkout(settings, data):
	"""Embedded Elements: create an unconfirmed PaymentIntent and return its secret.

	Idempotent per reference: a reload/retry reuses the open PaymentIntent instead of
	orphaning Integration Requests and abandoning PaymentIntents.
	"""
	data = frappe._dict(data)
	assert_reference_payable(data.get("reference_doctype"), data.get("reference_docname"))
	client = get_stripe_client(settings)

	reused = _reuse_open_checkout_intent(client, data)
	if reused:
		return reused

	integration_request = create_request_log(data, service_name="Stripe")
	intent = client.payment_intents.create(
		{
			"amount": to_minor_units(data.amount, data.currency),
			"currency": (data.currency or "").lower(),
			"description": data.get("description"),
			"receipt_email": data.get("payer_email"),
			"metadata": get_stripe_metadata(
				settings, data=data, integration_request=integration_request.name
			),
			"automatic_payment_methods": {"enabled": True, "allow_redirects": "never"},
		}
	)
	integration_request.db_set("output", intent.id, update_modified=False)
	return {"client_secret": intent.client_secret, "payment_intent": intent.id}


def _reuse_open_checkout_intent(client, data):
	"""Return an existing open PaymentIntent for this reference, if still reusable."""
	reference_doctype = data.get("reference_doctype")
	reference_docname = data.get("reference_docname")
	if not (reference_doctype and reference_docname):
		return None
	rows = frappe.get_all(
		"Integration Request",
		filters={
			"integration_request_service": "Stripe",
			"status": "Queued",
			"reference_doctype": reference_doctype,
			"reference_docname": reference_docname,
		},
		fields=["output"],
		order_by="creation desc",
		limit=1,
	)
	if not rows or not rows[0].output:
		return None
	try:
		intent = client.payment_intents.retrieve(rows[0].output)
	except Exception:
		return None
	if intent.get("status") not in ("requires_payment_method", "requires_confirmation"):
		return None
	if intent.get("amount") != to_minor_units(data.amount, data.currency):
		return None
	return {"client_secret": intent.client_secret, "payment_intent": intent.id}


def create_payment_intent_on_stripe(settings):
	"""Confirm a one-off payment via PaymentIntents (SCA/3DS ready).

	Server-side create+confirm path (a direct payment_method or a legacy
	card token). The embedded flow goes through finalize_payment_intent_by_id.
	"""
	client = settings.stripe
	try:
		payment_method = settings.data.get("payment_method")
		if not payment_method and settings.data.get("stripe_token_id"):
			# Backward-compat: convert a legacy card token to a PaymentMethod.
			payment_method = client.payment_methods.create(
				{"type": "card", "card": {"token": settings.data.get("stripe_token_id")}}
			).id
		params = {
			"amount": to_minor_units(settings.data.amount, settings.data.currency),
			"currency": (settings.data.currency or "").lower(),
			"payment_method": payment_method,
			"confirm": bool(payment_method),
			"description": settings.data.get("description"),
			"receipt_email": settings.data.get("payer_email"),
			"metadata": get_stripe_metadata(settings, integration_request=settings.integration_request.name),
			"automatic_payment_methods": {"enabled": True, "allow_redirects": "never"},
		}
		intent = client.payment_intents.create(
			params,
			{
				"idempotency_key": idempotency_key(
					"pi", settings.data.get("reference_docname"), settings.data.get("amount"), payment_method
				)
			},
		)
		return handle_payment_intent_status(settings, intent)

	except stripe.error.CardError as e:
		settings.integration_request.db_set("status", "Failed", update_modified=False)
		settings.integration_request.db_set("error", frappe.as_json(e.json_body), update_modified=False)
		return {"redirect_to": "payment-failed", "status": "Failed", "error": e.user_message}


def handle_payment_intent_status(settings, intent):
	if intent.status == "succeeded":
		settings.integration_request.db_set("status", "Completed", update_modified=False)
		settings.integration_request.db_set("output", intent.id, update_modified=False)
		settings.flags.status_changed_to = "Completed"
		return settings.finalize_request()

	if intent.status in ("requires_action", "requires_confirmation"):
		return {
			"requires_action": True,
			"client_secret": intent.client_secret,
			"payment_intent": intent.id,
			"status": "Pending",
		}

	if intent.status == "processing":
		# Async method still settling; payment_intent.succeeded webhook can finalize later.
		return {"redirect_to": success_redirect(dict(intent.get("metadata") or {})), "status": "Pending"}

	settings.integration_request.db_set("status", "Failed", update_modified=False)
	return {"redirect_to": "payment-failed", "status": "Failed"}


def assert_intent_matches_reference(settings, intent):
	"""Bind a client-supplied PaymentIntent to the order being settled."""
	meta = dict(intent.get("metadata") or {})
	if (meta.get("reference_doctype"), meta.get("reference_docname")) != (
		settings.data.get("reference_doctype"),
		settings.data.get("reference_docname"),
	):
		frappe.throw(_("This payment does not belong to the order being settled."), frappe.PermissionError)


def claim_integration_request(settings):
	"""Atomically claim the Integration Request for settlement.

	Returns True only for the winning caller so finalize_request() runs once.
	"""
	status = frappe.db.get_value(
		"Integration Request", settings.integration_request.name, "status", for_update=True
	)
	if status == "Completed":
		return False
	settings.integration_request.db_set("status", "Completed", update_modified=False)
	return True


def finalize_payment_intent_by_id(settings, pi_id):
	"""Retrieve a (client-confirmed) PaymentIntent and finalize idempotently."""
	client = getattr(settings, "stripe", None) or get_stripe_client(settings)
	intent = client.payment_intents.retrieve(pi_id)
	assert_intent_matches_reference(settings, intent)

	if intent.status == "succeeded":
		return finalize_payment_intent(settings, intent)
	if intent.status in ("requires_action", "requires_confirmation"):
		return {
			"requires_action": True,
			"client_secret": intent.client_secret,
			"payment_intent": intent.id,
			"status": "Pending",
		}
	if intent.status == "processing":
		return {"redirect_to": success_redirect(dict(intent.get("metadata") or {})), "status": "Pending"}
	return {"redirect_to": "payment-failed", "status": "Failed"}


def finalize_payment_intent(settings, intent, integration_request=None):
	"""Mark the original Integration Request complete and run on_payment_authorized.

	Idempotent: callable from the synchronous return AND a later webhook.
	"""
	metadata = dict(intent.get("metadata") or {})
	ir_name = integration_request or metadata.get("integration_request")
	if ir_name and frappe.db.exists("Integration Request", ir_name):
		settings.integration_request = frappe.get_doc("Integration Request", ir_name)
	else:
		ir = frappe.db.get_value("Integration Request", {"output": intent.get("id")}, "name")
		settings.integration_request = (
			frappe.get_doc("Integration Request", ir)
			if ir
			else create_request_log(intent, service_name="Stripe")
		)

	if settings.integration_request.status == "Completed":
		return {"redirect_to": success_redirect(metadata), "status": "Completed"}

	if not claim_integration_request(settings):
		return {"redirect_to": success_redirect(metadata), "status": "Completed"}

	settings.data = frappe._dict(
		{
			"reference_doctype": metadata.get("reference_doctype"),
			"reference_docname": metadata.get("reference_docname"),
		}
	)
	settings.integration_request.db_set("output", intent.get("id"), update_modified=False)
	settings.flags.status_changed_to = "Completed"
	return settings.finalize_request()
