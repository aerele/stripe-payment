# Copyright (c) Frappe Technologies Pvt. Ltd. and contributors
# License: MIT. See LICENSE
#
# Install/uninstall Stripe custom fields on ERPNext doctypes.

import click
import frappe
from frappe.custom.doctype.custom_field.custom_field import create_custom_fields

# Customer mapping only in this feature PR (subscriptions/refunds add their fields later).
STRIPE_CUSTOM_FIELDS = {
	"Customer": [
		{
			"fieldname": "stripe_customer_id",
			"fieldtype": "Data",
			"label": "Stripe Customer ID",
			"read_only": 1,
			"no_copy": 1,
			"print_hide": 1,
			"insert_after": "default_currency",
			"module": "Stripe",
		}
	],
}


def after_install():
	if "erpnext" not in frappe.get_installed_apps():
		return
	click.secho("* Installing Stripe custom fields")
	create_custom_fields(STRIPE_CUSTOM_FIELDS)


def before_uninstall():
	if "erpnext" not in frappe.get_installed_apps():
		return
	click.secho("* Uninstalling Stripe custom fields")
	for dt, fields in STRIPE_CUSTOM_FIELDS.items():
		frappe.db.delete("Custom Field", {"dt": dt, "fieldname": ("in", [f["fieldname"] for f in fields])})
		frappe.clear_cache(doctype=dt)
