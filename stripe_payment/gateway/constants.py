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
