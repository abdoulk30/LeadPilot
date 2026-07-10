# Security Policy

LeadPilot handles sensitive sales lead data, contact history, and
financial documents (e.g. bank statements). We take security reports
seriously.

## Reporting a vulnerability

Please do not open a public GitHub issue for security vulnerabilities.

Instead, contact the owners directly:
- Marc Delsoin
- Abdoul Ba

(Add a dedicated security contact email here once one exists — e.g.
security@leadpilot.example — rather than personal addresses, once the
project has a domain.)

Please include:
- A description of the vulnerability and its potential impact
- Steps to reproduce, if possible
- Any relevant logs or screenshots (redact any real lead/client data)

## Scope

This policy covers the LeadPilot agent, its tool integrations (Google
Sheets/Drive/Gmail, Twilio, Slack), and any dashboard or API surface.
Of particular interest: prompt injection, any input that causes the
agent to take an unintended action (e.g. an unauthorized send, call
handoff, spreadsheet write, or Slack message), or any way a staged
action could execute without the rep's explicit approval — see the
internal threat model in `leadpilot-docs/security/threat-model.md` for
the categories we're already tracking.

## What to expect

We aim to acknowledge reports within a reasonable timeframe and will
work with you on disclosure timing. A formal SLA will be added here
once the team and process mature.

## Note

This is a public-safe summary. Internal security detail (exact
thresholds, validation logic, incident response procedures) is
intentionally kept in the private `leadpilot-docs` repo, not here.
