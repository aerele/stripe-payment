# Copyright (c) Frappe Technologies Pvt. Ltd. and contributors
# License: MIT. See LICENSE

from stripe_payment.install import after_install


def execute():
	after_install()
