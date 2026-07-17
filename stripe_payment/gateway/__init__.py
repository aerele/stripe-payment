# Copyright (c) Frappe Technologies Pvt. Ltd. and contributors
# License: MIT. See LICENSE

"""Stripe service package: shared primitives, Hosted Checkout, customers,
card-saving, PaymentIntents, subscriptions, webhooks, and reconciliation.

Pull in the specific submodule you need — for example the ``checkout`` module,
or ``handle_event`` from the ``webhooks`` submodule. This package deliberately
re-exports nothing, so there is no barrel to keep in sync and no eager
cross-module load when the package is first imported.
"""
