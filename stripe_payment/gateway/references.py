# Copyright (c) Frappe Technologies Pvt. Ltd. and contributors
# License: MIT. See LICENSE
#
# Helpers about a payment's reference document (metadata, subscription flag, redirect).

from urllib.parse import urlencode

import frappe
from frappe import _
from payment_core.utils import (
	create_payment_gateway,
	ensure_reference_transaction,
	update_payment_transaction,
)


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


def _gateway_string(settings):
	"""The canonical gateway identity: `<Payment Gateway row name>:<row name>`.

	Must match payment_core's desk action (`create_transaction_for_reference`)
	exactly, so checkout-created and desk-created transactions for the same
	reference reuse each other. The Payment Gateway row is named
	``Stripe-{gateway_name}`` (autoname: field:gateway).
	"""
	gateway_row_name = f"Stripe-{settings.gateway_name}"
	if not frappe.db.exists("Payment Gateway", gateway_row_name):
		# on_update may not have run yet (first save); create the row —
		# but only when the settings doc actually exists (tests pass mocks)
		if frappe.db.exists("Stripe Settings", settings.gateway_name):
			create_payment_gateway(
				gateway_row_name, settings="Stripe Settings", controller=settings.gateway_name
			)
	return f"{gateway_row_name}:{gateway_row_name}"


def ensure_payment_transaction(settings, data):
	"""Create the shared transaction row for a Stripe attempt.

	Gateway-only sites (ERPNext / apps with their own payment flow) have no
	Payment Transaction table: the attempt is tracked by the app's own
	reference document plus the Integration Request, so nothing is created
	here and every sync call no-ops on the unset name.
	"""
	data = data or settings.data
	transaction = data.get("payment_transaction")
	if transaction:
		return transaction
	if not frappe.db.table_exists("Payment Transaction"):
		return None
	transaction = ensure_reference_transaction(
		data.get("reference_doctype"),
		data.get("reference_docname"),
		_gateway_string(settings),
	)
	data.payment_transaction = transaction
	return transaction


def sync_payment_transaction(
	data,
	status,
	provider_transaction_id=None,
	checkout_url=None,
	error_message=None,
	provider_customer_id=None,
	metadata=None,
):
	"""Mirror Stripe state into the provider-independent transaction ledger.

	``metadata`` accumulates into the transaction's Diagnostics section (merged
	by payment_core, never clobbering earlier entries).
	"""
	return update_payment_transaction(
		(data or {}).get("payment_transaction"),
		status=status,
		provider_transaction_id=provider_transaction_id,
		checkout_url=checkout_url,
		error_message=error_message,
		provider_customer_id=provider_customer_id,
		metadata=metadata,
	)


def get_stripe_metadata(settings, data=None, integration_request=None):
	"""Stripe metadata is string->string; drop empty values."""
	data = data or settings.data
	meta = {
		"reference_doctype": data.get("reference_doctype"),
		"reference_docname": data.get("reference_docname"),
		"integration_request": integration_request,
		"payment_transaction": data.get("payment_transaction"),
	}
	return {k: str(v) for k, v in meta.items() if v is not None}


def success_redirect(metadata):
	"""payment-success URL carrying the reference, so the success page can load it."""
	dt = (metadata or {}).get("reference_doctype")
	dn = (metadata or {}).get("reference_docname")
	if dt and dn:
		return f"payment-success?{urlencode({'doctype': dt, 'docname': dn})}"
	return "payment-success"
