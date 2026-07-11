# Copyright (c) Frappe Technologies Pvt. Ltd. and contributors
# License: MIT. See LICENSE
#
# Settlement helpers shared by Hosted Checkout (this PR) and Embedded Checkout.
# Full PaymentIntent create/finalize lives in the Embedded Checkout feature PR.

import frappe


def claim_integration_request(settings):
	"""Atomically claim the Integration Request for settlement.

	The redirect and a later webhook can arrive concurrently. A SELECT ... FOR UPDATE
	serialises them: the first flips status to Completed and settles; the second
	blocks, then sees Completed and backs off — so finalize_request() runs exactly
	once (no duplicate Payment Entry). Returns True only for the winning caller.
	"""
	status = frappe.db.get_value(
		"Integration Request", settings.integration_request.name, "status", for_update=True
	)
	if status == "Completed":
		return False
	settings.integration_request.db_set("status", "Completed", update_modified=False)
	return True
