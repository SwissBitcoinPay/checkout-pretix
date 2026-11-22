# Security

This repository is the pretix plugin with which an event organizer's shop takes Bitcoin payments
for a sattle.me merchant. A flaw here could mark an order paid that is not, show an invoice that
pays someone else, or leak the integration key: please report it **privately**, never in a public
issue, pull request or discussion.

## How to report

- On GitHub: **Security** tab of this repository → **Report a vulnerability** (private
  vulnerability reporting).
- Or by email: security@sattle.me, subject starting with `[security] checkout-pretix`.

Please include what you found, the module or the view concerned, the versions of the plugin
(`pretix-sattleme`), of pretix and of `sattleme-checkout`, and, when you can, the steps that show
it. Do not test against real merchants' funds or keys.

## What is in scope

- This plugin: anything that confirms a payment the SDK did not report paid, confirms one order
  with another order's payment, makes a second payable invoice for an order, shows a payment page
  to someone without its link, leaks the integration key, or lets a customer make the server do
  more than the checks of their own payment.
- A flaw of the SDK goes to [checkout-python](https://github.com/sattleme/checkout-python) (its
  SECURITY.md); a rule of the protocol itself to
  [checkout-spec](https://github.com/sattleme/checkout-spec/blob/main/SECURITY.md); a flaw of
  pretix to [pretix](https://github.com/pretix/pretix/security).

We will acknowledge your report, keep you informed, and credit you when the fix is published,
unless you prefer otherwise.
