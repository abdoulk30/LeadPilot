"""testing/eval-suite.md Cases 1 and 2 — golden examples (normal input /
edge case). Both genuinely blocked on Step 4's agent loop, not just
untested: they require the *agent's own judgment* — Case 1 needs the
LLM to decide "Rank 1" and choose Text as the next cadence step from a
lead's full context; Case 2 needs the LLM to judge "document
completeness" and choose to draft a completion handoff. There is no
deterministic code path in this codebase for either decision — Step 3's
queue uses an interim heuristic (queue_builder.py, Decision 036 A9)
that is explicitly a stand-in until Step 4 exists, not an
implementation of these two cases.

Skipped rather than omitted, so `pytest tests/eval_suite/` still
reports all 11 cases and their real status in one run.
"""

import pytest


@pytest.mark.skip(reason="Case 1 requires the agent's own prioritization/drafting judgment — Step 4 not built yet")
def test_case_1_golden_example_normal_input():
    ...


@pytest.mark.skip(
    reason="Case 2 requires the agent's own document-completeness judgment to draft a handoff — Step 4 not built yet"
)
def test_case_2_golden_example_edge_case():
    ...
