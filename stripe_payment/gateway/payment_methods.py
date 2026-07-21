# Copyright (c) Frappe Technologies Pvt. Ltd. and contributors
# License: MIT. See LICENSE
#
# Shared payment-method parameters for PaymentIntents and Checkout Sessions.
# UPI (India / INR) requires redirect-capable flows when enabled.


def _upi_available(settings, currency: str | None) -> bool:
	"""Whether UPI should be offered: opted in on Stripe Settings AND INR currency."""
	return bool(getattr(settings, "enable_upi", 0)) and (currency or "").upper() == "INR"


def automatic_payment_methods_for(settings, currency: str | None) -> dict:
	"""PaymentIntent automatic_payment_methods block.

	UPI uses app-switch / bank redirect, so redirects are allowed when UPI is
	active; otherwise redirects stay off so card-only sites remain on-page.
	"""
	allow_redirects = "always" if _upi_available(settings, currency) else "never"
	return {"enabled": True, "allow_redirects": allow_redirects}


def checkout_payment_method_types(settings, currency: str | None) -> list[str] | None:
	"""Explicit method list for Hosted Checkout, or None for Dashboard defaults.

	When UPI is active, returns ["card", "upi"] so UPI renders even if the
	dashboard method ordering is ambiguous; otherwise None lets Stripe pick.
	"""
	if _upi_available(settings, currency):
		return ["card", "upi"]
	return None
