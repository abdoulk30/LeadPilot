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

    # Decision 026 (leadpilot-docs, 2026-07-11) supersedes Decision 024:
    # Sheets/Drive/Gmail all authenticate per-rep via this OAuth client
    # (drive.file scope + Google Picker), not a service account. Not
    # wired into GoogleSheetsConnector yet — that rework is real Step 2
    # work (leadpilot-docs mvp/README.md). These fields just make the
    # values loadable now so Step 2 has something to build against.
    google_oauth_client_id: str = ""
    google_oauth_client_secret: str = ""
    google_picker_api_key: str = ""
    # Where Google redirects after consent — must exactly match a URI
    # registered on the OAuth client in Google Cloud Console. No
    # default: wrong in prod if silently assumed, so require it be set.
    google_oauth_redirect_uri: str = ""

    # Fernet key (leadpilot.crypto) encrypting rep_google_credentials.
    # refresh_token_encrypted. Generate with:
    #   python3 -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
    credential_encryption_key: str = ""

    # Superseded by Decision 026 — still read by the as-shipped Step 1
    # GoogleSheetsConnector for local dev until Step 2's rework lands.
    # Do not build anything new against these two.
    google_service_account_key_path: str = ""
    # JSON mapping of source_id -> Google Sheet ID, e.g.
    # {"inbound_sheet_a": "1Byr..."}. A real "sources" config table is
    # overkill for Step 1 with a single test sheet — revisit once
    # there's more than one real source to configure.
    google_sheets_sources: str = "{}"

    def google_sheets_sources_map(self) -> dict[str, str]:
        return json.loads(self.google_sheets_sources)

    # Twilio — send_lead_text and the SMS half of search_communications
    # (Step 2, not built yet). Trial account: can only send to verified
    # numbers until upgraded.
    twilio_account_sid: str = ""
    twilio_auth_token: str = ""
    twilio_from_number: str = ""

    # Slack — dispatch_slack_handoff (Step 2, not built yet). Only the
    # bot token is needed for chat.postMessage; the App ID/Client ID+
    # Secret/Signing Secret/Verification Token from Slack's app config
    # page are for OAuth-install and event-subscription flows this
    # product doesn't use (no interactivity in Phase 1 — see
    # leadpilot-docs/tech-stack/stack-overview.md) and aren't stored
    # here for that reason, not because they were lost.
    slack_bot_token: str = ""
    # Comma-separated channel/user IDs — real 3-stakeholder list isn't
    # finalized yet (business decision, not a Step 0 blocker); a single
    # test channel ID is fine for local dev.
    slack_handoff_channel_ids: str = ""


settings = Settings()
