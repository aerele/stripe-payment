<div align="center">
<a href="https://stripe.com">
<img src="stripe_payment/public/images/stripe_payment-logo.svg" height="80px" width="80px" alt="Stripe Payment Logo">
</a>
<h2>Stripe Payment</h2>
<p>Modern Stripe payments for Frappe and ERPNext</p>

[![CI](https://github.com/aerele/stripe-payment/actions/workflows/ci.yml/badge.svg?branch=develop)](https://github.com/aerele/stripe-payment/actions/workflows/ci.yml)
[![Linters](https://github.com/aerele/stripe-payment/actions/workflows/linter.yml/badge.svg?branch=develop)](https://github.com/aerele/stripe-payment/actions/workflows/linter.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](license.txt)
</div>

<div align="center">
<a href="https://integrations.frappe.cloud/integrations/payment-integration/stripe/overview">Documentation</a>
·
<a href="https://github.com/aerele/stripe-payment/issues">Report an Issue</a>
·
<a href="https://github.com/aerele/stripe-payment/pulls">Contribute</a>
</div>

## Stripe Payment

Stripe Payment is a standalone payment gateway for Frappe. It connects Stripe with Frappe applications through [Payment Core](https://github.com/aerele/payment-core) and adds ERPNext settlement and subscription workflows when ERPNext is installed.

## Key Features

- **Modern checkout**: Accept one-time and recurring payments through Stripe-hosted Checkout with SCA and 3D Secure support.
- **PaymentIntents**: Process payments using Stripe's PaymentIntents API, including flows that require additional customer authentication.
- **ERPNext settlement**: Settle Payment Requests and create Payment Entries for full and partial payments.
- **Customers and saved cards**: Reuse Stripe customers and save payment methods only with the customer's consent.
- **Subscriptions**: Sync Subscription Plan prices, create Stripe subscriptions, and reconcile recurring invoices with ERPNext.
- **Reliable webhooks**: Verify signatures, prevent duplicate processing, log events, and retry failed reconciliation jobs.

### Under the Hood

- [**Frappe Framework**](https://github.com/frappe/frappe): The full-stack framework on which the app runs.
- [**Payment Core**](https://github.com/aerele/payment-core): The shared payment gateway contracts and utilities used by the integration.
- [**ERPNext**](https://github.com/frappe/erpnext): Provides Payment Request, Payment Entry, and Subscription workflows.
- [**Stripe Python SDK**](https://github.com/stripe/stripe-python): Communicates with the Stripe API.

## Installation

The `develop` branch requires Python 3.14 and compatible `develop` branches of Frappe and Payment Core. Install ERPNext before this app when you need its accounting and subscription workflows.

Set up a Frappe bench by following the [Frappe installation guide](https://docs.frappe.io/framework/user/en/installation), then run:

```sh
bench get-app https://github.com/aerele/payment-core --branch develop
bench get-app https://github.com/aerele/stripe-payment --branch develop
bench --site <site-name> install-app payment_core
bench --site <site-name> install-app stripe_payment
```

## Configuration

1. In the Desk, open **Stripe Settings** and create a record.
2. Enter the payment gateway name and your Stripe publishable and secret keys.
3. Save the settings to create the corresponding Payment Gateway.
4. Copy the generated **Webhook URL** to a webhook endpoint in the Stripe Dashboard.
5. Subscribe the endpoint to these events:
   - `checkout.session.completed`
   - `payment_intent.succeeded`
   - `setup_intent.succeeded`
   - `invoice.paid`
   - `invoice.payment_failed`
   - `customer.subscription.updated`
   - `customer.subscription.deleted`
   - `charge.refunded`
6. Add the endpoint's signing secret to **Webhook Signing Secret** in Stripe Settings.

For live payments, use live API keys and a live-mode webhook endpoint. Use test keys and a test-mode endpoint while testing.

## Development

This app uses `pre-commit` for formatting and linting:

```sh
cd apps/stripe_payment
pre-commit install
```

Run the test suite with:

```sh
bench --site <site-name> run-tests --app stripe_payment
```

## Contributing

Contributions are welcome. Before opening a pull request, please create or reference an [issue](https://github.com/aerele/stripe-payment/issues), add tests for behavioral changes, and ensure the test suite and pre-commit checks pass.

## License

This project is licensed under the [MIT License](license.txt).

<br>
<br>
<div align="center">
  <a href="https://aerele.in">
    <picture>
      <source media="(prefers-color-scheme: dark)" srcset="./stripe_payment/public/images/aerele-dark.png">
      <img src="./stripe_payment/public/images/aerele.png" alt="Aerele Technologies" height="32"/>
    </picture>
  </a>
</div>
