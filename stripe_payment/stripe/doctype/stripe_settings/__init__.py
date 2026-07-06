# Copyright (c) Frappe Technologies Pvt. Ltd. and contributors
# License: MIT. See LICENSE
#
# Stripe webhook endpoint (path: ...doctype.stripe_settings.webhooks).
# Verification/dispatch live in stripe_payment.gateway.webhooks; this keeps the
# stable endpoint path and re-exports the helpers for backward compatibility.

import frappe

from stripe_payment.gateway.webhooks import (
	WEBHOOK_SECRET_CACHE_KEY,
	clear_cache,
	construct_event,
	get_webhook_secrets,
	handle_event,
)


@frappe.whitelist(allow_guest=True)
def webhooks():
	r = frappe.request
	if not r:
		return

	payload = r.get_data()
	sig_header = frappe.get_request_header("Stripe-Signature")

	event, settings = construct_event(payload, sig_header)
	if event is None:
		# Bad/absent signature — tell Stripe to stop, do not process.
		frappe.local.response["http_status_code"] = 400
		return {"status": "invalid signature"}

	return handle_event(event, settings)
