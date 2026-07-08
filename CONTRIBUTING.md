# Contributing to LeadPilot

LeadPilot is currently a two-person project (Marc Delsoin, Abdoul Ba).
This file is written for that reality now, and to scale cleanly if the
team grows.

## Before you start

- Read the PRD and current architecture in the `leadpilot-docs` repo
  (separate, private repo — ask an owner for access).
- Check `leadpilot-docs/decisions/decisions-log.md` for context on why
  things are built the way they are before changing them.
- Check `leadpilot-docs/testing/known-issues-log.md` for open issues
  before starting new work that might depend on them.

## Development workflow

1. Branch from `main` (branch naming convention TBD — suggest
   `feature/short-description` or `fix/short-description`)
2. Make your change
3. Run the eval suite (`leadpilot-docs/testing/eval-suite.md`) locally
   — all 3 cases must pass before opening a PR
4. Open a PR using the template in `.github/PULL_REQUEST_TEMPLATE.md`
5. At least one other owner reviews before merge (see CODEOWNERS)

## Definition of done

Every change should meet the bar in
`leadpilot-docs/testing/definition-of-done.md` — evidence of actual
verification, not just "looks right."

## Security-sensitive changes

Any change touching the prompt-injection validation layer, the
duplicate-contact locking logic, or the Slack handoff stakeholder list
requires running `leadpilot-docs/security/pen-test-checklist.md` in
addition to the standard eval suite, and should be flagged explicitly
in the PR description.

## Commit messages

Write commit messages that explain why, not just what — matching the
reasoning-preserved standard used throughout the docs repo's decisions
log.
