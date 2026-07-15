# Copyright (c) Frappe Technologies Pvt. Ltd. and contributors
# License: MIT. See LICENSE
#
# Ensure Customer.stripe_customer_id exists after this feature lands.

from stripe_payment.install import after_install


def execute():
	after_install()
