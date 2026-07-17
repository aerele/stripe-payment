# Copyright (c) Frappe Technologies Pvt. Ltd. and contributors
# License: MIT. See LICENSE
#
# On existing sites migrating from the monorepo payments app, reassign the
# Stripe Settings doctype to the stripe_payment app's "Stripe" module.
# Records are untouched — only the `module` metadata changes.

import frappe


def execute():  # Frappe patch entry point, run via patches.txt on migrate
	for dt in ("Stripe Settings", "Stripe Webhook Log"):
		if frappe.db.exists("DocType", dt):
			frappe.db.set_value("DocType", dt, "module", "Stripe", update_modified=False)
