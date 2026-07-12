"""Application-level encryption for values that must never sit in
plaintext in Postgres — currently just each rep's Google OAuth refresh
token (models/rep_google_credential.py).

Decision (flagging for Marc, not yet in decisions/README.md): symmetric
Fernet encryption (the `cryptography` library) keyed by
CREDENTIAL_ENCRYPTION_KEY, not pgcrypto or a cloud KMS. Matches
Decision 021's "simplest correct mechanism" precedent — this is a
single unattended service with one encryption key, not a multi-tenant
system needing per-tenant key isolation or a KMS's audit trail. A
compromised CREDENTIAL_ENCRYPTION_KEY is the actual incident-scale
rotation event (re-encrypt every stored token, force every rep to
reconnect) — see security/secrets-rotation-runbook.md in the docs
repo, which already anticipated this for the refresh-token column
before this file existed.
"""

from cryptography.fernet import Fernet, InvalidToken

from leadpilot.config import settings


def encrypt(plaintext: str) -> str:
    return Fernet(settings.credential_encryption_key.encode()).encrypt(plaintext.encode()).decode()


def decrypt(ciphertext: str) -> str:
    try:
        return Fernet(settings.credential_encryption_key.encode()).decrypt(ciphertext.encode()).decode()
    except InvalidToken as e:
        raise ValueError("Could not decrypt — wrong key, or the value was never Fernet-encrypted") from e
