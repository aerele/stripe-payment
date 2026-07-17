# Copyright (c) Frappe Technologies Pvt. Ltd. and contributors
# License: MIT. See LICENSE
#
# Stripe webhook endpoint (path: ...doctype.stripe_settings.webhooks).
# Verification/dispatch live in stripe_payment.gateway.webhooks.

import frappe

from stripe_payment.gateway.webhooks import construct_event, handle_event


@frappe.whitelist(allow_guest=True)  # nosemgrep: frappe-semgrep-rules.rules.security.guest-whitelisted-method
def webhooks():
	"""Stripe webhook receiver.

	Authorization is the Stripe-Signature header: every request is verified
	against each configured account's webhook secret before any work happens.
	No Frappe role check applies — the caller is Stripe's servers, not a user.
	construct_event returns (None, None) on any signature failure, and we
	reject with 400 before touching the database.
	"""
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

	# event/settings are non-None only when the signature verified against an
	# active Stripe Settings account. The signature check above IS the auth
	# gate for this endpoint (Stripe is the caller, not a Frappe user). Elevate
	# to Administrator so the verified event can settle docs on that account,
	# then always restore so the next request doesn't stay elevated.
	original_user = frappe.session.user
	try:
		frappe.set_user("Administrator")  # nosemgrep
		return handle_event(event, settings)
	finally:
		frappe.set_user(original_user)  # nosemgrep
