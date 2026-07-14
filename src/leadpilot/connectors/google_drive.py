"""GoogleDriveClient — Drive API access for verify_drive_contents (PRD
v1.05 3a), authenticated per-rep the same way GoogleSheetsConnector is
(no service account, no shared/standing credential).

Uses the `drive.readonly` scope (Decision 033), not `drive.file`.
`drive.file`'s per-item Picker grant was the original design (Decision
026), but it turned out not to extend to a folder's contents — granting
a folder via Picker does not grant visibility into the files inside it,
confirmed against the real Drive API and matching reports from other
`drive.file` users hitting the same wall. `drive.readonly` genuinely
widens what this rep's token can read across their whole Drive, which
is a real, deliberate tradeoff (bigger blast radius than Decision 026's
original "only what's explicitly shared, one item at a time" model) —
not something to quietly forget. **Revisit this**: Decision 033 flags
it for a narrower alternative later if Google adds one (e.g. a
folder-scoped variant of drive.file that actually cascades to
children), rather than treating `drive.readonly` as the permanent
answer.

To limit what the *product* actually does with that broader read
access, `_folder_id_for` still only allows folder_ids the rep
explicitly granted via the Picker (leadpilot.google_credentials.
granted_file_ids) — the credential can technically read more, but
verify_drive_contents will still only ever look at a folder the rep
pointed it at, same DATA ACCESS GUARD behavior as before. `drive.file`
stays in SCOPES (google_oauth.py) alongside `drive.readonly` — it's
still what GoogleSheetsConnector's write path needs.

Not built against the LeadSourceConnector interface (connectors/base.py)
— that abstraction is specifically for lead-row sources (3e: fetch,
dedup, field-write a row). Listing a folder's contents is a different
shape of operation entirely, so this gets its own minimal interface
(DriveContentsClient) instead of forcing an ill-fitting shared one.

Passes supportsAllDrives/includeItemsFromAllDrives on every Drive API
call — without them, the API silently excludes Shared Drive content
from results even with sufficient permission, a separate gotcha from
the drive.file/drive.readonly scope question (confirmed via Google's
own Drive API docs while investigating this).
"""

import uuid
from abc import ABC, abstractmethod
from dataclasses import dataclass

from google.oauth2.credentials import Credentials as GoogleCredentials
from googleapiclient.discovery import build
from sqlalchemy.orm import Session

from leadpilot import google_credentials, google_oauth


@dataclass
class DriveFileInfo:
    file_id: str
    name: str
    mime_type: str
    size_bytes: int | None
    created_time: str | None


class DriveContentsClient(ABC):
    @abstractmethod
    def list_folder_contents(self, folder_id: str) -> list[DriveFileInfo]:
        """List the files directly inside folder_id (non-recursive)."""


FOLDER_MIME_TYPE = "application/vnd.google-apps.folder"
SPREADSHEET_MIME_TYPE = "application/vnd.google-apps.spreadsheet"


class RepNotConnectedError(ValueError):
    """Same distinction as GoogleSheetsConnector.RepNotConnectedError —
    lets callers tell "rep hasn't connected Google at all" apart from
    "connected, but hasn't granted this specific folder_id".
    """


class GoogleDriveClient(DriveContentsClient):
    def __init__(self, session: Session, rep_id: uuid.UUID):
        self._session = session
        self._rep_id = rep_id
        self._service = None

    def _client(self):
        if self._service is None:
            access_token = google_oauth.get_fresh_access_token(self._session, self._rep_id)
            if access_token is None:
                raise RepNotConnectedError(f"Rep {self._rep_id} has not connected a Google account")
            creds = GoogleCredentials(token=access_token)
            self._service = build("drive", "v3", credentials=creds)
        return self._service

    def mime_type(self, file_id: str) -> str:
        """Real Drive-side check, not a guess from the ID string or
        file extension. Used both for is_folder() below and by
        GoogleSheetsConnector.list_sources() to filter out folder IDs
        that ended up in a rep's granted_file_ids list once folder
        grants for verify_drive_contents started living in that same
        flat per-rep list (Decision 026) — the Sheets API 400s if
        handed a folder ID as if it were a spreadsheet.
        """
        meta = (
            self._client()
            .files()
            .get(fileId=file_id, fields="mimeType", supportsAllDrives=True)
            .execute()
        )
        return meta.get("mimeType")

    def is_folder(self, file_id: str) -> bool:
        """files.list with a '<id> in parents' query doesn't error for
        a non-folder ID, it just returns no results, so callers that
        need to tell "this granted ID is a folder" apart from "this
        granted ID is a sheet with nothing found under it" need this
        instead.
        """
        return self.mime_type(file_id) == FOLDER_MIME_TYPE

    def _folder_id_for(self, folder_id: str) -> str:
        granted = google_credentials.granted_file_ids(self._session, self._rep_id)
        if folder_id not in granted:
            raise ValueError(
                f"Rep {self._rep_id} has not granted access to folder_id {folder_id!r}. Granted: {granted}"
            )
        return folder_id

    def list_folder_contents(self, folder_id: str) -> list[DriveFileInfo]:
        fid = self._folder_id_for(folder_id)
        result = (
            self._client()
            .files()
            .list(
                q=f"'{fid}' in parents and trashed = false",
                fields="files(id, name, mimeType, size, createdTime)",
                supportsAllDrives=True,
                includeItemsFromAllDrives=True,
            )
            .execute()
        )
        return [
            DriveFileInfo(
                file_id=f["id"],
                name=f["name"],
                mime_type=f["mimeType"],
                size_bytes=int(f["size"]) if "size" in f else None,
                created_time=f.get("createdTime"),
            )
            for f in result.get("files", [])
        ]
