# Copyright (c) Frappe Technologies Pvt. Ltd. and contributors
# License: MIT. See LICENSE
#
# Settlement claim helper shared with Hosted Checkout finalize.

import frappe


def claim_integration_request(settings):
	"""Atomically claim the Integration Request for settlement. Returns True for the winner."""
	status = frappe.db.get_value(
		"Integration Request", settings.integration_request.name, "status", for_update=True
	)
	if status == "Completed":
		return False
	settings.integration_request.db_set("status", "Completed", update_modified=False)
	return True
