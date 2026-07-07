# Copyright (c) Frappe Technologies Pvt. Ltd. and contributors
# License: MIT. See LICENSE
#
# PaymentIntent / SetupIntent create + finalize logic (Embedded + server-confirm flows).

import hmac

import frappe
import stripe
from frappe import _
from frappe.integrations.utils import create_request_log

from stripe_payment.gateway.client import get_stripe_client, idempotency_key, to_minor_units
from stripe_payment.gateway.customers import resolve_stripe_customer
from stripe_payment.gateway.references import get_stripe_metadata, success_redirect


def create_request(settings, data):
	settings.data = frappe._dict(data)
	settings.stripe = get_stripe_client(settings)

	try:
		if settings.data.get("payment_intent"):
			# Embedded flow: reuse the intent's Integration Request to avoid double-settling.
			return finalize_payment_intent_by_id(settings, settings.data.get("payment_intent"))

		settings.integration_request = create_request_log(settings.data, service_name="Stripe")
		return create_payment_intent_on_stripe(settings)

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


def create_setup_intent_for_card(settings, data):
	"""SetupIntent to save a card off-session without charging (for later reuse)."""
	data = frappe._dict(data)
	client = get_stripe_client(settings)
	customer_id = resolve_stripe_customer(client, data)
	intent = client.setup_intents.create(
		{
			"customer": customer_id,
			"usage": "off_session",
			"metadata": get_stripe_metadata(settings, data=data),
		}
	)
	return {"client_secret": intent.client_secret, "setup_intent": intent.id, "customer": customer_id}


def create_payment_intent_for_checkout(settings, data):
	"""Embedded Elements: create an unconfirmed PaymentIntent and return its secret."""
	client = get_stripe_client(settings)
	integration_request = create_request_log(data, service_name="Stripe")
	customer_id = resolve_stripe_customer(client, data)
	intent = client.payment_intents.create(
		{
			"amount": to_minor_units(data.amount, data.currency),
			"currency": (data.currency or "").lower(),
			"description": data.get("description"),
			"receipt_email": data.get("payer_email"),
			"customer": customer_id,
			"metadata": get_stripe_metadata(
				settings, data=data, integration_request=integration_request.name
			),
			"automatic_payment_methods": {"enabled": True, "allow_redirects": "always"},
		}
	)
	integration_request.db_set("output", intent.id, update_modified=False)
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
		customer_id = resolve_stripe_customer(client, settings.data)
		params = {
			"amount": to_minor_units(settings.data.amount, settings.data.currency),
			"currency": (settings.data.currency or "").lower(),
			"payment_method": payment_method,
			"confirm": bool(payment_method),
			"description": settings.data.get("description"),
			"receipt_email": settings.data.get("payer_email"),
			"customer": customer_id,
			"metadata": get_stripe_metadata(settings, integration_request=settings.integration_request.name),
			"automatic_payment_methods": {"enabled": True, "allow_redirects": "always"},
		}
		# Store the card for off-session reuse only with explicit consent.
		if settings.data.get("save_card") and customer_id:
			params["setup_future_usage"] = "off_session"
		intent = client.payment_intents.create(
			params,
			# Key on the payment method so a retry de-dupes but a new attempt charges.
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
		# Card needs extra authentication (3DS) — let the client finish.
		return {
			"requires_action": True,
			"client_secret": intent.client_secret,
			"payment_intent": intent.id,
			"status": "Pending",
		}

	if intent.status == "processing":
		# Async method (e.g. bank transfer / delayed capture) still settling. Leave
		# the request pending — the payment_intent.succeeded webhook finalizes it.
		# Marking it Failed here would break these payments.
		return {"redirect_to": success_redirect(dict(intent.get("metadata") or {})), "status": "Pending"}

	# requires_payment_method / canceled
	settings.integration_request.db_set("status", "Failed", update_modified=False)
	return {"redirect_to": "payment-failed", "status": "Failed"}


def assert_intent_matches_reference(settings, intent):
	"""Bind a client-supplied PaymentIntent to the order being settled.

	Without this a caller could present a PaymentIntent that succeeded for a
	different (cheaper) order and settle this one for free. The reference in the
	intent's own metadata must match the reference we are about to settle.
	"""
	meta = dict(intent.get("metadata") or {})
	if (meta.get("reference_doctype"), meta.get("reference_docname")) != (
		settings.data.get("reference_doctype"),
		settings.data.get("reference_docname"),
	):
		frappe.throw(_("This payment does not belong to the order being settled."), frappe.PermissionError)


def claim_integration_request(settings):
	"""Atomically claim the Integration Request for settlement.

	The redirect and the webhook can arrive concurrently. A SELECT ... FOR UPDATE
	serialises them: the first flips status to Completed and settles; the second
	blocks, then sees Completed and backs off — so finalize_request() runs exactly
	once (no duplicate Payment Entry). Returns True only for the winning caller.
	"""
	status = frappe.db.get_value(
		"Integration Request", settings.integration_request.name, "status", for_update=True
	)
	if status == "Completed":
		return False
	settings.integration_request.db_set("status", "Completed", update_modified=False)
	return True


def enable_setup_future_usage(settings, payment_intent, client_secret, reference_doctype, reference_docname):
	"""Enable off-session reuse on an unconfirmed PaymentIntent (explicit consent)."""
	client = get_stripe_client(settings)
	intent = client.payment_intents.retrieve(payment_intent)
	# Ownership proof: only the browser that created the intent holds its client_secret.
	if not client_secret or not hmac.compare_digest(intent.client_secret or "", client_secret):
		frappe.throw(_("Invalid payment session."), frappe.PermissionError)
	settings.data = frappe._dict(
		{"reference_doctype": reference_doctype, "reference_docname": reference_docname}
	)
	assert_intent_matches_reference(settings, intent)
	if intent.status not in ("requires_payment_method", "requires_confirmation"):
		return {"updated": False}
	if not intent.get("customer"):
		return {"updated": False}
	client.payment_intents.update(payment_intent, {"setup_future_usage": "off_session"})
	return {"updated": True}


def finalize_payment_intent_by_id(settings, pi_id):
	"""Retrieve a (client-confirmed) PaymentIntent and finalize idempotently."""
	client = getattr(settings, "stripe", None) or get_stripe_client(settings)
	intent = client.payment_intents.retrieve(pi_id)
	# Reject a PaymentIntent minted for a different order before settling.
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
		# Async method still settling; the payment_intent.succeeded webhook finalizes it.
		return {"redirect_to": success_redirect(dict(intent.get("metadata") or {})), "status": "Pending"}
	return {"redirect_to": "payment-failed", "status": "Failed"}


def finalize_payment_intent(settings, intent, integration_request=None):
	"""Mark the original Integration Request complete and run on_payment_authorized.

	Idempotent: callable from the synchronous return AND the webhook. Keyed on
	the Integration Request stamped in the intent's metadata, so it runs once.
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
		# Lost the race to a concurrent webhook/redirect — already settled.
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
