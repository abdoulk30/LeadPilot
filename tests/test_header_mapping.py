"""Header-tolerance tests for GoogleSheetsConnector's row mapping —
added after Marc's first real intake sheet (uppercase headers, split
FIRST/LAST NAME, numbered PHONE columns) ingested 600+ leads with
every canonical field empty.
"""

from leadpilot.connectors.google_sheets import _map_row_fields, _resolve_header


def test_exact_canonical_headers_still_map():
    fields = _map_row_fields({"Name": "Jo", "Phone": "1", "Email": "a@b.c", "Company": "Co", "Status": "New"})
    assert fields == {"name": "Jo", "phone": "1", "email": "a@b.c", "company": "Co", "status": "New"}


def test_marcs_real_sheet_headers_map():
    fields = _map_row_fields({
        "EMAIL": "lead@example.com",
        "PHONE": "+15551230000",
        "LAST NAME": "Whitfield",
        "FIRST NAME": "Dana",
        "TIME STAMP": "2026-07-01",
        "What will you use the funds for?": "Equipment",
    })
    assert fields["name"] == "Dana Whitfield"
    assert fields["phone"] == "+15551230000"
    assert fields["email"] == "lead@example.com"


def test_numbered_phone_and_email_columns_use_first_nonempty():
    fields = _map_row_fields({
        "Phone 1": "", "Phone 2": "+15551112222", "Email #1": "first@x.com", "Email #2": "second@x.com",
    })
    assert fields["phone"] == "+15551112222"
    assert fields["email"] == "first@x.com"


def test_synonyms_map():
    fields = _map_row_fields({"Full Name": "A B", "Mobile": "+15550001111", "Business Name": "Acme", "Lead Status": "Hot"})
    assert fields == {"name": "A B", "phone": "+15550001111", "email": None, "company": "Acme", "status": "Hot"}


def test_resolve_header_finds_real_column_for_writes():
    header = ["EMAIL", "PHONE", "LAST NAME", "FIRST NAME", "Lead Status"]
    assert _resolve_header(header, "status") == "Lead Status"
    assert _resolve_header(header, "phone") == "PHONE"
    assert _resolve_header(header, "company") is None
