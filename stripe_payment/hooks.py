app_name = "stripe_payment"
app_title = "Stripe Payment"
app_publisher = "Frappe Technologies"
app_description = "Standalone Stripe gateway for Frappe (depends on payment_core)"
app_email = "hello@frappe.io"
app_license = "mit"

required_apps = ["payment_core"]

payment_gateway_module = {"Stripe": "stripe_payment.gateway"}

# Programmatic subscription create (ERPNext Payment Request / payment_core dispatcher).
gateway_subscription_handler = {
	"stripe": "stripe_payment.gateway.subscriptions.create_stripe_subscription",
}

# Auto-sync Subscription Plan price to Stripe when enabled on Stripe Settings.
doc_events = {
	"Subscription Plan": {"validate": "stripe_payment.gateway.subscriptions.sync_stripe_price"},
}

after_install = "stripe_payment.install.after_install"
before_uninstall = "stripe_payment.install.before_uninstall"
after_migrate = ["stripe_payment.install.after_install"]

scheduler_events = {
	"hourly": ["stripe_payment.gateway.reconciliation.sweep_pending"],
}

add_to_apps_screen = [
	{
		"name": "stripe_payment",
		"logo": "/assets/stripe_payment/images/stripe_payment-logo.svg",
		"title": "Stripe Payment",
		"route": "/app",
	}
]

export_python_type_annotations = True
