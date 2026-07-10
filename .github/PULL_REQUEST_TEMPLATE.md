## What this changes

## Why

Link to a decisions/ entry, known-issues-log.md item, or PRD section
if applicable.

## Evidence this works

Per `leadpilot-docs/testing/definition-of-done.md` — paste actual
output, not a description of expected output.

- [ ] Eval suite (`leadpilot-docs/testing/eval-suite.md`) — all 10
      cases pass
- [ ] If this touches the prompt-injection guard, duplicate-contact
      locking, the approval-gate conditional update (Decision 021), the
      authenticated-session check (Decision 013), or the Slack
      stakeholder list: relevant
      `leadpilot-docs/security/pen-test-checklist.md` items pass
- [ ] Docs updated if this changes architecture, tools, or the system
      prompt (`leadpilot-docs/architecture/`, `leadpilot-docs/prd/`)
- [ ] Decision logged in `leadpilot-docs/decisions/README.md` (the
      decisions log) if this changes established behavior

## Reviewer notes

Anything the reviewer should pay special attention to.
