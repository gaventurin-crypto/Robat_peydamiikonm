import pytest

from app.domain import (
    CaseStatus,
    INFO_OPTIONS,
    REQUEST_OPTIONS,
    InvalidTransition,
    normalize_case_id,
    toggle_selection,
    validate_transition,
)


def test_request_options_preserve_base_fields_and_allow_user_id_or_custom_type():
    base_options = list(INFO_OPTIONS)
    request_options = list(REQUEST_OPTIONS)
    assert request_options[: len(base_options)] == base_options
    assert request_options == base_options
    selected = toggle_selection([], "کد ملی", tuple(request_options))
    assert selected == ["کد ملی"]


def test_multiple_selection_and_toggle_preserves_other_choices():
    selected = toggle_selection([], "شهر محل زندگی", INFO_OPTIONS)
    selected = toggle_selection(selected, "کد ملی", INFO_OPTIONS)
    assert selected == ["شهر محل زندگی", "کد ملی"]
    assert toggle_selection(selected, "شهر محل زندگی", INFO_OPTIONS) == ["کد ملی"]


def test_status_transitions_are_restricted():
    validate_transition(CaseStatus.NEW, CaseStatus.UNDER_REVIEW)
    with pytest.raises(InvalidTransition):
        validate_transition(CaseStatus.NEW, CaseStatus.PAYMENT_CONFIRMED)


@pytest.mark.parametrize("case_id", ["r-000123", "R-123", "R-۱۲۳", "R-١٢٣"])
def test_case_id_normalizes_farsi_and_arabic_digits(case_id):
    assert normalize_case_id(case_id) == "R-000123"