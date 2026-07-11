# Copyright (c) Frappe Technologies Pvt. Ltd. and contributors
# License: MIT. See LICENSE
#
# Helpers about a payment's reference document (metadata, subscription flag, redirect).

from urllib.parse import urlencode

import frappe


def is_subscription_reference(data):
	dt, dn = data.get("reference_doctype"), data.get("reference_docname")
	if not dt or not dn or not frappe.db.exists(dt, dn):
		return False
	if not frappe.get_meta(dt).has_field("is_a_subscription"):
		return False
	return bool(frappe.db.get_value(dt, dn, "is_a_subscription"))


def get_stripe_metadata(settings, data=None, integration_request=None):
	"""Stripe metadata is string->string; drop empty values."""
	data = data or settings.data
	meta = {
		"reference_doctype": data.get("reference_doctype"),
		"reference_docname": data.get("reference_docname"),
		"integration_request": integration_request,
	}
	return {k: str(v) for k, v in meta.items() if v is not None}


def success_redirect(metadata):
	"""payment-success URL carrying the reference, so the success page can load it."""
	dt = (metadata or {}).get("reference_doctype")
	dn = (metadata or {}).get("reference_docname")
	if dt and dn:
		return f"payment-success?{urlencode({'doctype': dt, 'docname': dn})}"
	return "payment-success"
