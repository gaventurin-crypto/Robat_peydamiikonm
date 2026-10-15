import pytest

from app.config import Settings
from app.domain import INFO_OPTIONS, LEGACY_INFO_OPTIONS


def set_minimum_environment(monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "test-token")
    monkeypatch.setenv("ADMIN_TELEGRAM_ID", "123456789")
    monkeypatch.delenv("INFO_OPTIONS_ORDER", raising=False)
    monkeypatch.delenv("CASE_RETENTION_HOURS", raising=False)
    monkeypatch.delenv("MAX_UPLOAD_MB", raising=False)


def test_configuration_defaults_match_required_questions(monkeypatch):
    set_minimum_environment(monkeypatch)
    settings = Settings.from_environment()
    assert settings.info_options == INFO_OPTIONS
    assert settings.retention_hours == 24
    assert settings.max_upload_bytes == 10 * 1024 * 1024
    assert settings.have_question == "چه اطلاعاتی در اختیار داری؟ موارد مربوط را انتخاب کن. می‌توانی چند گزینه را هم‌زمان انتخاب کنی."
    assert settings.need_question == "چه اطلاعاتی نیاز داری؟ موارد موردنظر را انتخاب کن."
    assert settings.description_question == "هر توضیحی دربارهٔ درخواستت داری، اینجا بنویس."


def test_options_can_only_be_reordered_as_exact_permutation(monkeypatch):
    set_minimum_environment(monkeypatch)
    reordered = tuple(reversed(INFO_OPTIONS))
    monkeypatch.setenv("INFO_OPTIONS_ORDER", "|".join(reordered))
    assert Settings.from_environment().info_options == reordered
    monkeypatch.setenv("INFO_OPTIONS_ORDER", "آدرس|کد ملی")
    with pytest.raises(ValueError):
        Settings.from_environment()


def test_legacy_option_order_is_preserved_and_new_fields_are_appended(monkeypatch):
    set_minimum_environment(monkeypatch)
    reordered_legacy = tuple(reversed(LEGACY_INFO_OPTIONS))
    monkeypatch.setenv("INFO_OPTIONS_ORDER", "|".join(reordered_legacy))

    options = Settings.from_environment().info_options

    assert options[: len(LEGACY_INFO_OPTIONS)] == reordered_legacy
    assert set(options) == set(INFO_OPTIONS) | {"آدرس", "تاریخ تولد", "شماره حساب یا کارت بانکی"}


@pytest.mark.parametrize(
    ("name", "value"),
    [("ADMIN_TELEGRAM_ID", "owner"), ("CASE_RETENTION_HOURS", "0"), ("MAX_UPLOAD_MB", "50")],
)
def test_invalid_security_settings_are_rejected(monkeypatch, name, value):
    set_minimum_environment(monkeypatch)
    monkeypatch.setenv(name, value)
    with pytest.raises(ValueError):
        Settings.from_environment()