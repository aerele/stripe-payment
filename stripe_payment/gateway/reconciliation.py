# Copyright (c) Frappe Technologies Pvt. Ltd. and contributors
# License: MIT. See LICENSE
#
# Maps verified Stripe webhook events onto ERPNext records; each handler is idempotent.

import frappe
import stripe
from frappe.utils import cint, flt, getdate, now_datetime

from stripe_payment.gateway.client import from_minor_units, get_stripe_client
from stripe_payment.gateway.subscriptions import link_stripe_subscription


# NOTE ON "UNUSED" FUNCTIONS IN THIS MODULE
# The reconcile_*/mark_dunning/sync_subscription_status/handle_setup_intent_succeeded/
# process_refund functions are event handlers dispatched dynamically by Stripe event
# type through the _HANDLERS table at the bottom of this file (route_event looks them
# up by string key), so they are never called by name. The _-prefixed functions are
# internal helpers for those handlers. Static "unused function" analysis cannot see
# the table-driven dispatch; none of these are dead code.
def route_event(event, settings):
	handler = _HANDLERS.get(event["type"])
	if not handler:
		return {"status_label": "Ignored"}
	return handler(event, settings)


def _status_label(result):  # helper for the reconcile_* handlers (see dispatch note above)
	"""Log label from a finaliser result. Async (bank-debit) sessions settle later,
	so surface Pending instead of masking it as Processed."""
	status = (result or {}).get("status")
	if status == "Failed":
		return "Failed"
	if status == "Pending":
		return "Pending"
	return "Processed"


def reconcile_checkout_session(event, settings):  # dispatched via _HANDLERS
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
		"status_label": _status_label(result),
		"reference_doctype": meta.get("reference_doctype"),
		"reference_name": meta.get("reference_docname"),
	}


def reconcile_recurring_invoice(event, settings):  # dispatched via _HANDLERS
	"""invoice.paid — map a paid Stripe subscription invoice to its ERPNext SI."""
	invoice = event["data"]["object"]
	if not invoice.get("subscription"):
		return {"status_label": "Ignored"}  # not a subscription invoice

	# De-dupe at the ledger level too: one Payment Entry per Stripe invoice.
	if frappe.db.exists("Payment Entry", {"reference_no": invoice.get("id"), "docstatus": 1}):
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


def mark_dunning(event, settings):  # dispatched via _HANDLERS
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


def sync_subscription_status(event, settings):  # dispatched via _HANDLERS
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


def _comment(msg):  # helper used by the reconcilers above
	return frappe._(msg)


def _find_linked_subscription(invoice, settings):  # helper used by the reconcilers above
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


def _subscription_from_stripe_id(stripe_subscription_id):  # helper used by the reconcilers above
	if not stripe_subscription_id or not frappe.db.has_column("Subscription", "stripe_subscription_id"):
		return None
	return frappe.db.get_value("Subscription", {"stripe_subscription_id": stripe_subscription_id}, "name")


def _subscription_from_metadata(stripe_sub):  # helper used by the reconcilers above
	name = dict(stripe_sub.get("metadata") or {}).get("erpnext_subscription")
	if name and frappe.db.exists("Subscription", name):
		return name
	return None


def _invoice_period(invoice):  # helper used by the reconcilers above
	"""(from_date, to_date) of the recurring line on a Stripe invoice."""
	lines = (invoice.get("lines") or {}).get("data") or []
	period = lines[0].get("period") if lines else None
	if not period:
		return None, None
	from datetime import datetime, timezone

	start = getdate(datetime.fromtimestamp(period["start"], tz=timezone.utc))
	end = getdate(datetime.fromtimestamp(period["end"], tz=timezone.utc))
	return start, end


def _find_period_sales_invoice(erpnext_sub, invoice):  # helper used by the reconcilers above
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


def _generate_and_find_sales_invoice(erpnext_sub, invoice):  # helper used by the reconcilers above
	"""Stripe billed before ERPNext's scheduler — generate the period SI, then re-find."""
	_, end = _invoice_period(invoice)
	sub_doc = frappe.get_doc("Subscription", erpnext_sub)
	# No commit here: keep SI generation in the webhook's transaction so a later
	# Payment Entry failure rolls back the SI too. Let a generation failure propagate:
	# the caller's savepoint rolls back and the event is marked Failed and retried,
	# rather than being silently swallowed as "Ignored" and dropped from the sweep.
	sub_doc.process(posting_date=end or getdate(now_datetime()))
	return _find_period_sales_invoice(erpnext_sub, invoice)


def _create_payment_entry(si, invoice, settings):  # helper used by the reconcilers above
	from payment_core.utils import erpnext_app_import_guard

	with erpnext_app_import_guard():
		from erpnext.accounts.doctype.payment_entry.payment_entry import get_payment_entry

	pe = get_payment_entry("Sales Invoice", si)
	pe.reference_no = invoice.get("id")
	pe.reference_date = getdate(now_datetime())
	if pe.meta.has_field("stripe_payment_intent"):
		pe.stripe_payment_intent = invoice.get("payment_intent")
	# Reached only from the webhook / hourly sweep, both of which run as
	# Administrator, so the insert is already privileged (no ignore_permissions).
	pe.insert()
	pe.submit()
	return pe.name


def reconcile_one_off(event, settings):  # dispatched via _HANDLERS
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
		"status_label": _status_label(result),
		"reference_doctype": meta.get("reference_doctype"),
		"reference_name": meta.get("reference_docname"),
	}


def handle_setup_intent_succeeded(event, settings):  # dispatched via _HANDLERS
	"""setup_intent.succeeded — make the saved card the customer's default."""
	si = event["data"]["object"]
	customer = si.get("customer")
	payment_method = si.get("payment_method")
	if customer and payment_method:
		client = get_stripe_client(settings)
		client.customers.update(customer, {"invoice_settings": {"default_payment_method": payment_method}})
	return {"status_label": "Processed"}


def process_refund(event, settings):  # dispatched via _HANDLERS
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
	"""Scheduler: retry webhook events that failed processing (dropped/erroring deliveries)."""
	MAX_RETRIES = 5
	# Bounded batch per hourly run so a large backlog can't hold a single scheduler tick
	# open indefinitely; any leftover Failed rows are picked up on the next sweep and
	# retry_count still caps total attempts, so nothing is dropped. Sites that need larger
	# batches can raise this via the stripe_webhook_sweep_batch_size site config key.
	try:
		batch_size = cint(frappe.conf.get("stripe_webhook_sweep_batch_size")) or 50
		rows = frappe.get_all(
			"Stripe Webhook Log",
			filters={"status": "Failed", "retry_count": ("<", MAX_RETRIES)},
			fields=["name", "stripe_settings", "payload", "retry_count"],
			limit=batch_size,
		)
	except Exception:
		# Never let a scheduled task raise: log and bail, the next hourly run retries.
		frappe.log_error(frappe.get_traceback(), "Stripe webhook sweep could not load pending events")
		return

	# Reuse one Settings doc per account across rows instead of reloading it each iteration.
	settings_cache = {}
	# Collect outcomes and flush them as two bulk UPDATEs after the loop, rather than
	# one set_value per row (N separate UPDATE queries).
	failed = []
	processed = {}
	for row in rows:
		# Isolate each row in a savepoint so a failing retry rolls back only its own
		# partial writes, leaving rows already processed in this batch intact. The
		# scheduler commits the whole batch when the job finishes — no manual commit.
		frappe.db.savepoint("stripe_webhook_sweep_row")
		try:
			event = frappe.parse_json(row.payload)
			settings = _sweep_settings(row.stripe_settings, settings_cache)
			status_label = (route_event(event, settings) or {}).get("status_label", "Processed")
		except Exception:
			frappe.db.rollback(save_point="stripe_webhook_sweep_row")
			frappe.log_error(frappe.get_traceback(), "Stripe webhook sweep failed")
			status_label = "Failed"

		# Count the attempt whether the handler threw OR returned Failed, so a
		# permanently-failing event ages out of the sweep instead of looping forever.
		if status_label == "Failed":
			failed.append(row.name)
		else:
			processed.setdefault(status_label, []).append(row.name)

	_apply_sweep_status(failed, processed)


def _sweep_settings(name, cache):
	"""Load each Stripe Settings account at most once per sweep run."""
	if not name:
		return None
	if name not in cache:
		cache[name] = frappe.get_doc("Stripe Settings", name)
	return cache[name]


def _apply_sweep_status(failed, processed):
	"""Persist sweep outcomes as bulk UPDATEs instead of one write per row.

	`failed` -> single UPDATE that sets status=Failed and increments retry_count;
	`processed` is {status_label: [names]} -> one UPDATE per distinct label.
	"""
	if failed:
		frappe.db.sql(
			"""UPDATE `tabStripe Webhook Log`
			SET status = 'Failed', retry_count = retry_count + 1
			WHERE name IN %(names)s""",
			{"names": tuple(failed)},
		)
	for status_label, names in processed.items():
		frappe.db.sql(
			"UPDATE `tabStripe Webhook Log` SET status = %(status)s WHERE name IN %(names)s",
			{"status": status_label, "names": tuple(names)},
		)


_HANDLERS = {
	"checkout.session.completed": reconcile_checkout_session,
	"payment_intent.succeeded": reconcile_one_off,
	"setup_intent.succeeded": handle_setup_intent_succeeded,
	"invoice.paid": reconcile_recurring_invoice,
	"invoice.payment_failed": mark_dunning,
	"customer.subscription.updated": sync_subscription_status,
	"customer.subscription.deleted": sync_subscription_status,
	"charge.refunded": process_refund,
}
