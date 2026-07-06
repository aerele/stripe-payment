# Copyright (c) Frappe Technologies Pvt. Ltd. and contributors
# License: MIT. See LICENSE
#
# On existing sites, reassign the Stripe doctypes + custom fields to the
# stripe_payment app's "Stripe" module. Data (records + field values) is
# untouched — only the `module` metadata changes.

import frappe

STRIPE_CUSTOM_FIELDS = {
	"Customer": ("stripe_customer_id",),
	"Subscription": ("stripe_subscription_id", "stripe_customer_id"),
	"Payment Entry": ("stripe_payment_intent",),
}


def execute():
	for dt in ("Stripe Settings", "Stripe Webhook Log"):
		if frappe.db.exists("DocType", dt):
			frappe.db.set_value("DocType", dt, "module", "Stripe", update_modified=False)

	for dt, fieldnames in STRIPE_CUSTOM_FIELDS.items():
		for fieldname in fieldnames:
			name = frappe.db.get_value("Custom Field", {"dt": dt, "fieldname": fieldname})
			if name:
				frappe.db.set_value("Custom Field", name, "module", "Stripe", update_modified=False)

	# Point the display webhook_endpoint at the new app path so admins re-register.
	from frappe.utils import get_url

	new_url = get_url("/api/method/stripe_payment.stripe.doctype.stripe_settings.webhooks")
	for name in frappe.get_all("Stripe Settings", pluck="name"):
		frappe.db.set_value("Stripe Settings", name, "webhook_endpoint", new_url, update_modified=False)
