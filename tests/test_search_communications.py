"""Real tests against real local Postgres (for the rep fixture),
plus fake Gmail/Twilio clients — no live API access needed to verify
the search/merge/identifier-routing logic. See
search_communications.py's module docstring for the real, load-bearing
gap in what SMS search can do for name/company identifiers.
"""

import uuid

import pytest

from leadpilot import auth
from leadpilot.connectors.google_sheets import RepNotConnectedError
from leadpilot.tools.base import all_tools
from leadpilot.tools.search_communications import search_communications


class _FakeResponse:
    def __init__(self, data):
        self._data = data

    def execute(self):
        return self._data


class FakeGmailService:
    def __init__(self, list_response=None, get_responses=None):
        self.list_response = list_response or {"messages": []}
        self.get_responses = get_responses or {}
        self.list_calls: list[dict] = []
        self.get_calls: list[dict] = []

    def users(self):
        return self

    def messages(self):
        return self

    def list(self, *, userId, q):
        self.list_calls.append({"userId": userId, "q": q})
        return _FakeResponse(self.list_response)

    def get(self, *, userId, id, format=None, metadataHeaders=None):
        self.get_calls.append({"userId": userId, "id": id})
        return _FakeResponse(self.get_responses[id])


class _FakeTwilioMessage:
    def __init__(self, sid, from_, to, body, date_sent=None, direction="inbound"):
        self.sid = sid
        self.from_ = from_
        self.to = to
        self.body = body
        self.date_sent = date_sent
        self.direction = direction


class FakeTwilioClient:
    def __init__(self, by_from: dict | None = None, by_to: dict | None = None):
        self._by_from = by_from or {}
        self._by_to = by_to or {}
        self.list_calls: list[dict] = []
        self.messages = self

    def list(self, *, from_=None, to=None):
        self.list_calls.append({"from_": from_, "to": to})
        if from_ is not None:
            return self._by_from.get(from_, [])
        if to is not None:
            return self._by_to.get(to, [])
        return []


def _make_rep(session) -> uuid.UUID:
    rep = auth.create_rep(session, email=f"{uuid.uuid4()}-rep@example.com", password="testpassword123")
    return rep.rep_id


def _gmail_with_one_message(message_id="msg1", **overrides) -> FakeGmailService:
    headers = {
        "From": overrides.get("from_", "lead@example.com"),
        "To": overrides.get("to", "rep@leadpilot.com"),
        "Subject": overrides.get("subject", "Re: your application"),
        "Date": overrides.get("date", "Mon, 12 Jul 2026 10:00:00 -0400"),
    }
    return FakeGmailService(
        list_response={"messages": [{"id": message_id, "threadId": "t1"}]},
        get_responses={
            message_id: {
                "id": message_id,
                "snippet": overrides.get("snippet", "Thanks for sending this over..."),
                "payload": {
                    "headers": [{"name": k, "value": v} for k, v in headers.items()],
                    "parts": overrides.get("parts", []),
                },
            }
        },
    )


def test_raises_for_empty_identifier(db_session):
    rep_id = _make_rep(db_session)

    with pytest.raises(ValueError, match="cannot be empty"):
        search_communications(
            db_session, rep_id=rep_id, identifier="  ", gmail_service=FakeGmailService()
        )


def test_email_search_returns_parsed_message(db_session):
    rep_id = _make_rep(db_session)
    fake_gmail = _gmail_with_one_message(subject="Re: bank statements")

    result = search_communications(
        db_session, rep_id=rep_id, identifier="jane@acme.com", gmail_service=fake_gmail
    )

    assert fake_gmail.list_calls == [{"userId": "me", "q": "jane@acme.com"}]
    assert len(result["emails"]) == 1
    assert result["emails"][0]["subject"] == "Re: bank statements"
    assert result["emails"][0]["from"] == "lead@example.com"
    assert result["emails"][0]["has_attachment"] is False
    assert result["texts"] == []


def test_email_search_flags_attachments(db_session):
    rep_id = _make_rep(db_session)
    fake_gmail = _gmail_with_one_message(parts=[{"filename": "bank_statement.pdf"}])

    result = search_communications(
        db_session, rep_id=rep_id, identifier="jane@acme.com", gmail_service=fake_gmail
    )

    assert result["emails"][0]["has_attachment"] is True


def test_name_identifier_does_not_search_sms(db_session):
    """A name/company identifier only searches email — SMS has no
    free-text body search available (documented limitation).
    """
    rep_id = _make_rep(db_session)
    fake_twilio = FakeTwilioClient()

    result = search_communications(
        db_session,
        rep_id=rep_id,
        identifier="Acme Corp",
        gmail_service=FakeGmailService(),
        twilio_client=fake_twilio,
    )

    assert result["texts"] == []
    assert fake_twilio.list_calls == []  # never even called


def test_phone_identifier_searches_both_email_and_sms(db_session):
    rep_id = _make_rep(db_session)
    phone = "+15551234567"
    fake_twilio = FakeTwilioClient(
        by_from={phone: [_FakeTwilioMessage("SM1", phone, "+15550000000", "On my way", direction="inbound")]},
        by_to={phone: [_FakeTwilioMessage("SM2", "+15550000000", phone, "Please send docs", direction="outbound")]},
    )

    result = search_communications(
        db_session,
        rep_id=rep_id,
        identifier=phone,
        gmail_service=FakeGmailService(),
        twilio_client=fake_twilio,
    )

    assert len(result["texts"]) == 2
    sids = {t["message_sid"] for t in result["texts"]}
    assert sids == {"SM1", "SM2"}
    assert {"from_": phone, "to": None} in fake_twilio.list_calls
    assert {"from_": None, "to": phone} in fake_twilio.list_calls


def test_phone_identifier_dedupes_messages_appearing_in_both_directions(db_session):
    """If the same message_sid somehow comes back from both the
    from_= and to= queries, it should only appear once in results.
    """
    rep_id = _make_rep(db_session)
    phone = "+15551234567"
    same_message = _FakeTwilioMessage("SM1", phone, phone, "self-test", direction="inbound")
    fake_twilio = FakeTwilioClient(by_from={phone: [same_message]}, by_to={phone: [same_message]})

    result = search_communications(
        db_session,
        rep_id=rep_id,
        identifier=phone,
        gmail_service=FakeGmailService(),
        twilio_client=fake_twilio,
    )

    assert len(result["texts"]) == 1


def test_raises_if_rep_never_connected_google(db_session):
    rep_id = _make_rep(db_session)  # never goes through Google OAuth

    with pytest.raises(RepNotConnectedError):
        search_communications(db_session, rep_id=rep_id, identifier="jane@acme.com")


def test_accepts_string_rep_id(db_session):
    rep_id = _make_rep(db_session)

    result = search_communications(
        db_session, rep_id=str(rep_id), identifier="jane@acme.com", gmail_service=FakeGmailService()
    )

    assert result["emails"] == []


def test_registered_under_its_own_name():
    registered = all_tools()
    assert "search_communications" in registered
    assert registered["search_communications"].handler is search_communications
