# Copyright (c) Frappe Technologies Pvt. Ltd. and contributors
# License: MIT. See LICENSE
#
# Stripe customer resolution (reuse the same Stripe customer per ERPNext party).

import frappe
import stripe


def get_or_create_customer(client, customer=None, email=None, name=None):
	"""Return a Stripe customer id, reusing Customer.stripe_customer_id when possible.

	`customer` is the ERPNext Customer name (optional). When given, the resolved
	Stripe id is cached back onto Customer.stripe_customer_id so the same Stripe
	customer is reused for every future charge / subscription of that party.
	"""
	has_field = bool(customer) and frappe.db.has_column("Customer", "stripe_customer_id")

	# Reuse the id cached on the Customer.
	if has_field:
		existing = frappe.db.get_value("Customer", customer, "stripe_customer_id")
		if existing:
			try:
				obj = client.customers.retrieve(existing)
				if not obj.get("deleted"):
					return existing
			except stripe.error.InvalidRequestError:
				# Cached id is stale / deleted at Stripe; log a trace and fall through to recreate.
				frappe.log_error(
					f"Stale Stripe customer id {existing} for {customer}", "Stripe customer resolution"
				)

	# Recover an existing Stripe customer by metadata (dedupes a rolled-back write-back).
	if customer:
		found = _find_stripe_customer_by_party(client, customer)
		if found:
			if has_field:
				frappe.db.set_value("Customer", customer, "stripe_customer_id", found, update_modified=False)
			return found

	# Create. No idempotency key: name/email can differ per call; metadata lookup dedupes.
	obj = client.customers.create(
		{
			"email": email,
			"name": name or customer,
			"metadata": {"erpnext_customer": customer} if customer else {},
		}
	)
	if has_field:
		frappe.db.set_value("Customer", customer, "stripe_customer_id", obj.id, update_modified=False)
	return obj.id


def _find_stripe_customer_by_party(client, customer):
	"""Find a non-deleted Stripe customer previously created for this ERPNext party."""
	# Escape quotes so an apostrophe in the party name can't break the search query.
	safe_customer = customer.replace("\\", "\\\\").replace("'", "\\'")
	try:
		result = client.customers.search(
			{"query": f"metadata['erpnext_customer']:'{safe_customer}'", "limit": 1}
		)
	except stripe.error.StripeError:
		return None  # search unavailable / eventual-consistency miss; other errors surface
	for obj in result.get("data") or []:
		if not obj.get("deleted"):
			return obj.id
	return None


def get_party_for_reference(data):
	"""Resolve the ERPNext Customer behind a payment's reference document."""
	dt, dn = data.get("reference_doctype"), data.get("reference_docname")
	if not dt or not dn or not frappe.db.exists(dt, dn):
		return None
	if dt == "Payment Request":
		row = frappe.db.get_value("Payment Request", dn, ["party_type", "party"], as_dict=True)
		if row and row.party_type == "Customer":
			return row.party
		return None
	if frappe.get_meta(dt).has_field("customer"):
		return frappe.db.get_value(dt, dn, "customer")
	return None


def resolve_stripe_customer(client, data):
	"""Reusable Stripe customer id for this payment's party (saves the card for reuse)."""
	party = get_party_for_reference(data)
	return get_or_create_customer(
		client,
		customer=party,
		email=data.get("payer_email"),
		name=data.get("payer_name") or party,
	)
