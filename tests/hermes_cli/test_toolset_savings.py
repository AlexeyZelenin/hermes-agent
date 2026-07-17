"""Automated corpus + context-savings tests for the toolset selector.

Complements the unit tests in ``test_toolset_selector.py`` (per-rule golden
table, invariants, aux, fallback) with two things the acceptance criteria of
``t_44c4a030`` call for:

1. A **representative task corpus** exercising every tool group plus negative
   (minimal / denylisted) and worst-case tasks — asserting mandatory base tools,
   relevant selection, and correct exclusion.
2. A **measured savings** check: resolve each narrowed selection to its concrete
   tool schemas and assert the byte/token reduction against the full ceiling,
   for both the typical case and the worst case.

The published report (``docs/reports/toolset-selection-savings.md``) is
regenerated from the same corpus via ``scripts/measure_toolset_savings.py``, so
these thresholds and that report cannot drift apart. Thresholds are deliberately
loose lower bounds: they prove the mechanism saves real tokens without turning
into change-detectors that break on benign rule tuning.
"""
from __future__ import annotations

import pytest

from tests.hermes_cli import toolset_corpus as corpus

# The measurement boots the whole tool registry (model_tools import); do it once.
pytestmark = pytest.mark.filterwarnings("ignore")


@pytest.fixture(scope="module")
def report():
    return corpus.measure()


@pytest.fixture(scope="module")
def rows(report):
    return {r.case.name: r for r in report.rows}


# --- mandatory base tools ---------------------------------------------------

def test_base_present_in_every_selection(rows):
    """Invariant: base ⊆ every narrowed selection, across the whole corpus."""
    for name, row in rows.items():
        assert set(corpus.BASE) <= set(row.selected), f"{name} dropped a base toolset"


# --- relevant selection -----------------------------------------------------

def test_positive_cases_select_expected_group(rows):
    for case in corpus.CORPUS:
        for want in case.expect:
            assert want in rows[case.name].selected, (
                f"{case.name}: expected {want} to be selected, got {rows[case.name].selected}"
            )


def test_denylisted_capabilities_excluded(rows):
    for case in corpus.CORPUS:
        for banned in case.forbid:
            assert banned not in rows[case.name].selected, (
                f"{case.name}: {banned} must be excluded (denylist/negative)"
            )


def test_minimal_tasks_narrow_to_base_only(rows):
    for case in corpus.CORPUS:
        if case.minimal:
            assert sorted(rows[case.name].selected) == sorted(corpus.BASE), (
                f"{case.name}: a no-signal task must narrow to base only"
            )


def test_every_selection_is_deduped_and_subset_of_ceiling(rows):
    ceiling = set(corpus.FULL_CEILING)
    for name, row in rows.items():
        assert row.selected == sorted(set(row.selected)), f"{name} not deduped/sorted"
        assert set(row.selected) <= ceiling, f"{name} widened beyond ceiling"


# --- measured savings -------------------------------------------------------

def test_full_ceiling_is_in_the_expected_ballpark(report):
    """The narrowable ceiling + kanban floor is the ~17k-token surface we cut."""
    assert report.worker_full_tokens > 15_000
    assert report.ceiling_tokens > 12_000


def test_minimal_tasks_save_the_most(rows):
    """A base-only worker sheds the great majority of the ceiling's schema."""
    for case in corpus.CORPUS:
        if case.minimal:
            assert rows[case.name].saved_pct >= 60.0


def test_typical_savings_are_substantial(report):
    """Median saving across non-worst tasks clears a conservative floor."""
    assert report.typical_saved_pct >= 40.0


def test_worst_case_still_saves_but_least(report, rows):
    """Even a task touching almost everything saves *something* — and it is the
    corpus minimum (the honest worst case), strictly below the full ceiling."""
    worst = rows["worst-case"]
    assert 0.0 < worst.saved_pct
    assert worst.tokens < report.ceiling_tokens
    assert worst.saved_pct == report.worst_saved_pct


def test_no_selection_ever_exceeds_the_ceiling_cost(report, rows):
    for row in rows.values():
        assert row.tokens <= report.ceiling_tokens
        assert row.worker_tokens <= report.worker_full_tokens
