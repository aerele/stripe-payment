app_name = "stripe_payment"
app_title = "Stripe Payment"
app_publisher = "Frappe Technologies"
app_description = "Standalone Stripe gateway for Frappe (depends on payment_core)"
app_email = "hello@frappe.io"
app_license = "mit"

# Depends on the shared base app.
required_apps = ["payment_core"]

# Installation
# ------------
after_install = "stripe_payment.install.after_install"
before_uninstall = "stripe_payment.install.before_uninstall"

# Register the Stripe subscription handler with payment_core's dispatcher.
gateway_subscription_handler = {
	"stripe": "stripe_payment.gateway.subscriptions.create_stripe_subscription",
}

# Register the Stripe service module with payment_core's gateway registry.
payment_gateway_module = {"Stripe": "stripe_payment.gateway"}

# Document Events / JS / scheduler
doc_events = {
	"Subscription Plan": {"validate": "stripe_payment.gateway.subscriptions.sync_stripe_price"},
}
doctype_js = {"Payment Entry": "public/js/payment_entry_stripe.js"}
scheduler_events = {
	"hourly": ["stripe_payment.gateway.reconciliation.sweep_pending"],
}

# Apps
# ------------------

# required_apps = []

# Each item in the list will be shown as an app in the apps page
add_to_apps_screen = [
	{
		"name": "stripe_payment",
		"logo": "/assets/stripe_payment/images/stripe_payment-logo.svg",
		"title": "Stripe Payment",
		"route": "/app",
	}
]

# Includes in <head>
# ------------------

# include js, css files in header of desk.html
# app_include_css = "/assets/stripe_payment/css/stripe_payment.css"
# app_include_js = "/assets/stripe_payment/js/stripe_payment.js"

# include js, css files in header of web template
# web_include_css = "/assets/stripe_payment/css/stripe_payment.css"
# web_include_js = "/assets/stripe_payment/js/stripe_payment.js"

# include custom scss in every website theme (without file extension ".scss")
# website_theme_scss = "stripe_payment/public/scss/website"

# include js, css files in header of web form
# webform_include_js = {"doctype": "public/js/doctype.js"}
# webform_include_css = {"doctype": "public/css/doctype.css"}

# include js in page
# page_js = {"page" : "public/js/file.js"}

# include js in doctype views
# doctype_js = {"doctype" : "public/js/doctype.js"}
# doctype_list_js = {"doctype" : "public/js/doctype_list.js"}
# doctype_tree_js = {"doctype" : "public/js/doctype_tree.js"}
# doctype_calendar_js = {"doctype" : "public/js/doctype_calendar.js"}

# Svg Icons
# ------------------
# include app icons in desk
# app_include_icons = "stripe_payment/public/icons.svg"

# Home Pages
# ----------

# application home page (will override Website Settings)
# home_page = "login"

# website user home page (by Role)
# role_home_page = {
# 	"Role": "home_page"
# }

# Generators
# ----------

# automatically create page for each record of this doctype
# website_generators = ["Web Page"]

# automatically load and sync documents of this doctype from downstream apps
# importable_doctypes = [doctype_1]

# Jinja
# ----------

# add methods and filters to jinja environment
# jinja = {
# 	"methods": "stripe_payment.utils.jinja_methods",
# 	"filters": "stripe_payment.utils.jinja_filters"
# }

# Installation
# ------------

# before_install = "stripe_payment.install.before_install"
# after_install = "stripe_payment.install.after_install"

# Uninstallation
# ------------

# before_uninstall = "stripe_payment.uninstall.before_uninstall"
# after_uninstall = "stripe_payment.uninstall.after_uninstall"

# Integration Setup
# ------------------
# To set up dependencies/integrations with other apps
# Name of the app being installed is passed as an argument

# before_app_install = "stripe_payment.utils.before_app_install"
# after_app_install = "stripe_payment.utils.after_app_install"

# Integration Cleanup
# -------------------
# To clean up dependencies/integrations with other apps
# Name of the app being uninstalled is passed as an argument

# before_app_uninstall = "stripe_payment.utils.before_app_uninstall"
# after_app_uninstall = "stripe_payment.utils.after_app_uninstall"

# Build
# ------------------
# To hook into the build process

# after_build = "stripe_payment.build.after_build"

# Desk Notifications
# ------------------
# See frappe.core.notifications.get_notification_config

# notification_config = "stripe_payment.notifications.get_notification_config"

# Permissions
# -----------
# Permissions evaluated in scripted ways

# permission_query_conditions = {
# 	"Event": "frappe.desk.doctype.event.event.get_permission_query_conditions",
# }
#
# has_permission = {
# 	"Event": "frappe.desk.doctype.event.event.has_permission",
# }

# Document Events
# ---------------
# Hook on document methods and events

# doc_events = {
# 	"*": {
# 		"on_update": "method",
# 		"on_cancel": "method",
# 		"on_trash": "method"
# 	}
# }

# Scheduled Tasks
# ---------------

# scheduler_events = {
# 	"all": [
# 		"stripe_payment.tasks.all"
# 	],
# 	"daily": [
# 		"stripe_payment.tasks.daily"
# 	],
# 	"hourly": [
# 		"stripe_payment.tasks.hourly"
# 	],
# 	"weekly": [
# 		"stripe_payment.tasks.weekly"
# 	],
# 	"monthly": [
# 		"stripe_payment.tasks.monthly"
# 	],
# }

# Testing
# -------

# before_tests = "stripe_payment.install.before_tests"

# Extend DocType Class
# ------------------------------
#
# Specify custom mixins to extend the standard doctype controller.
# extend_doctype_class = {
# 	"Task": "stripe_payment.custom.task.CustomTaskMixin"
# }

# Overriding Methods
# ------------------------------
#
# override_whitelisted_methods = {
# 	"frappe.desk.doctype.event.event.get_events": "stripe_payment.event.get_events"
# }
#
# each overriding function accepts a `data` argument;
# generated from the base implementation of the doctype dashboard,
# along with any modifications made in other Frappe apps
# override_doctype_dashboards = {
# 	"Task": "stripe_payment.task.get_dashboard_data"
# }

# exempt linked doctypes from being automatically cancelled
#
# auto_cancel_exempted_doctypes = ["Auto Repeat"]

# Ignore links to specified DocTypes when deleting documents
# -----------------------------------------------------------

# ignore_links_on_delete = ["Communication", "ToDo"]

# Request Events
# ----------------
# before_request = ["stripe_payment.utils.before_request"]
# after_request = ["stripe_payment.utils.after_request"]

# Job Events
# ----------
# before_job = ["stripe_payment.utils.before_job"]
# after_job = ["stripe_payment.utils.after_job"]

# User Data Protection
# --------------------

# user_data_fields = [
# 	{
# 		"doctype": "{doctype_1}",
# 		"filter_by": "{filter_by}",
# 		"redact_fields": ["{field_1}", "{field_2}"],
# 		"partial": 1,
# 	},
# 	{
# 		"doctype": "{doctype_2}",
# 		"filter_by": "{filter_by}",
# 		"partial": 1,
# 	},
# 	{
# 		"doctype": "{doctype_3}",
# 		"strict": False,
# 	},
# 	{
# 		"doctype": "{doctype_4}"
# 	}
# ]

# Authentication and authorization
# --------------------------------

# auth_hooks = [
# 	"stripe_payment.auth.validate"
# ]

# Automatically update python controller files with type annotations for this app.
export_python_type_annotations = True

# Require all whitelisted methods to have type annotations
require_type_annotated_api_methods = True

# default_log_clearing_doctypes = {
# 	"Logging DocType Name": 30  # days to retain logs
# }

# Translation
# ------------
# List of apps whose translatable strings should be excluded from this app's translations.
# ignore_translatable_strings_from = []
