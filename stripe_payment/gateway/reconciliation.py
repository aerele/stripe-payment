# Copyright (c) Frappe Technologies Pvt. Ltd. and contributors
# License: MIT. See LICENSE
#
# Maps verified Stripe webhook events onto ERPNext records; each handler is idempotent.

import frappe
import stripe
from frappe.utils import add_to_date, flt, getdate, now_datetime

from stripe_payment.gateway.client import from_minor_units, get_stripe_client
from stripe_payment.gateway.subscriptions import link_stripe_subscription


def route_event(event, settings):
	handler = _HANDLERS.get(event["type"])
	if not handler:
		return {"status_label": "Ignored"}
	return handler(event, settings)


def reconcile_checkout_session(event, settings):
	"""checkout.session.completed — Hosted Checkout one-off / subscription start."""
	session = event["data"]["object"]
	meta = dict(session.get("metadata") or {})

	if session.get("mode") == "subscription" and session.get("subscription"):
		# Link the ERPNext Subscription so invoice.paid can reconcile each cycle.
		erpnext_sub = meta.get("erpnext_subscription")
		if erpnext_sub and frappe.db.exists("Subscription", erpnext_sub):
			link_stripe_subscription(erpnext_sub, session.get("subscription"), session.get("customer"))

	# Mark the originating Payment Request paid (idempotent).
	result = settings.finalize_checkout_session(session["id"]) or {}
	return {
		"status_label": "Failed" if result.get("status") == "Failed" else "Processed",
		"reference_doctype": meta.get("reference_doctype"),
		"reference_name": meta.get("reference_docname"),
	}


def reconcile_recurring_invoice(event, settings):
	"""invoice.paid — map a paid Stripe subscription invoice to its ERPNext SI."""
	invoice = event["data"]["object"]
	if not invoice.get("subscription"):
		return {"status_label": "Ignored"}  # not a subscription invoice

	# De-dupe at the ledger level too: one Payment Entry per Stripe invoice.
	if frappe.db.exists("Payment Entry", {"reference_no": invoice.get("id"), "docstatus": 1}):
		return {"status_label": "Ignored"}

	# The first cycle is often already settled by the checkout return, whose Payment
	# Entry carries the same invoice PaymentIntent (not the invoice id). Dedupe on that
	# too so checkout.session.completed and invoice.paid cannot both book the first cycle.
	pi = invoice.get("payment_intent")
	if (
		pi
		and frappe.db.has_column("Payment Entry", "stripe_payment_intent")
		and frappe.db.exists("Payment Entry", {"stripe_payment_intent": pi, "docstatus": 1})
	):
		return {"status_label": "Ignored"}

	erpnext_sub = _find_linked_subscription(invoice, settings)
	if not erpnext_sub:
		return {"status_label": "Ignored"}

	si = _find_period_sales_invoice(erpnext_sub, invoice)
	if not si:
		si = _generate_and_find_sales_invoice(erpnext_sub, invoice)
	if not si:
		return {"status_label": "Ignored"}

	# The first-cycle invoice is often already settled by the checkout return (a Payment
	# Entry keyed on the PaymentIntent, not the Stripe invoice id), so book against this
	# SI only if it still has an outstanding balance — otherwise we'd double-book.
	if flt(frappe.db.get_value("Sales Invoice", si, "outstanding_amount")) <= 0:
		return {"status_label": "Ignored"}

	pe_name = _create_payment_entry(si, invoice, settings)
	return {
		"status_label": "Processed" if pe_name else "Ignored",
		"reference_doctype": "Sales Invoice",
		"reference_name": si,
	}


def mark_dunning(event, settings):
	"""invoice.payment_failed — flag the ERPNext Subscription; ERPNext grace logic runs."""
	invoice = event["data"]["object"]
	erpnext_sub = _find_linked_subscription(invoice, settings)
	if erpnext_sub:
		frappe.get_doc("Subscription", erpnext_sub).add_comment(
			"Comment", _comment("Stripe reported a failed payment for invoice {0}.").format(invoice.get("id"))
		)
	return {
		"status_label": "Processed" if erpnext_sub else "Ignored",
		"reference_doctype": "Subscription" if erpnext_sub else None,
		"reference_name": erpnext_sub,
	}


def sync_subscription_status(event, settings):
	"""customer.subscription.updated/deleted — keep the ERPNext link/status note in step."""
	sub = event["data"]["object"]
	erpnext_sub = _subscription_from_stripe_id(sub.get("id")) or _subscription_from_metadata(sub)
	if not erpnext_sub:
		return {"status_label": "Ignored"}

	if event["type"] == "customer.subscription.deleted":
		frappe.get_doc("Subscription", erpnext_sub).add_comment(
			"Comment", _comment("Stripe subscription {0} was cancelled.").format(sub.get("id"))
		)
	else:
		link_stripe_subscription(erpnext_sub, sub.get("id"), sub.get("customer"))
	return {"status_label": "Processed", "reference_doctype": "Subscription", "reference_name": erpnext_sub}


def _comment(msg):
	return frappe._(msg)


def _find_linked_subscription(invoice, settings):
	erpnext_sub = _subscription_from_stripe_id(invoice.get("subscription"))
	if erpnext_sub:
		return erpnext_sub
	# Fall back to the Stripe subscription's metadata, then cache the link.
	try:
		client = get_stripe_client(settings)
		sub = client.subscriptions.retrieve(invoice.get("subscription"))
	except stripe.error.InvalidRequestError:
		return None  # genuinely not found; transient errors propagate -> logged Failed -> retried
	erpnext_sub = _subscription_from_metadata(sub)
	if erpnext_sub:
		link_stripe_subscription(erpnext_sub, sub.get("id"), sub.get("customer"))
	return erpnext_sub


def _subscription_from_stripe_id(stripe_subscription_id):
	if not stripe_subscription_id or not frappe.db.has_column("Subscription", "stripe_subscription_id"):
		return None
	return frappe.db.get_value("Subscription", {"stripe_subscription_id": stripe_subscription_id}, "name")


def _subscription_from_metadata(stripe_sub):
	name = dict(stripe_sub.get("metadata") or {}).get("erpnext_subscription")
	if name and frappe.db.exists("Subscription", name):
		return name
	return None


def _invoice_period(invoice):
	"""(from_date, to_date) of the recurring line on a Stripe invoice."""
	lines = (invoice.get("lines") or {}).get("data") or []
	period = lines[0].get("period") if lines else None
	if not period:
		return None, None
	from datetime import datetime, timezone

	start = getdate(datetime.fromtimestamp(period["start"], tz=timezone.utc))
	end = getdate(datetime.fromtimestamp(period["end"], tz=timezone.utc))
	return start, end


def _find_period_sales_invoice(erpnext_sub, invoice):
	start, end = _invoice_period(invoice)
	filters = {"subscription": erpnext_sub, "docstatus": 1, "is_return": 0}
	if start and end:
		exact = frappe.get_all(
			"Sales Invoice",
			filters={**filters, "from_date": start, "to_date": end},
			pluck="name",
			limit=1,
		)
		if exact:
			return exact[0]
		# Tolerance for day-level date drift: an unpaid SI whose period OVERLAPS this
		# Stripe invoice's period — never a non-overlapping later cycle. Oldest first,
		# since Stripe bills periods in order.
		overlap = frappe.get_all(
			"Sales Invoice",
			filters={
				**filters,
				"outstanding_amount": (">", 0),
				"from_date": ("<=", end),
				"to_date": (">=", start),
			},
			pluck="name",
			order_by="posting_date asc",
			limit=1,
		)
		return overlap[0] if overlap else None
	# No period on the invoice: fall back to the OLDEST unpaid SI (Stripe bills in order).
	unpaid = frappe.get_all(
		"Sales Invoice",
		filters={**filters, "outstanding_amount": (">", 0)},
		pluck="name",
		order_by="posting_date asc",
		limit=1,
	)
	return unpaid[0] if unpaid else None


def _generate_and_find_sales_invoice(erpnext_sub, invoice):
	"""Stripe billed before ERPNext's scheduler — generate the period SI, then re-find."""
	_, end = _invoice_period(invoice)
	sub_doc = frappe.get_doc("Subscription", erpnext_sub)
	# No commit here: keep SI generation in the webhook's transaction so a later Payment
	# Entry failure rolls back the SI too. A generation failure must PROPAGATE (not be
	# swallowed into a terminal "Ignored") so handle_event marks the log Failed and retries.
	sub_doc.process(posting_date=end or getdate(now_datetime()))
	return _find_period_sales_invoice(erpnext_sub, invoice)


def _create_payment_entry(si, invoice, settings):
	from payment_core.utils import erpnext_app_import_guard

	with erpnext_app_import_guard():
		from erpnext.accounts.doctype.payment_entry.payment_entry import get_payment_entry

	pe = get_payment_entry("Sales Invoice", si)
	pe.reference_no = invoice.get("id")
	pe.reference_date = getdate(now_datetime())
	if pe.meta.has_field("stripe_payment_intent"):
		pe.stripe_payment_intent = invoice.get("payment_intent")
	pe.flags.ignore_permissions = True
	pe.insert()
	pe.submit()
	return pe.name


def record_intent_processing(event, settings):
	"""payment_intent.processing — async method (e.g. UPI) accepted and still settling.

	Interim only: payment_intent.succeeded performs the settlement. Recording it keeps
	the event out of the "Ignored" bucket so the timeline shows the pending state.
	"""
	intent = event["data"]["object"]
	if intent.get("invoice"):
		return {"status_label": "Ignored"}  # subscription invoices settle via invoice.paid
	meta = dict(intent.get("metadata") or {})
	return {
		"status_label": "Processed",
		"reference_doctype": meta.get("reference_doctype"),
		"reference_name": meta.get("reference_docname"),
	}


def mark_intent_failed(event, settings):
	"""payment_intent.payment_failed — a one-off async payment (e.g. UPI) failed after
	'processing'. Mark its Integration Request Failed so it is not left stuck Pending."""
	intent = event["data"]["object"]
	if intent.get("invoice"):
		return {"status_label": "Ignored"}  # subscription failures via invoice.payment_failed
	ir = frappe.db.get_value("Integration Request", {"output": intent.get("id")}, "name")
	if not ir:
		return {"status_label": "Ignored"}
	doc = frappe.get_doc("Integration Request", ir)
	if doc.status != "Completed":
		doc.db_set("status", "Failed", update_modified=False)
	meta = dict(intent.get("metadata") or {})
	return {
		"status_label": "Processed",
		"reference_doctype": meta.get("reference_doctype"),
		"reference_name": meta.get("reference_docname"),
	}


def reconcile_one_off(event, settings):
	"""payment_intent.succeeded — backstop for one-off payments.

	PaymentIntents that belong to a Stripe invoice (subscriptions) are handled
	via invoice.paid, so they are skipped here.
	"""
	intent = event["data"]["object"]
	if intent.get("invoice"):
		return {"status_label": "Ignored"}

	meta = dict(intent.get("metadata") or {})
	if not meta.get("reference_docname"):
		return {"status_label": "Ignored"}

	result = settings.finalize_payment_intent(intent) or {}
	return {
		"status_label": "Failed" if result.get("status") == "Failed" else "Processed",
		"reference_doctype": meta.get("reference_doctype"),
		"reference_name": meta.get("reference_docname"),
	}


def handle_setup_intent_succeeded(event, settings):
	"""setup_intent.succeeded — make the saved card the customer's default."""
	si = event["data"]["object"]
	customer = si.get("customer")
	payment_method = si.get("payment_method")
	if customer and payment_method:
		client = get_stripe_client(settings)
		client.customers.update(customer, {"invoice_settings": {"default_payment_method": payment_method}})
	return {"status_label": "Processed"}


def process_refund(event, settings):
	"""charge.refunded — flag the linked Payment Entry with the refund details.

	The Stripe refund is recorded against the Payment Entry as a comment rather
	than auto-posting a credit note, so the accounting reversal stays an explicit,
	auditable action (avoids silently mutating the ledger from a webhook).
	"""
	charge = event["data"]["object"]
	pi = charge.get("payment_intent")
	pe = (
		frappe.db.get_value("Payment Entry", {"stripe_payment_intent": pi, "docstatus": 1}, "name")
		if pi
		else None
	)
	if not pe:
		return {"status_label": "Ignored"}

	amount = from_minor_units(charge.get("amount_refunded", 0), charge.get("currency"))
	frappe.get_doc("Payment Entry", pe).add_comment(
		"Comment",
		frappe._(
			"Stripe refund processed: {0} {1} (charge {2}). Post a credit note / reversal if required."
		).format(amount, (charge.get("currency") or "").upper(), charge.get("id")),
	)
	return {"status_label": "Processed", "reference_doctype": "Payment Entry", "reference_name": pe}


def sweep_pending():
	"""Scheduler: retry webhook events that FAILED or were stranded mid-processing.

	"Failed" rows are transient errors already marked for retry. "Received" rows older
	than a few minutes are events whose worker died after the dedupe row was committed
	but before the outcome was written (a hard crash) — otherwise handle_event would
	treat redeliveries as duplicates forever. route_event is idempotent, so replay is safe.
	"""
	MAX_RETRIES = 5
	stale_cutoff = add_to_date(now_datetime(), minutes=-10)
	rows = frappe.get_all(
		"Stripe Webhook Log",
		or_filters=[
			{"status": "Failed", "retry_count": ("<", MAX_RETRIES)},
			{"status": "Received", "creation": ("<", stale_cutoff)},
		],
		fields=["name", "stripe_settings", "payload", "retry_count"],
		limit=50,
	)
	for row in rows:
		try:
			event = frappe.parse_json(row.payload)
			settings = frappe.get_doc("Stripe Settings", row.stripe_settings) if row.stripe_settings else None
			status_label = (route_event(event, settings) or {}).get("status_label", "Processed")
		except Exception:
			frappe.db.rollback()
			frappe.log_error(frappe.get_traceback(), "Stripe webhook sweep failed")
			status_label = "Failed"

		# Count the attempt whether the handler threw OR returned Failed, so a
		# permanently-failing event ages out of the sweep instead of looping forever.
		updates = {"status": status_label}
		if status_label == "Failed":
			updates["retry_count"] = (row.retry_count or 0) + 1
		frappe.db.set_value("Stripe Webhook Log", row.name, updates, update_modified=False)
		frappe.db.commit()


_HANDLERS = {
	"checkout.session.completed": reconcile_checkout_session,
	"payment_intent.succeeded": reconcile_one_off,
	"payment_intent.processing": record_intent_processing,
	"payment_intent.payment_failed": mark_intent_failed,
	"setup_intent.succeeded": handle_setup_intent_succeeded,
	"invoice.paid": reconcile_recurring_invoice,
	"invoice.payment_failed": mark_dunning,
	"customer.subscription.updated": sync_subscription_status,
	"customer.subscription.deleted": sync_subscription_status,
	"charge.refunded": process_refund,
}
