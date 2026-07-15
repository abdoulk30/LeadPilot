"""doc_checklist had no direct test coverage at all before the
2026-07-15 security review (only indirectly exercised through
test_ui.py's HTTP-level tests) — these specifically target
security/pen-test-checklist.md's "File validation checks" section.
"""

from leadpilot.connectors.google_drive import PDF_MIME_TYPE
from leadpilot.queue_builder import MIN_DOC_BYTES, doc_checklist


def _file(name, mime_type=PDF_MIME_TYPE, size_bytes=10_000):
    return {"file_id": "f1", "name": name, "mime_type": mime_type, "size_bytes": size_bytes}


def test_valid_pdf_counts_as_present():
    results = doc_checklist([_file("bank_statement.pdf")])
    row = next(r for r in results if r["label"] == "Bank statements")
    assert row["present"] is True
    assert row["detail"] == "bank_statement.pdf"


def test_missing_document_is_absent_with_no_detail():
    results = doc_checklist([])
    row = next(r for r in results if r["label"] == "Bank statements")
    assert row["present"] is False
    assert row["detail"] is None


def test_zero_byte_file_named_to_match_does_not_count():
    """pen-test-checklist.md: zero-byte file named to match an
    expected document.
    """
    results = doc_checklist([_file("bank_statement.pdf", size_bytes=0)])
    row = next(r for r in results if r["label"] == "Bank statements")
    assert row["present"] is False
    assert "under 5KB" in row["detail"]


def test_under_size_threshold_does_not_count():
    results = doc_checklist([_file("bank_statement.pdf", size_bytes=MIN_DOC_BYTES)])
    row = next(r for r in results if r["label"] == "Bank statements")
    assert row["present"] is False  # exactly at the threshold, not over it


def test_non_pdf_file_renamed_with_pdf_extension_does_not_count():
    """pen-test-checklist.md: the exact scenario the mime_type fix
    closes — a plain-text (or any non-PDF) file whose *name* ends in
    .pdf must not count, since Drive's own mime_type reveals the real
    content type regardless of what the filename claims.
    """
    results = doc_checklist([_file("bank_statement.pdf", mime_type="text/plain")])
    row = next(r for r in results if r["label"] == "Bank statements")
    assert row["present"] is False
    assert "not a PDF" in row["detail"]


def test_real_pdf_with_non_pdf_name_does_not_count():
    """Same check, the other direction: a real PDF that doesn't end in
    .pdf shouldn't silently count either — the name match is still
    part of the contract, not just the content type.
    """
    results = doc_checklist([_file("bank_statement.docx", mime_type=PDF_MIME_TYPE)])
    row = next(r for r in results if r["label"] == "Bank statements")
    assert row["present"] is False


def test_all_three_required_docs_independently_evaluated():
    results = doc_checklist([
        _file("application_form.pdf"),
        _file("prequal_questionnaire.pdf", mime_type="text/plain"),  # renamed, doesn't count
    ])
    by_label = {r["label"]: r for r in results}
    assert by_label["Application"]["present"] is True
    assert by_label["Bank statements"]["present"] is False
    assert by_label["Bank statements"]["detail"] is None  # no candidate at all
    assert by_label["Prequal questionnaire"]["present"] is False
    assert "not a PDF" in by_label["Prequal questionnaire"]["detail"]
