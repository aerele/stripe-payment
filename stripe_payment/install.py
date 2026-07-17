# Copyright (c) Frappe Technologies Pvt. Ltd. and contributors
# License: MIT. See LICENSE
#
# Install/uninstall Stripe custom fields on ERPNext doctypes.

import click
import frappe
from frappe.custom.doctype.custom_field.custom_field import create_custom_fields

from stripe_payment.gateway.constants import STRIPE_CUSTOM_FIELDS


def after_install():
	# Never let install/migrate abort mid-way and leave the site inconsistent.
	try:
		if "erpnext" not in frappe.get_installed_apps():
			return
		click.secho("* Installing Stripe custom fields")
		create_custom_fields(STRIPE_CUSTOM_FIELDS)
		# Reconciliation looks up Sales Invoices by their subscription each webhook /
		# sweep; index the column so that lookup stays O(log n) as volume grows.
		if frappe.db.has_column("Sales Invoice", "subscription"):
			frappe.db.add_index("Sales Invoice", ["subscription", "docstatus"])
	except Exception:
		frappe.log_error(frappe.get_traceback(), "Stripe after_install failed")


def before_uninstall():
	if "erpnext" not in frappe.get_installed_apps():
		return
	click.secho("* Uninstalling Stripe custom fields")
	for dt, fields in STRIPE_CUSTOM_FIELDS.items():
		frappe.db.delete("Custom Field", {"dt": dt, "fieldname": ("in", [f["fieldname"] for f in fields])})
		frappe.clear_cache(doctype=dt)
