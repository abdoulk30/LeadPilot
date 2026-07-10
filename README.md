# LeadPilot

An AI agent for B2B sales and business development teams. LeadPilot
orchestrates lead triage and multi-channel communication pipelines
across Google Workspace (Sheets, Drive) and Slack, running on a
persistent hourly schedule — parsing disparate lead sheets, auditing
contact history to prevent duplicate outreach, verifying document
completeness for deal handoff, and surfacing a prioritized queue to
the sales rep.

Owners: Marc Delsoin, Abdoul Ba

## Status

Pre-build. Tech stack is locked (Python + Claude Agent SDK, FastAPI,
Postgres via Neon, Render — see the docs repo's
`tech-stack/stack-overview.md`, Decision 022), but no application code
exists yet — see the docs repo's `mvp/README.md` build order for what's
next.

## What it does

- Pulls lead data from multiple Google Sheets into one view
- Cross-references contact history (call/text/email/Slack/sheet-edit)
  to stop duplicate outreach
- Verifies required documents (application, bank statements, prequal
  answers) are present in Google Drive before a deal is marked ready
- Drafts lead outreach (call, text, email) and a back-office Slack
  handoff the moment a file is complete — but never sends, calls, or
  writes anything without the rep explicitly approving it first; for
  calls specifically, approval copies the number to the rep's
  clipboard rather than placing any call
- Searches a client's email/text history by name, company, email, or
  phone number
- Treats all lead-sourced text as literal data, never as instructions
  — hardened against prompt injection by design

## Documentation

Product requirements, architecture, security threat model, decisions,
and test plans live in a separate private repo: `leadpilot-docs`. This
repo (`leadpilot`) intentionally does not contain product/security
planning docs — see that repo's README for why.

## Getting started

Setup instructions will be added here once there's real code to run.
See `leadpilot-docs/commands/README.md` for the current draft commands
(Python/pytest/Render, per the locked stack).

## Contributing

See [CONTRIBUTING.md](./CONTRIBUTING.md).

## Security

See [SECURITY.md](./SECURITY.md) to report a vulnerability.

## License

See [LICENSE](./LICENSE).
