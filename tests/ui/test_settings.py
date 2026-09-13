from pathlib import Path

import pytest
from PySide6.QtCore import QSettings

from xfeed.domain import CollectionTargetKind
from xfeed.ui.settings import UiSettingsStore


def test_ui_settings_defaults_and_round_trip(tmp_path: Path) -> None:
    path = tmp_path / "ui.ini"
    settings = UiSettingsStore(path)

    assert settings.my_feed_count() == 10
    assert settings.auto_interval_minutes() == 0
    assert settings.collect_all_count() == 10
    assert settings.source_count(42) == 10
    assert settings.window_size() == (1180, 760)
    assert settings.source_splitter_sizes() == (340, 720)
    assert settings.language() == "en"

    settings.set_my_feed_count(50)
    settings.set_auto_interval_minutes(10)
    settings.set_collect_all_count(20)
    settings.set_source_count(42, 30)
    settings.set_window_size(1440, 900)
    settings.set_source_splitter_sizes(380, 900)
    settings.set_language("tr")

    reloaded = UiSettingsStore(path)
    assert reloaded.my_feed_count() == 50
    assert reloaded.auto_interval_minutes() == 10
    assert reloaded.collect_all_count() == 20
    assert reloaded.source_count(42) == 30
    assert reloaded.source_count(43) == 10
    assert reloaded.window_size() == (1440, 900)
    assert reloaded.source_splitter_sizes() == (380, 900)
    assert reloaded.language() == "tr"


def test_set_language_rejects_unknown_codes(tmp_path: Path) -> None:
    settings = UiSettingsStore(tmp_path / "ui.ini")

    with pytest.raises(ValueError, match="language"):
        settings.set_language("fr")


def test_feed_settings_are_independent(tmp_path: Path) -> None:
    path = tmp_path / "ui.ini"
    settings = UiSettingsStore(path)

    settings.set_feed_count(CollectionTargetKind.FOR_YOU, 20)
    settings.set_feed_count(CollectionTargetKind.FOLLOWING, 50)
    settings.set_feed_auto_interval_minutes(CollectionTargetKind.FOR_YOU, 5)
    settings.set_feed_auto_interval_minutes(CollectionTargetKind.FOLLOWING, 10)

    reloaded = UiSettingsStore(path)
    assert reloaded.feed_count(CollectionTargetKind.FOR_YOU) == 20
    assert reloaded.feed_count(CollectionTargetKind.FOLLOWING) == 50
    assert reloaded.feed_auto_interval_minutes(CollectionTargetKind.FOR_YOU) == 5
    assert reloaded.feed_auto_interval_minutes(CollectionTargetKind.FOLLOWING) == 10


def test_legacy_for_you_settings_are_read_but_new_writes_are_target_keyed(
    tmp_path: Path,
) -> None:
    path = tmp_path / "ui.ini"
    raw = QSettings(str(path), QSettings.Format.IniFormat)
    raw.setValue("my_feed/count", 50)
    raw.setValue("my_feed/auto_interval_minutes", 10)
    raw.sync()

    settings = UiSettingsStore(path)
    assert settings.feed_count(CollectionTargetKind.FOR_YOU) == 50
    assert settings.feed_auto_interval_minutes(CollectionTargetKind.FOR_YOU) == 10
    assert settings.feed_count(CollectionTargetKind.FOLLOWING) == 10
    assert settings.feed_auto_interval_minutes(CollectionTargetKind.FOLLOWING) == 0

    settings.set_my_feed_count(20)
    settings.set_auto_interval_minutes(5)

    reloaded = UiSettingsStore(path)
    assert reloaded.feed_count(CollectionTargetKind.FOR_YOU) == 20
    assert reloaded.feed_auto_interval_minutes(CollectionTargetKind.FOR_YOU) == 5
    assert raw.value("my_feed/for_you/count", type=int) == 20
    assert raw.value("my_feed/for_you/auto_interval_minutes", type=int) == 5


@pytest.mark.parametrize(
    ("method", "arguments"),
    [
        ("feed_count", ("for_you",)),
        ("feed_count", ("following",)),
        ("set_feed_count", ("for_you", 20)),
        ("set_feed_count", ("following", 20)),
        ("feed_auto_interval_minutes", ("for_you",)),
        ("feed_auto_interval_minutes", ("following",)),
        ("set_feed_auto_interval_minutes", ("for_you", 5)),
        ("set_feed_auto_interval_minutes", ("following", 5)),
    ],
)
def test_feed_settings_reject_raw_string_target_kinds(
    tmp_path: Path,
    method: str,
    arguments: tuple[object, ...],
) -> None:
    settings = UiSettingsStore(tmp_path / "ui.ini")

    with pytest.raises(ValueError, match="home feed"):
        getattr(settings, method)(*arguments)


@pytest.mark.parametrize(
    ("method", "value"),
    [
        ("set_my_feed_count", 5),
        ("set_my_feed_count", True),
        ("set_auto_interval_minutes", 15),
        ("set_collect_all_count", 50),
    ],
)
def test_ui_settings_reject_unsupported_choices(tmp_path: Path, method: str, value: object) -> None:
    settings = UiSettingsStore(tmp_path / "ui.ini")

    with pytest.raises(ValueError, match="supported"):
        getattr(settings, method)(value)


@pytest.mark.parametrize("source_id", [0, -1, True, "1"])
def test_source_count_rejects_invalid_source_ids(tmp_path: Path, source_id: object) -> None:
    settings = UiSettingsStore(tmp_path / "ui.ini")

    with pytest.raises(ValueError, match="source_id"):
        settings.source_count(source_id)  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="source_id"):
        settings.set_source_count(source_id, 10)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("method", "values"),
    [
        ("set_window_size", (0, 760)),
        ("set_window_size", (1180, True)),
        ("set_source_splitter_sizes", (-1, 720)),
        ("set_source_splitter_sizes", (340, "720")),
    ],
)
def test_geometry_setters_reject_non_positive_non_integer_values(
    tmp_path: Path,
    method: str,
    values: tuple[object, object],
) -> None:
    settings = UiSettingsStore(tmp_path / "ui.ini")

    with pytest.raises(ValueError, match="positive integers"):
        getattr(settings, method)(*values)


def test_corrupt_persisted_values_fall_back_without_rewriting_the_file(tmp_path: Path) -> None:
    path = tmp_path / "ui.ini"
    raw = QSettings(str(path), QSettings.Format.IniFormat)
    raw.setValue("my_feed/count", "eleven")
    raw.setValue("my_feed/auto_interval_minutes", 15)
    raw.setValue("sources/collect_all_count", False)
    raw.setValue("sources/counts/7", 99)
    raw.setValue("window/width", -20)
    raw.setValue("window/height", "bad")
    raw.setValue("sources/splitter_left", 0)
    raw.setValue("sources/splitter_right", None)
    raw.sync()

    settings = UiSettingsStore(path)

    assert settings.my_feed_count() == 10
    assert settings.auto_interval_minutes() == 0
    assert settings.collect_all_count() == 10
    assert settings.source_count(7) == 10
    assert settings.window_size() == (1180, 760)
    assert settings.source_splitter_sizes() == (340, 720)
