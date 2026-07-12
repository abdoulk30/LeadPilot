"""GoogleDriveClient — Drive API access for verify_drive_contents (PRD
v1.05 3a), authenticated per-rep the same way GoogleSheetsConnector is
(Decision 026: `drive.file` scope, no service account, no shared/
standing credential).

Not built against the LeadSourceConnector interface (connectors/base.py)
— that abstraction is specifically for lead-row sources (3e: fetch,
dedup, field-write a row). Listing a folder's contents is a different
shape of operation entirely, so this gets its own minimal interface
(DriveContentsClient) instead of forcing an ill-fitting shared one.

folder_id is validated against the same
leadpilot.google_credentials.granted_file_ids list GoogleSheetsConnector
checks source_id against — the Picker grants access to folders the same
way it grants access to individual sheets, and both live in one flat
per-rep list.
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


_FOLDER_MIME_TYPE = "application/vnd.google-apps.folder"


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

    def is_folder(self, file_id: str) -> bool:
        """Real Drive-side check, not a guess from the ID string —
        files.list with a '<id> in parents' query doesn't error for a
        non-folder ID, it just returns no results, so callers that need
        to tell "this granted ID is a folder" apart from "this granted
        ID is a sheet with nothing found under it" need this instead.
        """
        meta = self._client().files().get(fileId=file_id, fields="mimeType").execute()
        return meta.get("mimeType") == _FOLDER_MIME_TYPE

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
