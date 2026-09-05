# Plan 002: Bind every order action to the preview the user actually saw

Status TODO. Priority P1. Effort M. Risk MED. Category correctness/concurrency. Planned at f8900a8, 2026-09-04. Depends on 001.

## Context and drift check

Repository D:/projects/onlineshop is a single-instance aiogram/SQLite internal order bot. Run `git diff --stat f8900a8..HEAD -- order_bot/bot.py tests` and inspect working changes. Expected prerequisite changes are tests/tooling and bounded /admins fixes. Stop on unexplained confirmation/FSM changes.

Current excerpts from order_bot/bot.py:

~~~python
# 87-92: preview_keyboard
InlineKeyboardButton(text="تأیید و ارسال", callback_data="order:confirm")
# 410-412: confirm checks keys, not preview identity/state
data = await state.get_data()
required = {"draft_token", *(key for key, *_ in STEPS)}
if not required.issubset(data):
# 443-444: an await permits another task to change the draft
sent = await publish_order(callback.bot, db, config, order, admin)
await state.clear()
# 521
dispatcher = Dispatcher()
~~~

Photo handler at :341 changes photo_file_id in every OrderForm state; :355-359 does not refresh an existing preview. Edit/cancel/duplicate callbacks at :388/:397/:457 also have no draft binding. Database draft_token uniqueness already provides insertion idempotency; retain it. A token alone is insufficient when the same draft is edited: use preview revision or immutable snapshot identity too.

## Scope and conventions

Allowed: order_bot/bot.py, tests/test_bot.py, tests/test_handlers.py, new tests/test_draft_integrity.py, README.md for behavior documentation, plan index. Database schema, catalog/API and delivery-worker changes are out of scope. Preserve active/private authorization on every action, masked duplicate warning, Persian labels and current integer money representation. Follow unittest IsolatedAsyncioTestCase with temporary SQLite and fake transport, as in tests/test_bot.py.

## Commands

Existing: `python -B -m unittest discover -v`. New test module gate: `python -B -m unittest tests.test_draft_integrity -v`. Run prerequisite lint/typecheck gates exactly as documented in updated README (planned `ruff check order_bot tests`, `mypy order_bot`). No live Telegram credentials.

## Steps

1. Write routed regression tests: old confirm/edit/cancel against a new draft; confirm while editing with keys still present; old duplicate-confirm against a different draft; replacement photo during preview. Assert no stale action changes/persists the current draft. Verify new module -> red specifically for these regressions on current code; existing suite stays green.
2. Define a compact callback schema with action, draft identity and preview revision within Telegram callback_data byte limit. Validate identity, revision, authorization and expected state before mutations. Duplicate consent must correspond to the current warning for the current snapshot; editing invalidates it. Disable old buttons best-effort, but server validation is authoritative. Verify new module -> stale action and duplicate-consent tests pass, current valid preview creates one row, repeated action creates no second row.
3. Treat photo replacement as a draft edit: generate/display a new preview and invalidate prior actions. Keep preview snapshot stable through save, including normalized fields. Verify new module -> submitted photo/text equal latest displayed snapshot; previous preview cannot confirm it.
4. Enable per-context event isolation using aiogram's supplied mechanism for this single process. Also clear state only if it still belongs to the completed draft; this matters if publication later moves outside the handler. Use deterministic asyncio.Event barriers to test concurrent confirmation/new-order and double confirmation; no arbitrary sleeps. Verify new module -> newer draft survives, each draft saves at most once, unrelated users can proceed independently.
5. Run full regression, lint/typecheck and update README behavior. Verify `python -B -m unittest discover -v` -> all pass, including order entry and retry ownership from 001.

## Done criteria and maintenance

All four action types are identity-bound. Validating only callback formatting or only FSM state is insufficient. Existing UUID uniqueness and SQL transactions remain. Any new draft field must participate in the confirmed snapshot; new mutable UI actions must invalidate preview revision. Revisit storage-backed event isolation if multi-process bot handling is introduced; SimpleEventIsolation alone does not coordinate processes.

## Stop conditions and git

Stop if fixing this requires schema migration or changing privacy/authorization policy, or if Telegram callback limits cannot accommodate the proposed shape. Prefer a shorter server-side identifier over omitting integrity checks. Branch codex/draft-integrity, conventional atomic commits such as `fix: bind order actions to preview revision`; do not deploy/push without instruction. Update plan index after all gates pass.
