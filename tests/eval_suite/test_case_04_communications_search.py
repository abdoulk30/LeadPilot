"""testing/eval-suite.md Case 4 — Communications search.

Full fix + detailed test coverage lives in tests/test_search_communications.py
(search_communications was reworked 2026-07-14 to resolve a searched
identifier to its Lead record and expand the search across every known
identifier that lead has — it previously searched only the literal
string provided). This file proves the exact case scenario end-to-end
in one place, so `pytest tests/eval_suite/` is self-contained.
"""

import uuid

from leadpilot import auth
from leadpilot.models.leads import Lead
from leadpilot.tools.search_communications import search_communications


class _FakeResponse:
    def __init__(self, data):
        self._data = data

    def execute(self):
        return self._data


class FakeGmailService:
    def __init__(self, list_response, get_responses):
        self.list_response = list_response
        self.get_responses = get_responses
        self.list_calls: list[dict] = []

    def users(self):
        return self

    def messages(self):
        return self

    def list(self, *, userId, q):
        self.list_calls.append({"userId": userId, "q": q})
        return _FakeResponse(self.list_response)

    def get(self, *, userId, id, format=None, metadataHeaders=None):
        return _FakeResponse(self.get_responses[id])


def _make_rep(session) -> uuid.UUID:
    rep = auth.create_rep(session, email=f"{uuid.uuid4()}-eval-case-4@example.com", password="testpassword123")
    session.flush()
    return rep.rep_id


def test_case_4_communications_search_across_all_identifiers(db_session):
    rep_id = _make_rep(db_session)
    lead = Lead(
        display_name="John Doe", primary_phone="+15550100001",
        primary_email="john.doe@example.com", company="Doe Roofing",
    )
    db_session.add(lead)
    db_session.flush()

    # A message that only mentions John Doe's email — not his phone
    # number, which is the identifier the rep actually searched with.
    gmail = FakeGmailService(
        list_response={"messages": [{"id": "m1"}]},
        get_responses={
            "m1": {
                "id": "m1",
                "snippet": "Attaching the requested bank statement.",
                "payload": {
                    "headers": [
                        {"name": "From", "value": "john.doe@example.com"},
                        {"name": "To", "value": "rep@leadpilot.example"},
                        {"name": "Subject", "value": "Bank statement"},
                        {"name": "Date", "value": "Mon, 14 Jul 2026 10:00:00 -0400"},
                    ],
                    "parts": [{"filename": "bank_statement.pdf"}],
                },
            },
        },
    )

    result = search_communications(
        db_session, rep_id=rep_id, identifier="+15550100001", gmail_service=gmail,
    )

    # Searching by phone alone still surfaces the email-only match.
    assert len(result["emails"]) == 1
    assert result["emails"][0]["from"] == "john.doe@example.com"
    # Attachment references are included.
    assert result["emails"][0]["has_attachment"] is True
    # The query actually sent to Gmail covers every known identifier,
    # not just the phone number the rep typed.
    query = gmail.list_calls[0]["q"]
    assert "john.doe@example.com" in query
    assert "+15550100001" in query
    assert '"John Doe"' in query
    assert '"Doe Roofing"' in query
