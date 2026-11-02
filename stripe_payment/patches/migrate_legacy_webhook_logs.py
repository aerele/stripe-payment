# Copyright (c) Frappe Technologies Pvt. Ltd. and contributors
# License: MIT. See LICENSE
#
# Copy legacy Stripe Webhook Log rows into payment_core's provider-neutral
# Payment Webhook Log. The legacy doctype is kept read-only for history; new
# deliveries land only in the common log.

import frappe
from payment_core.webhooks import get_or_create_log


def execute():
	if "payment_core" not in frappe.get_installed_apps():
		return
	if not frappe.db.table_exists("Stripe Webhook Log"):
		return
	if not frappe.db.table_exists("Payment Webhook Log"):
		return
	legacy = frappe.get_all(
		"Stripe Webhook Log",
		fields=[
			"name",
			"stripe_event_id",
			"event_type",
			"stripe_object_id",
			"status",
			"stripe_settings",
			"payload",
		],
		order_by="creation asc",
	)
	for row in legacy:
		if not row.stripe_event_id:
			continue
		log, duplicate = get_or_create_log(
			source=f"Stripe-{row.stripe_settings}:Stripe-{row.stripe_settings}"
			if row.stripe_settings
			else "Stripe",
			event_id=row.stripe_event_id,
			event_type=row.event_type,
			payload=frappe.parse_json(row.payload) if row.payload else None,
			provider_reference=row.stripe_object_id,
		)
		if duplicate:
			continue
		status = {"Processed": "Processed", "Ignored": "Ignored"}.get(row.status, "Failed")
		frappe.db.set_value("Payment Webhook Log", log.name, {"status": status}, update_modified=False)
