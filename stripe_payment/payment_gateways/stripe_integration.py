# Copyright (c) Frappe Technologies Pvt. Ltd. and contributors
# License: MIT. See LICENSE
#
# Backward-compat import path; prefer gateway.subscriptions.create_stripe_subscription.

from stripe_payment.gateway.subscriptions import create_stripe_subscription

__all__ = ["create_stripe_subscription"]
