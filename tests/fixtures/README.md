# Fixtures — provenance

**These payloads were not captured from the wire.**

PLAN.md M1 calls for *recorded real payloads* from a Razorpay test-mode account. No
test-mode credentials were available during this build, so `webhooks/*.json` and
`downtimes_list.json` are **synthesised** by `scripts/make_fixtures.py` from
Razorpay's public webhook and Downtime API documentation.

What that means for anyone reading results built on them:

| Property | Status |
|---|---|
| Envelope structure (`entity`, `event`, `contains`, `payload.<name>.entity`, `created_at`) | Documented shape, believed faithful |
| Error field vocabulary (`error_code`, `error_reason`, `error_source`, `error_step`) | Drawn from `antar/signals/razorpay_errors.py`, which is itself compiled from public documentation and carries the same verification duty |
| Field-by-field fidelity to a live response | **Unverified.** Fields Razorpay sends that we do not model are absent, and any field we model that Razorpay has since renamed would go unnoticed |
| Behavioural quirks of test mode vs live | **Unknown.** This is the gap PLAN.md M1 wanted closed in week one, and it is not closed. See `docs/LIMITATIONS.md` |

## Closing the gap

`scripts/record_fixtures.py` is the real path. With `RAZORPAY_KEY_ID` and
`RAZORPAY_KEY_SECRET` in the environment it drives the test-mode API, captures the
actual responses and webhook deliveries, and overwrites these files. Every parsing
test runs unchanged against the recordings, so the swap is a one-command operation
and any divergence surfaces as a test failure rather than as a silent wrong answer.

Until that is run, treat the integration layer as *structurally* tested and not
*empirically* tested, and read `docs/LIMITATIONS.md` before believing anything about
how Antar behaves against the live API.

## Layout

```
webhooks/
  payment.failed.<failure-class>.json    one per class the taxonomy must resolve
  subscription.*.json                    mandate lifecycle transitions
  payment.downtime.*.json                started / updated / resolved
  order.paid.json, invoice.expired.json  recovery and the second loss class
downtimes_list.json                      GET /payments/downtimes collection
```
