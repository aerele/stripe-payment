# Copyright (c) Frappe Technologies Pvt. Ltd. and contributors
# License: MIT. See LICENSE
#
# Stripe refunds. The ledger entry follows via the charge.refunded webhook.

import frappe
from frappe import _
from frappe.utils import flt

from stripe_payment.gateway.client import get_stripe_client, idempotency_key, to_minor_units


def refund_intent(settings, payment_intent, amount=None):
	"""Refund a PaymentIntent. The ledger entry follows via the charge.refunded webhook."""
	client = get_stripe_client(settings)
	params = {"payment_intent": payment_intent}
	if amount:
		pi = client.payment_intents.retrieve(payment_intent)
		params["amount"] = to_minor_units(amount, pi.currency)
	refund = client.refunds.create(
		params, {"idempotency_key": idempotency_key("refund", payment_intent, amount or "full")}
	)
	return {"refund": refund.id, "status": refund.status}


@frappe.whitelist()
def refund_payment_entry(payment_entry: str, amount: float | None = None):
	"""Refund a Stripe-originated Payment Entry. Books are updated by the webhook."""
	# Real-money action: require write access to this specific Payment Entry.
	frappe.has_permission("Payment Entry", "write", payment_entry, throw=True)
	pi = frappe.db.get_value("Payment Entry", payment_entry, "stripe_payment_intent")
	if not pi:
		frappe.throw(_("This Payment Entry has no linked Stripe payment to refund."))

	settings = _settings_owning_intent(pi)
	if not settings:
		frappe.throw(_("Could not find the Stripe account that owns this payment."))

	return refund_intent(settings, pi, flt(amount) if amount else None)


def _settings_owning_intent(payment_intent):
	"""Find which Stripe account a PaymentIntent belongs to (supports multiple accounts)."""
	for name in frappe.get_all("Stripe Settings", pluck="name"):
		settings = frappe.get_doc("Stripe Settings", name)
		try:
			get_stripe_client(settings).payment_intents.retrieve(payment_intent)
			return settings
		except Exception:
			continue
	return None
