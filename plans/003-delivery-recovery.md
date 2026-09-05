# Plan 003: Make persisted orders recoverable when UI or delivery fails

Status TODO. Priority P1. Effort M. Risk MED. Category reliability. Planned at f8900a8, 2026-09-04. Depends on 001 and 002.

## Context and drift check

Repo D:/projects/onlineshop; aiogram polling, one SQLite instance, authorized sellers only. Run `git diff --stat f8900a8..HEAD -- order_bot/bot.py order_bot/database.py tests`. Account for prerequisite callback/FSM fixes before proceeding.

At audit, order_bot/bot.py:416 saves an order; :442 awaits callback.answer before :443 publish_order. A failed callback acknowledgment leaves a durable pending row without publication. Existing-row handling at :432-440 clears state and offers retry only for failed. database.py:222 recovery changes only sending to failed. No user-facing pending/failed listing exists.

~~~python
# database.py:194-195
UPDATE orders SET delivery_status = 'sending', delivery_attempts = delivery_attempts + 1,
delivery_error = NULL WHERE id = ? AND delivery_status IN ('pending', 'failed')
~~~

Keep this atomic claim behavior. publish_order at :182-209 stores photo_message_id for split sends and avoids resending that photo on a known partial failure. Existing tests/test_bot.py:test_long_message_retry_does_not_resend_photo verifies this. The documented crash window between Telegram accepting a send and the DB recording it remains uncertain; do not promise exactly-once network delivery.

## Scope

Allowed: order_bot/bot.py, order_bot/database.py, tests/test_delivery_recovery.py, related existing tests and README. No broker, external notifications, automatic replay of ambiguous sends, API migration or schema overhaul. Keep seller ownership checks from retry at bot.py:466 and private-chat authorization. A recovery list may expose only the caller's order reference/delivery state; owner operation scope must be explicit and tested.

## Steps and verification

1. Add real routed tests plus temporary-DB tests: callback acknowledgment raises after save; process boundary after durable insert; repeated confirm of pending; persisted failed order after FSM loss; concurrent retry; foreign seller cannot retry; split photo/text partial failure. Use deterministic injected exceptions, no real network. Verify `python -B -m unittest tests.test_delivery_recovery -v` -> targeted regressions fail on old behavior, existing tests still pass.
2. Ensure Telegram callback acknowledgment is best-effort UI feedback and cannot prevent recovery/publication of an already committed order. Catch known Telegram transport errors at that boundary without swallowing domain/DB errors. Revisit order of acknowledgment without using it as a correctness prerequisite. Verify the new module -> acknowledgment failure leaves a recoverable reference and does not stop the normal publish attempt.
3. Add a bounded, ordered DB query and private recovery command for caller-owned pending/failed records. Expose reference and delivery status only; paginate. Repeated confirmation of a persisted pending/failed order should provide its recovery action rather than silently discard access. Verify new module -> recovery works after an empty/new FSM and inactive/foreign callers see no order data.
4. Keep startup recovery of interrupted sending visible as ambiguous/manual-review work. Differentiate success, failure and already-in-progress in UI; do not describe an in-progress claim as a completed failure. Preserve stored photo IDs. Verify new module -> one network send per concurrent claim, failed work remains discoverable, sent rows are not resent, and partial retry reuses photo_message_id.
5. Document restart, manual reconciliation and retry runbook. Verify `python -B -m unittest discover -v` and prerequisite lint/typecheck -> all pass. Test a fresh Database object over the same temporary file to prove persistence across process-lifetime state changes.

## Acceptance and future migration

Every committed pending/failed order can be found by an authorized recovery path without its original callback message. UI failure cannot be the sole cause of permanently inaccessible work. No claim is made that an uncertain Telegram send is safe to replay automatically. Later PostgreSQL/outbox migration must preserve this distinction, durable message IDs and ownership constraints. No PII or tokens in failure logs; record sanitized error class and order correlation reference.

## Stop conditions and git

Stop on any proposal requiring external exactly-once semantics, replaying production orders, relaxing ownership checks or rewriting existing customer records. Branch codex/delivery-recovery; conventional commits such as `fix: recover persisted pending orders`. No production execution/push. Mark plan DONE only after the targeted and full gates pass.
