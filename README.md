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

Early build. Tech stack is not yet finalized — see the docs repo's
`tech-stack/` folder.

## What it does

- Pulls lead data from multiple Google Sheets into one view
- Cross-references contact history (call/text/email) to stop
  duplicate outreach
- Verifies required documents (application, bank statements, prequal
  answers) are present in Google Drive before a deal is marked ready
- Notifies the right back-office stakeholders on Slack the moment a
  file is complete
- Treats all lead-sourced text as literal data, never as instructions
  — hardened against prompt injection by design

## Roadmap

See [ROADMAP.md](./ROADMAP.md) for what's shipped, in progress, and planned.

## Documentation

Product requirements, architecture, security threat model, decisions,
and test plans live in a separate private repo: `leadpilot-docs`. This
repo (`leadpilot`) intentionally does not contain product/security
planning docs — see that repo's README for why.

## Getting started

Setup instructions will be added here once the tech stack is decided.
See `leadpilot-docs/commands/README.md` for the current draft.

## Contributing

See [CONTRIBUTING.md](./CONTRIBUTING.md).

## Security

See [SECURITY.md](./SECURITY.md) to report a vulnerability.

## License

See [LICENSE](./LICENSE).
