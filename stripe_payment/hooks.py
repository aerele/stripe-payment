app_name = "stripe_payment"
app_title = "Stripe Payment"
app_publisher = "Frappe Technologies"
app_description = "Standalone Stripe gateway for Frappe (depends on payment_core)"
app_email = "hello@frappe.io"
app_license = "mit"

# Depends on the shared base app.
required_apps = ["payment_core"]

# Shared gateway service package (client/constants/references; features extend it).
payment_gateway_module = {"Stripe": "stripe_payment.gateway"}

# Legacy subscription create (parity with monorepo payments.stripe_integration).
gateway_subscription_handler = {
	"stripe": "stripe_payment.payment_gateways.stripe_integration.create_stripe_subscription",
}

after_install = "stripe_payment.install.after_install"
before_uninstall = "stripe_payment.install.before_uninstall"
# Ensure Customer.stripe_customer_id exists on sites that already had the app installed.
after_migrate = ["stripe_payment.install.after_install"]

add_to_apps_screen = [
	{
		"name": "stripe_payment",
		"logo": "/assets/stripe_payment/images/stripe_payment-logo.svg",
		"title": "Stripe Payment",
		"route": "/app",
	}
]

export_python_type_annotations = True
