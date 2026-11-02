# Copyright (c) Frappe Technologies Pvt. Ltd. and contributors
# License: MIT. See LICENSE
#
# Stripe webhook verification, dedupe and dispatch. The HTTP endpoint stays at
# payments.payment_gateways.doctype.stripe_settings.webhooks and delegates here.

import frappe
import stripe
from frappe.utils.password import get_decrypted_password
from payment_core.webhooks import DUPLICATE, get_or_create_log, mark_failed, mark_processed

from stripe_payment.gateway import reconciliation
from stripe_payment.gateway.references import _gateway_string

WEBHOOK_SECRET_CACHE_KEY = "stripe_webhook_secrets"


def construct_event(payload, sig_header):
	"""Verify the signature against every configured Stripe account's secret.

	Returns (event, settings_doc) on success, (None, None) on failure.
	"""
	if not sig_header:
		return None, None

	for settings_name, secret in get_webhook_secrets():
		try:
			event = stripe.Webhook.construct_event(payload, sig_header, secret)
			return event, frappe.get_doc("Stripe Settings", settings_name)
		except stripe.error.SignatureVerificationError:
			continue
		except frappe.DoesNotExistError:
			# Settings deleted since the cache was populated; refresh and skip it.
			clear_cache()
			continue
		except ValueError:
			# Malformed payload — no point trying other secrets.
			return None, None

	return None, None


def handle_event(event, settings):
	"""Dedupe on the Stripe event id, then route to the reconciler.

	A prior *Failed* attempt is allowed to reprocess (its log row is reused); any
	other prior status is a genuine duplicate and is skipped.

	The caller (the webhooks endpoint) is responsible for elevation: it has
	already set_user("Administrator") after verifying the signature, and will
	restore the original user in its own finally block.
	"""
	event_id = event["id"]
	obj = event["data"]["object"]
	log, duplicate = get_or_create_log(
		source=_gateway_string(settings) if settings else "Stripe",
		event_id=event_id,
		event_type=event["type"],
		payload=event,
		provider_reference=obj.get("id"),
	)
	if duplicate:
		return DUPLICATE

	# Run the handler inside a savepoint: a failure rolls back only the handler's
	# partial writes while keeping the log row, and the request transaction is
	# committed by Frappe once we return — no manual commit required.
	frappe.db.savepoint("stripe_webhook_handler")
	try:
		result = reconciliation.route_event(event, settings) or {}
		mark_processed(
			log.name,
			status=result.get("status_label", "Processed"),
			reference_doctype=result.get("reference_doctype"),
			reference_docname=result.get("reference_name"),
		)
	except Exception:
		frappe.db.rollback(save_point="stripe_webhook_handler")
		mark_failed(log.name, frappe.get_traceback())
		frappe.log_error(frappe.get_traceback(), "Stripe webhook processing failed")
		# Transient failure — ask Stripe to retry; the dedupe above reprocesses the Failed row.
		frappe.local.response["http_status_code"] = 500
		return {"status": "error"}

	return {"status": "ok"}


def get_webhook_secrets():
	def _load():
		secrets = []
		# Read the encrypted secret directly from the password store — no per-row
		# get_doc (which would load the whole Settings document each iteration).
		for name in frappe.get_all("Stripe Settings", pluck="name"):
			secret = get_decrypted_password("Stripe Settings", name, "webhook_secret", raise_exception=False)
			if secret:
				secrets.append((name, secret))
		return secrets

	return frappe.cache().get_value(WEBHOOK_SECRET_CACHE_KEY, _load) or []


def clear_cache():
	frappe.cache().delete_value(WEBHOOK_SECRET_CACHE_KEY)
