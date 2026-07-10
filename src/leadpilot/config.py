"""Environment configuration.

Reads from .env.local (the file already used in this repo — see
.gitignore) falling back to real process environment variables (e.g.
what Render sets in production). Never commit real values — see
.env.example for the documented variable list.
"""

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

    google_oauth_client_id: str = ""
    google_oauth_client_secret: str = ""


settings = Settings()
