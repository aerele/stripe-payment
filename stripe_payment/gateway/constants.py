# Copyright (c) Frappe Technologies Pvt. Ltd. and contributors
# License: MIT. See LICENSE
#
# Stripe constants shared across the Stripe service modules.

# Pin the API version so behaviour doesn't drift if the dashboard default changes.
STRIPE_API_VERSION = "2024-06-20"

# Stripe amounts are in the smallest currency unit, except zero-decimal currencies.
ZERO_DECIMAL_CURRENCIES = {
	"BIF",
	"CLP",
	"DJF",
	"GNF",
	"JPY",
	"KMF",
	"KRW",
	"MGA",
	"PYG",
	"RWF",
	"UGX",
	"VND",
	"VUV",
	"XAF",
	"XOF",
	"XPF",
}

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
