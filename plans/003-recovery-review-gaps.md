# Recovery acceptance refinement

2026-09-06 review at executor commit 6c7d603. The broad Plan003 executor exhausted two revision rounds without satisfying its routed regression criteria. Its completion claim is rejected. Execution is refined into the following finite packages, with source changes only when an actual regression demonstrates the bug. No user decision is needed.

## Package R1: three real routed regressions

Scope tests/test_delivery_recovery.py and order_bot/bot.py only if these tests fail. Existing SQLite Database fixtures and production create_dispatcher are available. Never replace a test with assertTrue(True).

1. Confirmation acknowledgement: feed NEW_ORDER and a valid captioned PhotoSize update to reach OrderForm.preview. Extract actual confirmation callback_data from the resulting inline keyboard. Configure the recording transport to throw TelegramBadRequest or TelegramNetworkError only for AnswerCallbackQuery. Inside that failure injection assert an order is already durable and no channel SendPhoto has occurred. Feed the confirmation callback. Assert one persisted sent order, one channel SendPhoto, and cleared confirmed FSM. Use subTest or separate tests for both errors. Calling retry on a pre-saved order does not test this boundary.
2. Pagination: insert eleven orders for seller100 and one for seller101. Feed /recovery for100. Assert exactly ten own references and a recovery:1 button. Feed that callback with callback.from_user=100 and callback.message.from_user set to the BOT. Inspect only newly emitted messages: exactly the eleventh own reference and a previous-page button, no foreign reference/name/phone/address. Click back and verify the first page. Pass malformed and extremely large page payloads through this callback; assert an error response without DB overflow or any send.
3. Restart/mid-form: create a pending row, start NEW_ORDER and verify customer_name FSM before feeding /recovery. Assert the reference is returned and the form state/data remains intact. Construct a fresh Database on the same file and a fresh production dispatcher with empty FSM, clear recording output, then feed /recovery again and verify the durable reference in the new output.

Run the three groups plus Ruff and commit. Report actual assertions and results. Other deficient tests remain explicitly unapproved for package R2.

## Package R2: delivery and reconciliation boundaries

Replace the misleading pending_reconfirmation test by creating a real valid preview, persisting its exact draft token without publication, then feeding its actual confirm callback and asserting a recovery button, one durable row and no second creation. Test ordinary ambiguous retry cannot send, the review-list click only prompts, explicit reconciliation consent does send, and the DB claim rejects ambiguity unless explicitly allowed. At 6c7d603 allow_ambiguous=True was accidentally passed in ordinary retry while reconcile omitted it; correct the call sites and button flow.

Use transport entered/release asyncio.Events and wait_for for concurrent retry tests; no sleep scheduling. Hold the first channel SendPhoto after claim, launch a second attempt, verify one claim/send and in-progress behavior, then release and assert final sent state. For partial delivery, fail the text send after successful photo publication, construct a fresh Database/FSM, retry through the routed callback and prove only text is retried with the saved photo ID retained. Foreign/inactive/private-chat negative cases and sent-row replay must assert both no data leakage and no network/DB side effect. Keep three persistence/query DB regressions removed during the earlier failed revision.

Run targeted and full legacy unittest suite, Ruff, mypy and pip check. Only after both packages pass independently can Plan003 be marked DONE and integrated.
