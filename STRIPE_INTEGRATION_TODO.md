# Stripe integration: what's left

The step-by-step launch guide is [docs/LAUNCH_PAYMENTS.md](docs/LAUNCH_PAYMENTS.md).
This file records how the Checkout Studio settings were applied.

## Why it isn't the "embedded form"

This account has **Managed Payments** on by default (Stripe is merchant of
record and handles sales tax / VAT). Checked in test mode on 2026-09-21,
Managed Payments:

- refuses `ui_mode: form` (and `custom`); only `hosted_page` and
  `embedded_page` are allowed;
- refuses `automatic_tax: {enabled: false}`, because it always calculates
  tax;
- refuses any product without a `tax_code`.

So the checkout is Stripe's **embedded page** (`ui_mode: embedded_page`,
`stripe.createEmbeddedCheckoutPage`), mounted inside the game's ACCOUNT
sheet. To use the embedded form instead, turn Managed Payments off and take
on sales-tax registration and filing yourself (Stripe Tax can help).

## Values to replace

None are placeholders. The checkout is created in code from the wallet
top-up packs.

**Files:**
- [billing.py](billing.py)

| Field | Current value | Notes |
|-------|---------------|-------|
| mode | `payment` | one-time wallet top-ups ($10 / $25 / $50) |
| line_items[].price_data | built from `billing.PACKS` | no Dashboard Price IDs needed |
| line_items[].price_data.product_data.tax_code | `txcd_10201003` | confirm with Stripe or your accountant; override with `STRIPE_TAX_CODE` |

## Configured parameters

**Files:**
- [billing.py](billing.py) (`create_checkout`)
- [static/js/account.js](static/js/account.js) (loads `https://js.stripe.com/dahlia/stripe.js`, mounts the checkout)

| Parameter | Value |
|-----------|-------|
| ui_mode | `embedded_page` (`STRIPE_CHECKOUT_UI=hosted_page` for a redirect instead) |
| billing_address_collection | `auto` |
| phone_number_collection | `{enabled: false}` |
| submit_type | `auto` |
| automatic_tax | not sent (Managed Payments requires tax on) |
| payment_method_collection | not sent (subscription mode only) |
| integration_identifier | not sent (it labels the embedded form, which can't run here) |
| invoice_creation | enabled: every top-up gets an invoice / receipt |
| customer_creation | `always` |
| return_url | the game page the player paid from, with `?billing=success&cs={CHECKOUT_SESSION_ID}` |

## Setup and next steps

See [docs/LAUNCH_PAYMENTS.md](docs/LAUNCH_PAYMENTS.md): the environment
variables, the webhook, a test run with card `4242 4242 4242 4242`, and
going live.

## Resources

- https://support.stripe.com
- https://docs.stripe.com/mcp
- https://docs.stripe.com/payments/managed-payments/eligibility
