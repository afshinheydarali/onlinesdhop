# Fake payment and delivery worker

The sandbox callback is `POST /api/v1/payments/fake/callback`. The caller
sends the exact JSON bytes with `X-Fake-Gateway-Signature: sha256=<hex>`, where
the digest is HMAC-SHA256 over those bytes using `FAKE_PAYMENT_HMAC_SECRET`.
The payload has exactly these fields: `event_id`, `order_id` (the public order
ID), integer `amount`, three-letter `currency`,
`provider_transaction_id`, and `status` (`success` or `failed`). The server
checks the signature, server-side amount/currency/order binding, event replay
fingerprint, and provider transaction uniqueness before mutating payment
state. A late callback is recorded as `reconciliation` and cannot revive an
expired or cancelled order.

Delivery jobs are durable rows in PostgreSQL. The repository includes the
worker process and its lease/retry state machine. A worker claims with
`FOR UPDATE SKIP LOCKED`, a random claim token and a lease. A timeout after a
Telegram request becomes `ambiguous` and needs explicit reconciliation before
retry. After a photo succeeds its message ID is committed before text is
sent, so a retry sends only text. The worker transport is injected and has no
provider implementation in this repository:

```powershell
python -m backend.delivery_worker --transport my_transport:make_transport
```

`make_transport` must return an object implementing async `send_photo(order)`
and `send_text(order, photo_message_id)`. The transport is intentionally
injected, so the demo and tests use a deterministic fake transport and never
send to a real Telegram channel. Payment calls are likewise confined to the
documented fake callback.
