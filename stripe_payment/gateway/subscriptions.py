# Copyright (c) Frappe Technologies Pvt. Ltd. and contributors
# License: MIT. See LICENSE
#
# Subscription plan / ERPNext-Subscription linkage helpers, programmatic
# subscription creation (V1 contract), and Subscription Plan -> Stripe Price sync.

import frappe
from frappe import _
from frappe.integrations.utils import create_request_log
from frappe.utils import add_to_date, cint, get_datetime, now_datetime

from stripe_payment.gateway.client import get_stripe_client, idempotency_key, to_minor_units
from stripe_payment.gateway.customers import get_or_create_customer

INTERVAL_MAP = {"Day": "day", "Week": "week", "Month": "month", "Year": "year"}

# Stripe free trials must end at least ~48 hours after creation.
_MIN_TRIAL_HOURS = 48

BILL_FROM_CYCLE_ONE = "Bill From Cycle One"
CHARGE_NOW_DEFER_FIRST = "Charge Now + Defer First Cycle"


def get_subscription_plan_details(reference_doctype, reference_docname):
	"""The (plan, qty) rows behind a subscription's Payment Request."""
	return frappe.get_all(
		"Subscription Plan Detail",
		filters={"parent": reference_docname, "parenttype": reference_doctype},
		fields=["plan", "qty"],
		order_by="idx",
	)


def get_subscription_line_items(reference_doctype, reference_docname):
	"""Stripe subscription items [{price, quantity}] from the synced plan prices."""
	items = []
	for row in get_subscription_plan_details(reference_doctype, reference_docname):
		price = frappe.db.get_value("Subscription Plan", row.plan, "product_price_id")
		if not price:
			frappe.throw(
				_("Subscription Plan {0} has no synced Stripe price; sync it before checkout.").format(
					row.plan
				)
			)
		items.append({"price": price, "quantity": row.qty or 1})
	return items


def find_erpnext_subscription(party, plan_names):
	"""Best-effort match of an active ERPNext Subscription by party + plan set.

	The Stripe subscription is created from a Payment Request, while ERPNext's
	Subscription doctype is what generates the recurring Sales Invoices; they are
	not linked natively. This finds the Subscription whose plans cover the paid
	plans so we can stamp the Stripe id onto it. The webhook later corrects this
	authoritatively from the Stripe subscription metadata.
	"""
	if not party or not plan_names or not frappe.db.exists("DocType", "Subscription"):
		return None
	candidates = frappe.get_all(
		"Subscription",
		filters={"party": party, "status": ("in", ["Active", "Trialing", "Trial"])},
		pluck="name",
		order_by="creation desc",
	)
	for name in candidates:
		plans = set(
			frappe.get_all(
				"Subscription Plan Detail",
				filters={"parent": name, "parenttype": "Subscription"},
				pluck="plan",
			)
		)
		if set(plan_names).issubset(plans):
			return name
	return None


def link_stripe_subscription(erpnext_subscription, stripe_subscription_id, stripe_customer_id=None):
	"""Stamp the Stripe ids onto an ERPNext Subscription (the reconciliation link)."""
	if not erpnext_subscription:
		return
	values = {}
	if frappe.db.has_column("Subscription", "stripe_subscription_id"):
		values["stripe_subscription_id"] = stripe_subscription_id
	if stripe_customer_id and frappe.db.has_column("Subscription", "stripe_customer_id"):
		values["stripe_customer_id"] = stripe_customer_id
	if values:
		frappe.db.set_value("Subscription", erpnext_subscription, values, update_modified=False)


def is_charge_now_defer_first_cycle(settings) -> bool:
	"""Whether Settings uses the Charge Now + Defer First Cycle billing model."""
	return (getattr(settings, "subscription_billing_model", None) or "") == CHARGE_NOW_DEFER_FIRST


def get_first_cycle_trial_end(reference_doctype, reference_docname) -> int:
	"""Unix timestamp for the end of the first plan interval (Stripe trial_end).

	Used by Charge Now + Defer First Cycle: the customer pays the Payment Request
	amount immediately (one-time), while recurring plan prices stay on trial until
	this timestamp so Stripe does not double-bill the first cycle.
	"""
	details = get_subscription_plan_details(reference_doctype, reference_docname)
	if not details:
		frappe.throw(_("No subscription plan found on this payment request."))

	plan_name = details[0].plan
	row = frappe.db.get_value(
		"Subscription Plan",
		plan_name,
		["billing_interval", "billing_interval_count"],
		as_dict=True,
	)
	if not row or not row.billing_interval:
		frappe.throw(_("Subscription Plan {0} is missing a billing interval.").format(frappe.bold(plan_name)))

	count = cint(row.billing_interval_count) or 1
	now = now_datetime()
	interval = row.billing_interval
	if interval == "Day":
		end = add_to_date(now, days=count)
	elif interval == "Week":
		end = add_to_date(now, days=7 * count)
	elif interval == "Month":
		end = add_to_date(now, months=count)
	elif interval == "Year":
		end = add_to_date(now, years=count)
	else:
		frappe.throw(_("Unsupported billing interval {0} on plan {1}.").format(interval, plan_name))

	# Stripe requires free trials to end at least 48 hours after creation.
	min_end = add_to_date(now, hours=_MIN_TRIAL_HOURS)
	if get_datetime(end) < get_datetime(min_end):
		end = min_end

	return int(get_datetime(end).timestamp())


def build_charge_now_line_item(amount, currency, description=None):
	"""One-time Checkout / invoice line for the immediate Charge Now amount."""
	return {
		"price_data": {
			"currency": (currency or "").lower(),
			"unit_amount": to_minor_units(amount, currency),
			"product_data": {
				"name": description or _("Subscription charge (first cycle)"),
			},
		},
		"quantity": 1,
	}


def apply_charge_now_defer_first_cycle(
	params, *, amount, currency, reference_doctype, reference_docname, description=None
):
	"""Mutate Hosted Checkout session params for Charge Now + Defer First Cycle.

	- Adds a one-time line item for ``amount`` (paid at Checkout).
	- Sets ``subscription_data.trial_end`` to the end of the first plan interval
	  so recurring prices are not invoiced until the next cycle.
	"""
	line_items = list(params.get("line_items") or [])
	line_items.append(build_charge_now_line_item(amount, currency, description=description))
	params["line_items"] = line_items

	sub_data = dict(params.get("subscription_data") or {})
	sub_data["trial_end"] = get_first_cycle_trial_end(reference_doctype, reference_docname)
	params["subscription_data"] = sub_data
	return params


def apply_charge_now_defer_first_cycle_subscription(
	create_args,
	client,
	*,
	customer_id,
	amount,
	currency,
	reference_doctype,
	reference_docname,
	description=None,
):
	"""Mutate Subscription.create args for Charge Now + Defer First Cycle.

	Pending invoice item on the customer is pulled into the first invoice (the
	trial start invoice) so the customer pays immediately while recurring items
	remain $0 until trial_end.
	"""
	create_args["trial_end"] = get_first_cycle_trial_end(reference_doctype, reference_docname)
	client.invoice_items.create(
		{
			"customer": customer_id,
			"amount": to_minor_units(amount, currency),
			"currency": (currency or "").lower(),
			"description": description or _("Subscription charge (first cycle)"),
		}
	)
	return create_args


def get_stripe_settings_for_gateway(payment_gateway_account):
	"""Resolve a Payment Gateway Account name to its Stripe Settings doc.

	Walks the same chain as get_gateway_controller() in stripe_settings.py:
	    Payment Gateway Account -> Payment Gateway -> Stripe Settings
	Returns None when the account is not backed by Stripe Settings.
	"""
	pg = frappe.db.get_value("Payment Gateway Account", payment_gateway_account, "payment_gateway")
	if not pg:
		return None
	gw = frappe.db.get_value("Payment Gateway", pg, ["gateway_settings", "gateway_controller"], as_dict=True)
	if not gw or gw.gateway_settings != "Stripe Settings":
		return None
	return frappe.get_doc("Stripe Settings", gw.gateway_controller)


def create_stripe_subscription(gateway_controller, data):
	stripe_settings = frappe.get_doc("Stripe Settings", gateway_controller)
	stripe_settings.data = frappe._dict(data)
	stripe_settings.stripe = get_stripe_client(stripe_settings)

	try:
		stripe_settings.integration_request = create_request_log(stripe_settings.data, "Host", "Stripe")
		return create_subscription_on_stripe(stripe_settings)

	except Exception:
		stripe_settings.log_error("Unable to create Stripe subscription")
		return {
			"redirect_to": frappe.redirect_to_message(
				_("Server Error"),
				_(
					"It seems that there is an issue with the server's stripe configuration. In case of failure, the amount will get refunded to your account."
				),
			),
			"status": 401,
		}


def create_subscription_on_stripe(stripe_settings):
	client = stripe_settings.stripe
	data = stripe_settings.data

	pr = frappe.get_doc("Payment Request", data.reference_docname)
	party = pr.party if pr.party_type == "Customer" else None
	items = get_subscription_line_items("Payment Request", pr.name)
	plan_names = [
		row.plan
		for row in frappe.get_all(
			"Subscription Plan Detail",
			filters={"parent": pr.name, "parenttype": "Payment Request"},
			fields=["plan"],
		)
	]

	customer_id = get_or_create_customer(
		client, customer=party, email=data.get("payer_email"), name=data.get("payer_name") or party
	)
	erpnext_sub = find_erpnext_subscription(party, plan_names)
	metadata = {"reference_doctype": "Payment Request", "reference_docname": pr.name}
	if erpnext_sub:
		metadata["erpnext_subscription"] = erpnext_sub
	if party:
		metadata["erpnext_customer"] = party

	try:
		create_args = {
			"customer": customer_id,
			"items": items,
			"metadata": metadata,
			"payment_behavior": "default_incomplete",
			"payment_settings": {"save_default_payment_method": "on_subscription"},
			"expand": ["latest_invoice.payment_intent"],
		}

		if is_charge_now_defer_first_cycle(stripe_settings):
			# Charge PR amount now; defer first recurring plan invoice via trial.
			apply_charge_now_defer_first_cycle_subscription(
				create_args,
				client,
				customer_id=customer_id,
				amount=data.get("amount") or pr.grand_total,
				currency=data.get("currency") or pr.currency,
				reference_doctype="Payment Request",
				reference_docname=pr.name,
				description=data.get("description") or pr.subject,
			)

		subscription = client.subscriptions.create(
			create_args, {"idempotency_key": idempotency_key("sub", pr.name)}
		)
		intent = getattr(getattr(subscription, "latest_invoice", None), "payment_intent", None)
		# Stamp the first invoice's PaymentIntent (refundable), not the sub id (sub_xxx).
		output_id = intent.id if intent is not None else subscription.id
		stripe_settings.integration_request.db_set("output", output_id, update_modified=False)
		link_stripe_subscription(erpnext_sub, subscription.id, customer_id)

		if subscription.status in ("active", "trialing"):
			stripe_settings.integration_request.db_set("status", "Completed", update_modified=False)
			stripe_settings.flags.status_changed_to = "Completed"
		elif subscription.status == "incomplete":
			# First invoice needs client-side confirmation; invoice.paid webhook settles it.
			if intent is not None:
				return {
					"requires_action": True,
					"client_secret": intent.client_secret,
					"payment_intent": intent.id,
					"status": "Pending",
				}
			stripe_settings.integration_request.db_set("status", "Pending", update_modified=False)
		else:
			stripe_settings.integration_request.db_set("status", "Failed", update_modified=False)
			frappe.log_error(f"Stripe Subscription ID {subscription.id}: status {subscription.status}")

	except Exception:
		stripe_settings.integration_request.db_set("status", "Failed", update_modified=False)
		stripe_settings.log_error("Unable to create Stripe subscription")

	stripe_settings.data.setdefault("reference_doctype", "Payment Request")
	stripe_settings.data.setdefault("reference_docname", pr.name)
	return stripe_settings.finalize_request()


def sync_stripe_price(doc, method=None):
	"""doc_event on Subscription Plan (erpnext) — owned by the payments app."""
	if doc.price_determination not in ("Fixed Rate", "Monthly Rate", "Based On Price List"):
		return
	if not doc.payment_gateway:
		return

	settings = get_stripe_settings_for_gateway(doc.payment_gateway)
	if not settings:
		return  # plan is not on a Stripe gateway

	if not settings.get("sync_subscription_price"):
		return  # opt-in disabled on this Stripe account — keep the manual flow

	# Per-unit recurring amount (Stripe multiplies by quantity at checkout).
	unit_cost = _plan_unit_cost(doc)
	if not unit_cost:
		if doc.price_determination == "Based On Price List":
			frappe.msgprint(
				_(
					"No rate found in the plan's Price List for its Item, so the "
					"Stripe price could not be synced."
				),
				indicator="orange",
				alert=True,
			)
		return

	client = get_stripe_client(settings)

	unit_amount = to_minor_units(unit_cost, doc.currency)
	recurring = {
		"interval": INTERVAL_MAP[doc.billing_interval],
		"interval_count": cint(doc.billing_interval_count) or 1,
	}

	try:
		product_id = None
		old_price_id = None
		if doc.product_price_id:
			existing = client.prices.retrieve(doc.product_price_id)
			if _matches(existing, unit_amount, doc.currency, recurring):
				return  # already in sync — no API write
			product_id = existing.product  # reuse same Product
			old_price_id = doc.product_price_id

		if not product_id:
			product_id = client.products.create(
				{"name": doc.plan_name, "metadata": {"erpnext_plan": doc.name}},
				{"idempotency_key": idempotency_key("stripe_product", doc.name)},
			).id

		price = client.prices.create(
			{
				"product": product_id,
				"unit_amount": unit_amount,
				"currency": (doc.currency or "").lower(),
				"recurring": recurring,
				"metadata": {"erpnext_plan": doc.name},
			},
			{
				"idempotency_key": idempotency_key(
					"stripe_price",
					doc.name,
					product_id,
					unit_amount,
					(doc.currency or "").lower(),
					recurring["interval"],
					recurring["interval_count"],
				)
			},
		)
		doc.product_price_id = price.id  # persists: we run on validate

		if old_price_id:
			# Archive the superseded price only now that the new one is live + persisted.
			client.prices.update(old_price_id, {"active": False})

	except Exception:
		frappe.log_error(frappe.get_traceback(), "Stripe price sync failed")
		frappe.msgprint(
			_("Could not sync this plan's price to Stripe; the Product Price ID may be stale. Please retry."),
			indicator="orange",
			alert=True,
		)


def _matches(price, unit_amount, currency, recurring):
	return bool(
		price.get("active")
		and price.unit_amount == unit_amount
		and (price.currency or "").upper() == (currency or "").upper()
		and price.get("recurring")
		and price.recurring.interval == recurring["interval"]
		and price.recurring.interval_count == recurring["interval_count"]
	)


def _plan_unit_cost(doc):
	"""Per-unit recurring amount in the plan's currency (qty=1).

	Stripe stores a single unit price and multiplies by quantity at checkout,
	so we always push the qty=1 rate.
	"""
	if doc.price_determination == "Based On Price List":
		from payment_core.utils import erpnext_app_import_guard

		with erpnext_app_import_guard():
			from erpnext.accounts.doctype.subscription_plan.subscription_plan import get_plan_rate

		return get_plan_rate(doc.name, quantity=1)
	# Fixed Rate / Monthly Rate carry a per-interval cost on the plan itself.
	return doc.cost
