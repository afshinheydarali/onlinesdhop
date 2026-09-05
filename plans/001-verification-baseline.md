# Plan 001: Establish repeatable verification and fix bounded input defects

Status: DONE. Priority P1. Effort M. Fix risk LOW, except historical phone-data repair MED. Category tests/dx/bugs. Planned at f8900a8, 2026-09-04. Dependencies: none.

Supervisor approval, 2026-09-05: reviewed commits 797f5cf, 85a9b45, 7d29078; independently verified 34 tests, Ruff, mypy and pip check. Integrated at bd95678. GitHub Actions run 33956758383 passed on Python 3.11 and 3.12. Minimal typing narrowings in existing bot/database code and Docker constrained installation were approved scope adjustments.

## Context and drift check

Repository: D:/projects/onlineshop. Python aiogram polling bot, SQLite, unittest. Run `git diff --stat f8900a8..HEAD -- order_bot tests README.md requirements.txt Dockerfile compose.yaml` and `git diff -- order_bot tests`. Compare the following facts with current code before editing; stop on unexplained changes.

- `order_bot/database.py:8`: `from datetime import UTC, datetime, timedelta`.
- `order_bot/validation.py:46`: `re.fullmatch(r"09\d{9}", value)`; Persian/Arabic digits are translated but other decimal scripts remain unchanged.
- `order_bot/bot.py:295`: all admins are joined into one string and passed to `message.answer`.
- `tests/test_bot.py:51`: the preview test asserts callback strings, not a routed interaction.
- `README.md:7` advertises Python 3.10; `:111` describes more flow coverage than exists.
- requirements.txt pins aiogram 3.31.0 and tzdata 2025.3. No CI, pyproject configuration or transitive lock was tracked at audit time.

Preserve private-chat checks, active-admin checks, SQL parameterization, UTC storage and Persian user messages. Match `tests/test_database.py` TemporaryDirectory/setUp/tearDown and `tests/test_bot.py` IsolatedAsyncioTestCase/AsyncMock conventions. Never use actual bot credentials or production SQLite in tests.

## Scope

Allowed: tests/, order_bot/validation.py, order_bot/bot.py only for admin-list splitting, README.md, requirements.txt, new requirements-dev.txt/lock files, pyproject.toml, .github/workflows/ci.yml, .gitignore and this plan's index status. No order-confirmation redesign, delivery redesign, framework migration, or production deployment.

## Commands

Existing verified gate: `python -B -m unittest discover -v` -> all tests pass (23 at audit). Runtime observed: Python 3.11.0, aiogram 3.31.0. No existing lint/typecheck command.

Executor setup, not performed in the audit: `python -m venv .venv`; Windows install: `.\.venv\Scripts\python.exe -m pip install -r requirements.txt -r requirements-dev.txt` after creating the dev manifest. Use the equivalent venv Python on other platforms. Do not repair unrelated global packages.

## Steps

1. Add tests/test_handlers.py using real aiogram Update/Message/CallbackQuery objects through Dispatcher.feed_update, real temporary SQLite and memory FSM; mock only Telegram transport. Cover full form with optional skips, photo-caption shortcut, invalid-caption fallback, back/restart/cancel, owner admin commands, inactive seller, non-private callback and retry ownership. Assert stored fields and outgoing content. Keep current behavior characterization separate from new regressions in plans 002/003. Verify `python -B -m unittest tests.test_handlers -v` -> baseline flows pass; document any newly found failure before unrelated fixes.
2. Fix phone matching to allow ASCII after existing supported digit conversion, or deliberately normalize all supported decimal digits. Every accepted value must be ASCII and have one canonical representation. Do not silently rewrite stored keys: inspect only aggregate counts in a supplied copy and design repair if needed. Split /admins output on complete rows under Telegram's limit, including empty and maximum-length lists. Add regression cases in test_validation and test_handlers. Verify `python -B -m unittest tests.test_validation tests.test_handlers -v` -> accepted phone invariants and all output-size tests pass.
3. Set and document Python minimum 3.11 (default recommendation), retain 3.12 as canonical deployment runtime. Test declared versions rather than implying all versions are supported. Add dev tooling and a reproducible dependency lock generated in the clean venv; retain a single documented source of dependency truth. Add `ruff check order_bot tests`, `mypy order_bot`, unittest and `pip check` to CI; resolve useful type errors without blanket suppression. These are new gates, not existing audit results. Verify each command exits 0 in the venv and all CI jobs pass.
4. Correct README test claims, document the exact supported runtimes and commands. Add integration-test invocation and point to CI. Verify `python -B -m unittest discover -v` -> all existing/new tests pass and clean-environment `python -m pip check` -> no broken requirements.

## Acceptance and maintenance

CI runs on pull requests with the minimum supported Python and canonical Docker runtime. Tests never connect to Telegram. Unsupported phone digit scripts are rejected or intentionally canonicalized. Admin output never exceeds the limit. Maintain the support matrix whenever changing Python syntax or stdlib APIs. Coverage percentage is optional; executable coverage of important flows is required.

## Stop conditions and git

Stop on schema/data mutation needs, unavailable dependency compatibility, or unexpected source drift. Report instead of installing into global Python or weakening tests. Use branch codex/verification-baseline and atomic commits with the repository's conventional message style, e.g. `test: cover routed order entry`. No push, deployment or PR publication unless separately instructed. Mark TODO -> DONE only after gates pass.
