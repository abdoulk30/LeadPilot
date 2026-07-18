"""On-demand single-lead email drafting (leadpilot.email_drafting) —
real Postgres, scripted fake Anthropic client (same convention as
test_agent_loop.py's FakeAnthropicClient — never a real model call in
CI, per testing/ci-strategy.md).
"""

import json
import uuid
from types import SimpleNamespace

import pytest

from leadpilot import auth, email_drafting
from leadpilot.models.contact_history import Channel, ContactHistory, Stage, Tool
from leadpilot.models.leads import Lead


def _block(**kw):
    return SimpleNamespace(**kw)


def text_response(text):
    return SimpleNamespace(content=[_block(type="text", text=text)])


class FakeAnthropicClient:
    def __init__(self, response_text):
        self._response_text = response_text
        self.requests = []
        self.messages = self

    def create(self, **kwargs):
        self.requests.append(kwargs)
        return text_response(self._response_text)


@pytest.fixture()
def rep(db_session):
    return auth.create_rep(db_session, email=f"{uuid.uuid4()}-draft-test@example.com", password="testpassword123")


def _make_lead(session, *, email="lead@example.com", phone="555-1111", name="Dana Whitfield", company="Acme"):
    lead = Lead(display_name=name, primary_email=email, primary_phone=phone, company=company)
    session.add(lead)
    session.flush()
    return lead


def test_drafts_subject_and_body_from_fake_client(db_session, rep):
    lead = _make_lead(db_session)
    fake = FakeAnthropicClient(json.dumps({"subject": "Following up", "body": "Hi Dana, ..."}))

    result = email_drafting.draft_email_for_lead(db_session, lead.lead_id, anthropic_client=fake)

    assert result == {"subject": "Following up", "body": "Hi Dana, ..."}
    assert len(fake.requests) == 1


def test_context_includes_lead_fields_and_contact_history(db_session, rep):
    lead = _make_lead(db_session)
    db_session.add(ContactHistory(
        lead_id=lead.lead_id, rep_id=rep.rep_id, channel=Channel.CALL, tool=Tool.INITIATE_LEAD_CALL,
        stage=Stage.EXECUTED, content_ref=None,
    ))
    db_session.flush()
    fake = FakeAnthropicClient(json.dumps({"subject": "s", "body": "b"}))

    email_drafting.draft_email_for_lead(db_session, lead.lead_id, anthropic_client=fake)

    prompt = fake.requests[0]["messages"][0]["content"]
    assert "Dana Whitfield" in prompt
    assert "Acme" in prompt
    assert "INITIATE_LEAD_CALL" in prompt.upper() or "initiate_lead_call" in prompt.lower()


def test_rep_name_is_included_so_the_model_can_sign_the_draft(db_session, rep):
    rep.display_name = "Marc Delsoin"
    db_session.flush()
    lead = _make_lead(db_session)
    fake = FakeAnthropicClient(json.dumps({"subject": "s", "body": "b"}))

    email_drafting.draft_email_for_lead(db_session, lead.lead_id, rep=rep, anthropic_client=fake)

    prompt = fake.requests[0]["messages"][0]["content"]
    assert "Marc Delsoin" in prompt


def test_no_contact_history_still_drafts(db_session, rep):
    lead = _make_lead(db_session)
    fake = FakeAnthropicClient(json.dumps({"subject": "s", "body": "b"}))

    result = email_drafting.draft_email_for_lead(db_session, lead.lead_id, anthropic_client=fake)

    assert result == {"subject": "s", "body": "b"}
    prompt = fake.requests[0]["messages"][0]["content"]
    assert "first outreach" in prompt.lower()


def test_missing_lead_raises_without_calling_the_model(db_session, rep):
    fake = FakeAnthropicClient(json.dumps({"subject": "s", "body": "b"}))
    with pytest.raises(email_drafting.LeadNotFoundError):
        email_drafting.draft_email_for_lead(db_session, uuid.uuid4(), anthropic_client=fake)
    assert fake.requests == []


def test_lead_with_no_email_raises_without_calling_the_model(db_session, rep):
    lead = _make_lead(db_session, email=None)
    fake = FakeAnthropicClient(json.dumps({"subject": "s", "body": "b"}))
    with pytest.raises(ValueError, match="no email"):
        email_drafting.draft_email_for_lead(db_session, lead.lead_id, anthropic_client=fake)
    assert fake.requests == []


def test_markdown_fenced_response_is_still_parsed(db_session, rep):
    lead = _make_lead(db_session)
    fenced = "```json\n" + json.dumps({"subject": "s", "body": "b"}) + "\n```"
    fake = FakeAnthropicClient(fenced)

    result = email_drafting.draft_email_for_lead(db_session, lead.lead_id, anthropic_client=fake)
    assert result == {"subject": "s", "body": "b"}


def test_prose_wrapped_response_falls_back_to_first_json_object(db_session, rep):
    lead = _make_lead(db_session)
    wrapped = "Sure, here's a draft:\n" + json.dumps({"subject": "s", "body": "b"})
    fake = FakeAnthropicClient(wrapped)

    result = email_drafting.draft_email_for_lead(db_session, lead.lead_id, anthropic_client=fake)
    assert result == {"subject": "s", "body": "b"}


def test_malformed_response_raises_value_error(db_session, rep):
    lead = _make_lead(db_session)
    fake = FakeAnthropicClient("not json at all")

    with pytest.raises(ValueError):
        email_drafting.draft_email_for_lead(db_session, lead.lead_id, anthropic_client=fake)


def test_response_missing_a_required_key_raises(db_session, rep):
    lead = _make_lead(db_session)
    fake = FakeAnthropicClient(json.dumps({"subject": "s"}))

    with pytest.raises(ValueError):
        email_drafting.draft_email_for_lead(db_session, lead.lead_id, anthropic_client=fake)
