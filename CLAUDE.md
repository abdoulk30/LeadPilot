# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this repo is

LeadPilot is an AI sales agent (Claude Agent SDK tool-calling loop) that
consolidates lead data across Google Sheets, tracks contact history to
prevent duplicate outreach, verifies Drive documents for deal handoff,
and drafts outreach/Slack handoffs — but never sends, calls, texts, or
writes anything without explicit rep approval first.

Product/architecture/security planning intentionally does **not** live
here — it lives in the separate private repo `leadpilot-docs` (a sibling
directory, not a subfolder). Always check there before making a
nontrivial change:

- `leadpilot-docs/prd/README.md` → points at the current PRD version
- `leadpilot-docs/decisions/README.md` → why things are built the way
  they are; check before changing established mechanisms
- `leadpilot-docs/tech-stack/stack-overview.md` → the stack is locked
  (Decision 022); don't introduce a different language/framework/datastore
  without a new decision entry first
- `leadpilot-docs/mvp/README.md` → the build order (steps are sequenced
  by dependency). Confirm which step a change falls under before starting
- `leadpilot-docs/architecture/state-schema.md` → the source of truth
  for table/column semantics
- `leadpilot-docs/testing/known-issues-log.md` and `testing/eval-suite.md`

## Current build state

Step 1 (foundation) is merged to `main`: rep auth/sessions, the
contact-history/approval-gate state machine, and dedup/run-lock
tables. Step 2 (the actual agent tools) is in progress on
`abdouls-branch` — done so far: `rep_google_credentials` (Decision 026,
refresh tokens encrypted via `crypto.py`, Decision 029), the
`/auth/google/connect|callback|access-token` endpoints, and
`GoogleSheetsConnector` reworked for per-rep OAuth (no more service
account). Not yet started: `fetch_all_leads`, `send_lead_text`,
`send_lead_email`, `dispatch_slack_handoff`, `update_lead_sheet`,
`verify_drive_contents`, `fetch_ad_hoc_sheet`, `log_call_outcome`,
`search_communications`, the prompt-injection validation layer, and
`agent_run_locks`'s per-rep mutex rework (Decision 027).

**Known gap, not drift:** the real end-to-end OAuth flow (an actual
human clicking through Google's consent screen) hasn't been verified
live yet — blocked on Abdoul's Google account not being on the
project's OAuth test-user allowlist (a Google Cloud Console
config issue, confirmed unrelated to the code: the failed attempt's
echoed request parameters were all correct). `tests/test_google_sheets_connector_live.py`'s
4 live-data tests auto-skip until a rep completes that flow for real —
treat SKIPPED there as "not yet verified," not "broken."
`GOOGLE_SERVICE_ACCOUNT_KEY_PATH`/`GOOGLE_SHEETS_SOURCES` in
`.env.example` are now fully dead — nothing reads them anymore, kept
only as a documented-superseded trail per Decision 026's entry in
`decisions/README.md`.

## Commands

Local dev Postgres (no Docker; isolated data dir/port, not the system Postgres):

```
scripts/devdb.sh init     # first-time setup (idempotent)
scripts/devdb.sh start
scripts/devdb.sh stop
scripts/devdb.sh reset    # wipe and reinitialize
scripts/devdb.sh url      # prints DATABASE_URL to put in .env.local
scripts/devdb.sh psql     # open a psql shell against it
```

Setup:

```
pip install -r requirements.txt
cp .env.example .env.local   # fill in DATABASE_URL (from devdb.sh url) and REP_AUTH_SESSION_SECRET at minimum
alembic upgrade head
```

Run the app:

```
uvicorn leadpilot.app:app --reload --app-dir src   # or use .claude/launch.json's "leadpilot-dashboard" config
```

Tests (config in `pyproject.toml`: `pythonpath=src`, `testpaths=tests`):

```
pytest                                   # full suite
pytest tests/test_gate.py                # single file
pytest tests/test_gate.py::test_approve_then_execute_is_single_use   # single test
```

Notes on the test suite:
- Most tests run against the **real local dev Postgres** (`scripts/devdb.sh`), not mocks — the approval-gate and lock logic depend on real transaction/row-locking semantics a mock can't verify. Start `devdb.sh` before running tests.
- `tests/test_google_sheets_connector_live.py` hits the **real** Google Sheets API against a live test sheet with real credentials. It auto-skips unless `GOOGLE_SERVICE_ACCOUNT_KEY_PATH` is set to a real, existing file — never run this in CI (see `leadpilot-docs/testing/ci-strategy.md`: never let CI make real Google/Slack calls).

Migrations:

```
alembic revision --autogenerate -m "description"   # after changing a model — remember to import new model modules in alembic/env.py
alembic upgrade head
```

## Architecture

**Two-process split, one database.** The Web Service (FastAPI, rep-facing)
and the Cron Job (hourly agent batch run) are separate processes that
both read/write the same `DATABASE_URL`. This is what makes the
approval-gate conditional update actually enforce anything across them —
there's no in-memory state to coordinate.

**The approval gate is a state machine on one table, not a token.**
`contact_history` rows move `drafted → awaiting_rep_approval → approved →
executed` (or `rejected`/`expired`). Every transition in `gate.py` is a
single atomic conditional `UPDATE ... WHERE stage = <expected prior stage>`.
This is the entire enforcement mechanism for "the agent must never act
without rep approval" (Decision 021) — there is no separate signed
token object anywhere, and none should be added without a new decision
entry. `try_execute()` flipping `approved → executed` is the only thing
that authorizes a tool to perform its real side effect (send the text,
post to Slack, write the sheet, copy a number to the clipboard for a call).

**Locks are atomic INSERT ... ON CONFLICT, not SELECT-then-UPDATE**
(`locks.py`). Two distinct locks solve two distinct threats
(`security/threat-model.md` in the docs repo):
- `LeadActionLock` — per-lead cooldown, prevents double-dialing/texting the same lead from overlapping runs.
- `AgentRunLock` — singleton mutex so the hourly Cron Job can't run twice concurrently; has a staleness fallback so a crashed run doesn't block all future runs forever. (Flagged in `leadpilot-docs` Decision 027 as needing to become a *per-rep* mutex once the batch run is reworked from a single global run to one run per connected rep — not yet implemented.)

**`LeadSourceConnector` (`connectors/base.py`) is the abstraction over lead sources**, currently only implemented by `GoogleSheetsConnector`. Its `write_field` is deliberately split into two methods rather than one gated method:
- `stage_field_write()` — computes and returns a diff, never writes.
- `commit_field_write()` — performs the real write; callers must have already confirmed `gate.try_execute()` returned `True` for the corresponding `contact_history` row. The connector itself has zero opinion about approval state.

**Auth is DB-backed sessions, not JWTs** (`auth.py`, `models/rep.py`). A
session row can be revoked immediately (logout, deactivation) rather than
waiting for a stateless token to expire; the cookie value is additionally
signed (`itsdangerous`) as a cheap tamper check on top of the session
row's own randomness. `require_rep()` in `app.py` is the actual
enforcement point for the "AUTHENTICATION GUARD" system-prompt
requirement — any endpoint touching lead/contact data should depend on
it rather than reading the session cookie directly.

**Config** (`config.py`) reads `.env.local` first, falling back to `.env`,
then real process env vars (what Render sets in prod) — see `db.py` for
where `DATABASE_URL` gets consumed into the SQLAlchemy engine.
