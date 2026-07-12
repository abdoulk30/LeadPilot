"""verify_drive_contents — PRD v1.05 §3a/§3c step 4. Inspects a target
Google Drive folder (e.g. a lead's application-documents folder) to
check what's actually present — file names, MIME types, sizes, creation
timestamps — so downstream logic (the system prompt's workflow-
completeness check) can tell a real bank statement from a missing or
invalid one. Read-only; no approval gate needed — PRD 3a's execution-
gating rule groups this with fetch_all_leads/fetch_ad_hoc_sheet as
needing none.

Same per-rep OAuth model as GoogleSheetsConnector (Decision 026): scoped
to whatever folder_id that specific rep granted via the Picker
(leadpilot.google_credentials.granted_file_ids) — never another rep's,
never a shared/standing credential (DATA ACCESS GUARD, PRD 3c). If the
rep hasn't granted this folder yet, GoogleDriveClient's own "not
granted" ValueError surfaces naturally, same as fetch_ad_hoc_sheet does
for an ungranted sheet.
"""

import uuid

from sqlalchemy.orm import Session

from leadpilot.connectors.google_drive import DriveContentsClient, GoogleDriveClient
from leadpilot.tools.base import tool


@tool(
    name="verify_drive_contents",
    description=(
        "Inspects a Google Drive folder the rep has granted access to and returns what's actually "
        "present — file names, MIME types, sizes, and creation timestamps — for checking document "
        "completeness (e.g. application form, bank statements, questionnaires)."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "rep_id": {"type": "string", "description": "The requesting rep's UUID"},
            "folder_id": {"type": "string", "description": "The Google Drive folder ID to inspect"},
        },
        "required": ["rep_id", "folder_id"],
    },
)
def run(
    session: Session, rep_id: uuid.UUID, folder_id: str, client: DriveContentsClient | None = None
) -> list[dict]:
    client = client or GoogleDriveClient(session, rep_id)
    files = client.list_folder_contents(folder_id)
    return [
        {
            "file_id": f.file_id,
            "name": f.name,
            "mime_type": f.mime_type,
            "size_bytes": f.size_bytes,
            "created_time": f.created_time,
        }
        for f in files
    ]
