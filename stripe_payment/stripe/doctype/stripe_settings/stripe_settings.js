// Copyright (c) 2017, Frappe Technologies and contributors
// For license information, please see license.txt

frappe.ui.form.on("Stripe Settings", {
	refresh(frm) {
		// Credential validation is on-demand only — never block a save on a
		// live Stripe round-trip (slow / unreachable / rate-limited).
		frm.add_custom_button(__("Test Connection"), () => {
			frm.call({
				method: "test_connection",
				freeze: true,
				freeze_message: __("Verifying Stripe credentials..."),
				callback(r) {
					if (!r.exc) {
						frappe.msgprint(__("Stripe credentials are valid."));
					}
				},
			});
		});
	},
});
