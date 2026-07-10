"""Environment configuration.

Reads from .env.local (the file already used in this repo — see
.gitignore) falling back to real process environment variables (e.g.
what Render sets in production). Never commit real values — see
.env.example for the documented variable list.
"""

import json

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=(".env.local", ".env"),
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # No default — a relative socket-dir path isn't reliable across
    # working directories. Run `scripts/devdb.sh url` and put the
    # result in .env.local (see .env.example).
    database_url: str

    rep_auth_session_secret: str = ""

    # commands/README.md originally documented GOOGLE_OAUTH_CLIENT_ID/
    # SECRET for all Google access. For fetch_all_leads/update_lead_sheet
    # specifically, a service account is the right credential type —
    # this is an unattended Cron Job, not a per-user consent flow (see
    # GoogleSheetsConnector's module docstring). GOOGLE_OAUTH_CLIENT_ID/
    # SECRET are kept below since Gmail-as-the-rep (Step 2,
    # send_lead_email) is a genuinely different, per-rep-consent case —
    # confirm with Marc before assuming both are needed long-term.
    google_oauth_client_id: str = ""
    google_oauth_client_secret: str = ""

    google_service_account_key_path: str = ""
    # JSON mapping of source_id -> Google Sheet ID, e.g.
    # {"inbound_sheet_a": "1Byr..."}. A real "sources" config table is
    # overkill for Step 1 with a single test sheet — revisit once
    # there's more than one real source to configure.
    google_sheets_sources: str = "{}"

    def google_sheets_sources_map(self) -> dict[str, str]:
        return json.loads(self.google_sheets_sources)


settings = Settings()
