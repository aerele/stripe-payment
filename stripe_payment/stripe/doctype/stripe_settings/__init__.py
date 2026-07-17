# Copyright (c) Frappe Technologies Pvt. Ltd. and contributors
# License: MIT. See LICENSE
#
# Stripe webhook endpoint (path: ...doctype.stripe_settings.webhooks).
# Verification/dispatch live in stripe_payment.gateway.webhooks.

import frappe

from stripe_payment.gateway.webhooks import construct_event, handle_event


@frappe.whitelist(allow_guest=True)  # nosemgrep: frappe-semgrep-rules.rules.security.guest-whitelisted-method
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
