import pytest

from xfeed import i18n


def test_default_language_is_english(monkeypatch):
    monkeypatch.setattr(i18n, "_current", "en")
    assert i18n.tr("My Feed") == "My Feed"


def test_set_language_switches_translation(monkeypatch):
    monkeypatch.setattr(i18n, "_current", "tr")
    assert i18n.tr("My Feed") == "Akışım"
    assert i18n.current_language() == "tr"


def test_unknown_string_falls_back_to_english(monkeypatch):
    monkeypatch.setattr(i18n, "_current", "tr")
    assert i18n.tr("This string has no translation") == "This string has no translation"


def test_format_arguments_are_applied_after_translation(monkeypatch):
    monkeypatch.setattr(i18n, "_current", "tr")
    assert i18n.tr("Collect {feed}", feed="Sana Özel") == "Sana Özel Topla"
    assert i18n.tr("Collect {feed}", feed="For You") == "For You Topla"


def test_english_format_arguments_are_applied(monkeypatch):
    monkeypatch.setattr(i18n, "_current", "en")
    assert i18n.tr("Collect {feed}", feed="For You") == "Collect For You"


def test_set_language_rejects_unknown_codes():
    with pytest.raises(ValueError):
        i18n.set_language("fr")


def test_language_names_expose_display_labels():
    assert i18n.language_name("en") == "English"
    assert i18n.language_name("tr") == "Türkçe"
