# Copyright (c) Frappe Technologies Pvt. Ltd. and contributors
# License: MIT. See LICENSE
#
# Helpers about a payment's reference document (metadata, subscription flag, redirect).

from urllib.parse import urlencode

import frappe
from frappe import _


def is_subscription_reference(data):
	dt, dn = data.get("reference_doctype"), data.get("reference_docname")
	if not dt or not dn or not frappe.db.exists(dt, dn):
		return False
	if not frappe.get_meta(dt).has_field("is_a_subscription"):
		return False
	return bool(frappe.db.get_value(dt, dn, "is_a_subscription"))


def assert_reference_payable(reference_doctype, reference_docname):
	"""Block checkout when the reference is no longer payable.

	Mirrors settlement's Paid short-circuit (``settle_payment_request`` skips
	when ``status == "Paid"``) and ERPNext's ``validate_payment`` message for
	already-paid Payment Requests. Also rejects Cancelled / cancelled docs so
	users cannot open or complete Embedded checkout on a dead link.
	"""
	if not (reference_doctype and reference_docname):
		return
	if not frappe.db.exists(reference_doctype, reference_docname):
		frappe.throw(_("Invalid payment reference."), frappe.ValidationError)

	fields = ["docstatus"]
	meta = frappe.get_meta(reference_doctype)
	if meta.has_field("status"):
		fields.append("status")

	row = frappe.db.get_value(reference_doctype, reference_docname, fields, as_dict=True)
	if not row:
		frappe.throw(_("Invalid payment reference."), frappe.ValidationError)

	if row.docstatus == 2 or row.get("status") == "Cancelled":
		frappe.throw(
			_("The {0} {1} has been cancelled and cannot be paid.").format(
				_(reference_doctype), frappe.bold(reference_docname)
			)
		)

	if row.get("status") == "Paid":
		# Same wording as ERPNext payment_request.validate_payment for PR.
		if reference_doctype == "Payment Request":
			frappe.throw(
				_("The Payment Request {0} is already paid, cannot process payment twice").format(
					frappe.bold(reference_docname)
				)
			)
		frappe.throw(_("This payment has already been paid."))


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
