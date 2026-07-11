# Changelog

All notable changes to LeadPilot will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/),
and this project intends to adhere to [Semantic Versioning](https://semver.org/)
once a first release is cut.

## [Unreleased]

### Added
- Initial repo scaffold: README, PRD, CONTRIBUTING, LICENSE
  (placeholder), CODEOWNERS, issue/PR templates, CI workflow stub
- Docs repo (`leadpilot-docs`) established as the source of truth for
  product, architecture, security, and decisions
- Step 1 foundation, built by Abdoul on `abdouls-branch`
  (2026-07-09/10) and merged to `main` by Marc 2026-07-10
  (`cc4c8ac`): contact-history + approval-gate table (`leadpilot/gate.py`),
  dedup/run-lock tables (`leadpilot/models/dedup.py`, `run_lock.py`,
  `leadpilot/locks.py`), authenticated rep-session system
  (`leadpilot/auth.py`, `leadpilot/app.py`, Decision 013/023), and
  `GoogleSheetsConnector` implementing `LeadSourceConnector`
  (Decision 015/024) — see `leadpilot-docs/mvp/README.md` Step 1 for
  test-file evidence per item

### Changed
- Synced scaffold (README, CONTRIBUTING, SECURITY, `.env.example`, PR
  template, CI workflow comments) to Decision 022 (tech stack locked)
  and PRD v1.04 (10 tools, Google Voice dependency fully retired) —
  no functional change, docs/scaffold accuracy only

### Notes
- Step 1 (foundation) is merged to `main` as of 2026-07-10 — see
  `leadpilot-docs/mvp/README.md` Step 1 for what's built and verified.
  Tech stack is locked (Decision 022: Python + Claude Agent SDK,
  FastAPI, Postgres via Neon, Render — see
  `leadpilot-docs/tech-stack/stack-overview.md`); Step 2 (the tools)
  hasn't started — see `leadpilot-docs/mvp/README.md` build order
