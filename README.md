### Stripe Payments

Standalone Stripe gateway for Frappe (depends on `payment_core`).

This app is the **Stripe extract** from the monorepo `payments` app: legacy **Charges** API + card-token checkout. Feature work (PaymentIntents, Hosted Checkout, customers, webhooks, etc.) lands as independent PRs from `develop`.

### Installation

```bash
cd $PATH_TO_YOUR_BENCH
bench get-app $URL_OF_THIS_REPO --branch develop
bench install-app stripe_payment
```

Requires `payment_core` (and ERPNext for Payment Request / settlement flows).

### Contributing

This app uses `pre-commit` for code formatting and linting. Please [install pre-commit](https://pre-commit.com/#installation) and enable it for this repository:

```bash
cd apps/stripe_payment
pre-commit install
```

Pre-commit is configured to use the following tools for checking and formatting your code:

- ruff
- eslint
- prettier
- pyupgrade

### License

mit
