---
name: verify-commute-writes
description: Verify failed and retried commute saves after changes to StateWriter or the runtime paths that submit skips, polls, and timetable updates.
---

A skip stays in effect once the app saves it. A failed save leaves the saved commute active. A later successful save must apply the requested skip.

Run from the repository root.

```sh
uv sync --locked --extra app
uv run --no-sync pytest -q tests/test_writer_model.py tests/test_writer_transitions.py
uv run --no-sync pytest -q tests/test_runtime.py -k 'lookahead_written_after_a_failed_poll'
```

The Python model checks all 4,680 traces of one to four operations. Each operation is a skip, poll, look-ahead write, or retry with storage up or down. The model compares live and saved skip status after every operation against the real writer. The writer is serial. The bound covers one occurrence and no explicit reversal of a skip. The test uses an in-memory store. The other checks use the production JSON store and a fresh runtime or store instance.

| Property | Code | Counterexample |
| --- | --- | --- |
| A pending skip survives a successful timetable write | `StateWriter.commit`, `_write`, `_compose`, and `_pend` in `src/train_commute_companion/state_writer.py` | Commit `6fb09ab` fixed a failed skip, a failed replacement poll, and a successful look-ahead write that restored the trip. |
| An archived commute leaves a history row on disk | `_pending_archives`, `_pend`, and `_restage` in the same file | The retry behavior before `194882d` lost the archive or removed the trip without saving history. |
| A rejected strict action stays out of later saves | `StateWriter.commit_strict` and `retry` in the same file | The file-backed test fails if retry or a later write applies a strict action that failed. |

To replay the skip bug on a scratch branch, build the kept change from the result of restaging in `StateWriter.commit`. The shortest failing trace is `skip (disk full) → poll (disk full) → lookahead (saved)`. Restore the fresh proposal and rerun. To replay the archive bug, restore the stage predicate and history merge from the parent of `194882d`. The two archive cases fail at different disk assertions.

Sync commit for the executable checks and covered implementation is `76f59d38f3b027c6fcf95521e588589b95e6513a`. Review changes to the named functions and the runtime calls to `commit` or `retry`. Advance the sync commit only after matching the model to those calls and replaying the relevant historical failure.
