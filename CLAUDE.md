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

Step 1 (foundation) is merged to `main`. Step 2 (the actual agent
tools), split between Marc and Abdoul (Decision 032), is in progress
on `abdouls-branch` (Abdoul's half — Group A). Done so far:
`rep_google_credentials` (Decision 026, refresh tokens encrypted via
`crypto.py`, Decision 029), the full `/auth/google/connect|callback|
access-token|grant-file` flow, `GoogleSheetsConnector` reworked for
per-rep OAuth, `agent_run_locks`'s per-rep mutex rework (Decision
027/032), the `fetch_all_leads` tool, the tool-registration scaffold
(`tools/base.py`/`registry.py`, Decision 031), and the `/dev/picker-test`
harness. Not yet built: `fetch_ad_hoc_sheet`, `update_lead_sheet`,
`verify_drive_contents`, `log_call_outcome` (Abdoul's remaining 4),
plus all 6 of Marc's tools (`get_contact_history`, `initiate_lead_call`,
`send_lead_text`, `send_lead_email`, `dispatch_slack_handoff`,
`search_communications`) and the prompt-injection validation layer.

**The full real OAuth flow is now verified live, end to end** — not
just designed on paper. `pytest` shows **83 passed, 0 skipped**
(previously several `test_google_sheets_connector_live.py` and
`test_fetch_all_leads.py` tests auto-skipped pending this). Two real
bugs were caught getting here, both worth knowing before touching this
code:

1. **PKCE code_verifier was being discarded.** `google-auth-oauthlib`'s
   `Flow` generates a PKCE `code_verifier` per instance inside
   `authorization_url()` and needs that exact value back at
   `fetch_token()` time. `/connect` and `/callback` are separate HTTP
   requests, each building its own fresh `Flow` — the verifier was
   being thrown away the moment `/connect` returned. Fixed by signing
   it (same `itsdangerous` pattern as `state`) and carrying it in a
   second cookie, same as `state` already was.
2. **Picker was missing `.setAppId(<project number>)`.** Without it,
   Google's Picker still shows real files and fires a real "picked"
   callback with a valid file ID — the whole flow *looks* successful,
   the ID gets stored correctly — but Google never actually registers
   the `drive.file` per-file grant server-side. The access token
   afterward still can't read the file (404, not 403 — Google's Drive
   APIs return 404 rather than confirm a resource exists to an
   unauthorized caller). Proved this wasn't a code bug first, by
   bypassing the connector entirely with a raw `curl` call using the
   same access token and getting the identical 404 straight from
   Google. `app_id` is just the numeric segment before the first `-`
   in the OAuth client ID (the Cloud project number).

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
- `tests/test_google_sheets_connector_live.py`'s and `test_fetch_all_leads.py`'s live-data tests hit the **real** Google Sheets API against a live test sheet, authenticated as a real connected rep (per-rep OAuth, not a service account — that model is retired). They auto-skip unless a rep in the local dev DB has an active `rep_google_credentials` row with at least one granted file — get one by logging in, then visiting `/dev/picker-test` and clicking through Connect + Pick a sheet for real. Never run these in CI (see `leadpilot-docs/testing/ci-strategy.md`: never let CI make real Google/Slack calls).

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
- `AgentRunLock` — **per-rep** mutex (`rep_id` primary key, reworked from a singleton — Decision 027/032) so the same rep's batch run can't overlap with itself, while two different reps' runs never contend for the same row; has a staleness fallback so a crashed run doesn't block that rep's future runs forever. `fetch_all_leads` manages its own commit boundaries around acquire/release — deliberate, not an inconsistency with `gate.py`'s "caller commits" pattern: the lock only works as a real mutex if its acquisition is visible to other transactions immediately.

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
