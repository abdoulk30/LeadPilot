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

### Changed
- Synced scaffold (README, CONTRIBUTING, SECURITY, `.env.example`, PR
  template, CI workflow comments) to Decision 022 (tech stack locked)
  and PRD v1.04 (10 tools, Google Voice dependency fully retired) —
  no functional change, docs/scaffold accuracy only

### Notes
- No functional agent code yet. Tech stack is locked (Decision 022:
  Python + Claude Agent SDK, FastAPI, Postgres via Neon, Render — see
  `leadpilot-docs/tech-stack/stack-overview.md`); implementation
  hasn't started — see `leadpilot-docs/mvp/README.md` build order
