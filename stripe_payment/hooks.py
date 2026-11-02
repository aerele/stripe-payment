app_name = "stripe_payment"
app_title = "Stripe Payment"
app_publisher = "Aerele Technologies"
app_description = "Standalone Stripe gateway for Frappe (depends on payment_core)"
app_email = "integrations-support@aerele.in"
app_license = "mit"

required_apps = ["payment_core"]

payment_gateway_module = {"Stripe": "stripe_payment.gateway"}

# Settings doctypes this gateway app owns. The Gateway Settings hub lists
# these in its "Add Gateway" picker so new gateway accounts are created
# through the settings form (which registers the Payment Gateway row itself
# on save), instead of hand-built Payment Gateway rows that duplicate the
# automatic ones.
payment_gateway_settings_doctypes = ["Stripe Settings"]

# Programmatic subscription create (optional ERPNext compatibility / payment_core dispatcher).
gateway_subscription_handler = {
	"stripe": "stripe_payment.gateway.subscriptions.create_stripe_subscription",
}

# Auto-sync Subscription Plan price to Stripe when enabled on Stripe Settings.
doc_events = {
	# on_update, not validate: a Stripe API timeout / rate-limit during price
	# sync must never block a Subscription Plan save. The handler logs errors
	# and surfaces a non-blocking message; the doc is already persisted by now.
	"Subscription Plan": {"on_update": "stripe_payment.gateway.subscriptions.sync_stripe_price"},
}

after_install = "stripe_payment.install.after_install"
before_uninstall = "stripe_payment.install.before_uninstall"
after_migrate = ["stripe_payment.install.after_install"]

scheduler_events = {
	"hourly": ["stripe_payment.gateway.reconciliation.sweep_pending"],
}

# No add_to_apps_screen: gateways are payment_core plugins, configured from
# the Payment Core workspace (Payment Gateway registry / analytics dashboard).

export_python_type_annotations = True
