---
name: verify-telegram-keyboards
description: Verify Telegram callback retirement after changes to the bot callback handler, recovery button IDs, or keyboard edits.
---

Once Telegram accepts a keyboard edit, later edits must keep its retired controls gone. An action that advances recovery must retire all controls for that revision and step.

```sh
uv sync --locked --extra app
uv run --no-sync pytest -q tests_app/test_keyboard_model.py tests_app/test_bot.py tests_app/test_recovery_bot.py
```

The Python model runs all 384 permutations and edit outcomes for four overlapping taps on one message. Every tap carries the same original keyboard. The first request is held while the other taps start. The model compares the controls in each accepted edit with the real keyboard owner over the HTTP test transport. The normal core/app CI suite runs it. Restart, row order, and a server that applies an edit but loses its response lie outside this model.

| Property | Code | Historical failure |
| --- | --- | --- |
| Later edits keep earlier removals | `TelegramKeyboards.retire` in `app/tcc_app/keyboards.py` | The review of PR #12 showed that two taps could restore each other's controls. |
| Recovery invalidates sibling controls | `_retirement_key` in the same file and `recovery_progress_buttons` in `app/tcc_app/commands.py` | Boarding or stopping left a sibling tied to the old revision and step. |
| A reply failure leaves the keyboard alone | `Bot._button` and `Bot._reply` in `app/tcc_app/bot.py` | A successful action removed its control even when its reply failed. The app test drives this path through Telegram HTTP. |

In a scratch checkout, replace the accumulated set in `TelegramKeyboards.retire` with a fresh set per tap. The model fails when a skip edit restores the leaving button. Restore it, then use the full callback string in `_retirement_key`. The model fails when boarding leaves the old stop button visible. Restore it and rerun. Remove the reply-success condition only on a scratch checkout. The failed-reply app test must see an unexpected keyboard edit.

Sync commit for the executable checks and implementation is `ba663fb1b244e7ce76081e1ee7eac9f99060db85`. Advance it after matching the model to the covered paths and reproducing the relevant historical failure.
