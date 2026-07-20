# Copyright (c) Frappe Technologies Pvt. Ltd. and contributors
# License: MIT. See LICENSE
#
# Stripe client + amount/idempotency primitives.

import hashlib

import frappe
from frappe.utils import flt

from stripe_payment.gateway.constants import STRIPE_API_VERSION, ZERO_DECIMAL_CURRENCIES

# Per-request cache for the decrypted API key, keyed by Stripe Settings name.
# Avoids a DB read + AES decrypt on every get_stripe_client call within a
# single request/job (notably the hourly sweep, which builds one client per
# Settings doc per API call). Invalidated by StripeSettings.on_update/on_trash.
_API_KEY_CACHE = "stripe_api_keys"


def get_stripe_client(stripe_settings):
	"""Return a Stripe client bound to this Stripe Settings doc.

	stripe_settings may be a doc or the docname of a "Stripe Settings" record.

	A fresh stripe.StripeClient is built per call, carrying its own api key,
	pinned api version and http client. This keeps credentials isolated to the
	caller: mutating module-level stripe.api_key / stripe.api_version races across
	threads, so two concurrent requests for different Stripe accounts could charge
	the wrong account. Route every Stripe call through this client.
	"""
	import stripe

	name = stripe_settings if isinstance(stripe_settings, str) else stripe_settings.name

	api_key = frappe.cache().hget(_API_KEY_CACHE, name)
	if api_key is None:
		# Read just the decrypted secret_key from the password store rather than
		# loading the whole Settings doc with get_doc for a single field.
		from frappe.utils.password import get_decrypted_password

		api_key = get_decrypted_password("Stripe Settings", name, "secret_key", raise_exception=False)
		frappe.cache().hset(_API_KEY_CACHE, name, api_key)

	return stripe.StripeClient(
		api_key,
		stripe_version=STRIPE_API_VERSION,
		http_client=stripe.http_client.RequestsClient(),
	)


def clear_api_key_cache():
	"""Drop the cached Stripe API keys (call on Stripe Settings save/trash)."""
	frappe.cache().delete_key(_API_KEY_CACHE)


def to_minor_units(amount, currency):
	"""Convert a human amount to the integer Stripe expects (e.g. 12.50 USD -> 1250)."""
	if (currency or "").upper() in ZERO_DECIMAL_CURRENCIES:
		return int(round(flt(amount)))
	return int(round(flt(amount) * 100))


def from_minor_units(amount, currency):
	"""Inverse of to_minor_units (e.g. 1250 USD -> 12.50)."""
	if (currency or "").upper() in ZERO_DECIMAL_CURRENCIES:
		return flt(amount)
	return flt(amount) / 100.0


def idempotency_key(*parts):
	"""Deterministic Stripe idempotency key derived from ERPNext identifiers.

	Same inputs -> same key, so a retried create collapses to one Stripe object
	(Stripe dedupes idempotency keys for 24h). Keep the inputs stable per logical
	operation (e.g. the reference docname + amount), never a timestamp/random.
	"""
	raw = ":".join(str(p) for p in parts if p is not None)
	return hashlib.sha256(raw.encode("utf-8")).hexdigest()
