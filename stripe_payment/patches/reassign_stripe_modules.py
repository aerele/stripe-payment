# Copyright (c) Frappe Technologies Pvt. Ltd. and contributors
# License: MIT. See LICENSE
#
# On existing sites migrating from the monorepo payments app, reassign the
# Stripe Settings doctype to the stripe_payment app's "Stripe" module.
# Records are untouched — only the `module` metadata changes.

import frappe


def execute():
	if frappe.db.exists("DocType", "Stripe Settings"):
		frappe.db.set_value("DocType", "Stripe Settings", "module", "Stripe", update_modified=False)
