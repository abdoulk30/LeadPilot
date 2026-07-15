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

Step 1 (foundation) and Step 2 (all 11 agent tools, split between
Marc and Abdoul per Decision 032) are both **merged to `main`** as of
2026-07-13 (commit `41a7bd9` — `abdouls-branch` + `marc-step2-split`
combined; one real conflict, confined to `google_oauth.py`'s docstring
and `SCOPES` list, resolved). Group A (Abdoul): `fetch_all_leads`,
`fetch_ad_hoc_sheet`, `update_lead_sheet`, `verify_drive_contents`,
`log_call_outcome`, plus `rep_google_credentials` (Decision 026,
refresh tokens encrypted via `crypto.py`, Decision 029), the full
`/auth/google/connect|callback|access-token|grant-file` flow,
`GoogleSheetsConnector` reworked for per-rep OAuth,
`agent_run_locks`'s per-rep mutex rework (Decision 027/032), the
tool-registration scaffold (`tools/base.py`/`registry.py`, Decision
031), and the `/dev/picker-test` harness (now also supports granting a
Drive folder, not just a sheet). Group B (Marc): `get_contact_history`,
`initiate_lead_call`, `send_lead_text`, `send_lead_email`,
`dispatch_slack_handoff`, `search_communications`. The
prompt-injection validation layer is also built (`injection_guard.py`,
Decision 006, hooked into `lead_ingest`).

**Step 4 (the agent loop) is built on `marc-step4-agent-loop`**
(2026-07-14, Decision 037): `src/leadpilot/agent_loop.py` runs PRD
v1.06 §3b verbatim (frozen, cache-controlled system prompt) through a
hand-rolled Messages-API tool loop — deliberately not the Agent
SDK/tool-runner helpers, because the guards hook every tool call:
`rep_id` is stripped from model-visible schemas and injected
server-side, `LeadActionLock` (1h cooldown) gates outreach drafts
(Decision 007's missing caller), and the batch tool surface is
steps 1–6 only — no execute path, no `log_call_outcome` (an unattended
agent must not fabricate rep-reported outcomes). `agent_run.py` is the
cron entrypoint (`python -m leadpilot.agent_run`): per-rep iteration,
whole-run AgentRunLock (`fetch_all_leads(manage_run_lock=False)`),
per-run audit rows in `agent_run_reports`. Model: `claude-opus-4-8`
via `ANTHROPIC_MODEL`. Live evals: `python scripts/run_evals.py`
(real model, faked Google, ~$1–2 a sweep — never CI); results logged
in `leadpilot-docs/testing/eval-suite.md`. Two live findings worth
remembering: `output_config` structured outputs suppressed tool
calling entirely, and the model refuses to run unless the kickoff
asserts the authenticated session and lists granted item ids. Still
to do: deploy the actual Render Cron Job.

**Step 3 (the interface) is built on `marc-interface-build`**
(2026-07-14, Decision 036): server-rendered Jinja2 + htmx workspace —
templates in `src/leadpilot/templates/`, routes in `src/leadpilot/ui.py`
(same auth chain as the JSON API, redirect semantics), queue assembly
+ interim ranking in `src/leadpilot/queue_builder.py`, static assets
(glass CSS token system, 7 themes, vendored htmx) in
`src/leadpilot/static/`. Approve buttons call `gate.approve()` + the
tool's own `execute_*()`; no second approval mechanism exists.
`scripts/seed_demo_data.py` seeds a demo rep + leads for manual
walkthroughs (run `--wipe` before the full pytest suite — some
fetch_all_leads tests count all leads in the DB). Design source of
truth: `leadpilot-docs/context-files/leadpilot_interface_design_spec_v001.md`;
autonomous build decisions:
`leadpilot-docs/design/interface-build-decisions-v001.md`. Two
backend fixes rode along: `execute_initiate_lead_call` now sets
`outcome=PENDING` (log_call_outcome's documented contract — nothing
actually set it), and `/auth/google/callback` 303-redirects into the
workspace instead of returning JSON to a browser navigation.

**Post-merge fix required and applied (2026-07-13, commit `3dc3a52`):**
Marc's Decision 034 (same-cell concurrent-write protection) changed
`GoogleSheetsConnector.commit_field_write`'s signature to require a new
`expected_current` keyword argument with no default. `update_lead_sheet.py`
wasn't updated to match — it would have raised `TypeError` on any real
write, masked in tests because `tests/fakes.py`'s fake connector was
also stale. Fixed: `update_lead_sheet` now persists the reviewed
`current` value at staging time and passes it through at execute time;
`StaleWriteError`/`ConcurrentWriteError` propagate distinctly rather
than getting wrapped into a generic failure; `tests/fakes.py` now
enforces the same staleness check the real connector does. See
`leadpilot-docs/testing/known-issues-log.md` Issue 006 and
`leadpilot-docs/decisions/README.md` Decision 034 for full detail.

**Full suite on the merged `main`: 182 passed.** The only failures (9)
are live OAuth tests hitting `invalid_scope` — expected, not a code
bug: Decision 033/034 widened `SCOPES` (added `drive.readonly`,
`gmail.send`, `gmail.readonly`), so any rep who connected under an
older scope list (including the rep used for local dev testing) must
redo the "Connect Google Account" flow before those tests will pass
again. `alembic heads` shows a single unified head (`fed4e55c9f58`) —
Abdoul's `agent_run_locks` migration (`cd645f125bf4`) and Marc's
`sheet_cell_locks` migration (`fed4e55c9f58`) turned out to be sibling
migrations off the same prior head, and chained cleanly with no manual
`alembic merge` needed once both branches landed together.

**The full real OAuth flow is verified live, end to end** — not just
designed on paper. Two real bugs were caught getting here, both worth
knowing before touching this code:

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
