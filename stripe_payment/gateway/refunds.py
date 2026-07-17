# Copyright (c) Frappe Technologies Pvt. Ltd. and contributors
# License: MIT. See LICENSE
#
# Stripe refunds. Ledger follow-up is via charge.refunded webhook (webhooks PR)
# or manual accounting action.

import frappe
from frappe import _
from frappe.utils import flt

from stripe_payment.gateway.client import get_stripe_client, idempotency_key, to_minor_units


def refund_intent(settings, payment_intent, amount=None):
	"""Refund a PaymentIntent (full or partial)."""
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
	"""Refund a Stripe-originated Payment Entry."""
	# Real-money action: require write access to this specific Payment Entry.
	frappe.has_permission("Payment Entry", "write", payment_entry, throw=True)
	pi = frappe.db.get_value("Payment Entry", payment_entry, "stripe_payment_intent")
	if not pi:
		frappe.throw(_("This Payment Entry has no linked Stripe payment to refund."))

	settings = _resolve_settings(payment_entry, pi)
	if not settings:
		frappe.throw(_("Could not find the Stripe account that owns this payment."))

	return refund_intent(settings, pi, flt(amount) or None)


def _resolve_settings(payment_entry, payment_intent):
	"""Resolve the Stripe Settings account that owns a Payment Entry.

	Primary path: read the ``stripe_settings`` column stamped at settlement
	(a single local read, no Stripe API calls). Falls back to probing Settings
	docs only when the column is absent (older sites that haven't run the
	install patch yet) — and even then loads each doc once, not per-call.
	"""
	if frappe.get_meta("Payment Entry").has_field("stripe_settings"):
		name = frappe.db.get_value("Payment Entry", payment_entry, "stripe_settings")
		if name:
			return frappe.get_doc("Stripe Settings", name)

	# Legacy fallback: owning account wasn't stamped. Probe each account's
	# Stripe API (get_stripe_client accepts the name and resolves+decrypts
	# internally) and load the owning Settings doc exactly once — after the
	# probe identifies it — so no get_doc runs inside the iteration loop.
	for name in frappe.get_all("Stripe Settings", pluck="name"):
		try:
			get_stripe_client(name).payment_intents.retrieve(payment_intent)
			return frappe.get_doc("Stripe Settings", name)
		except Exception:
			continue
	return None
