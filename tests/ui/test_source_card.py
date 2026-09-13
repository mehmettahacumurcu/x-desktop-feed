from xfeed.sources import SourceRecord
from xfeed.ui.settings import UiSettingsStore
from xfeed.ui.source_card import SourceCard


def record(*, enabled: bool = True) -> SourceRecord:
    return SourceRecord(7, "openai", "https://x.com/openai", enabled, "2026-07-21")


def test_source_card_has_spacious_separate_rows_and_restores_count(tmp_path, qtbot) -> None:
    settings = UiSettingsStore(tmp_path / "ui.ini")
    settings.set_source_count(7, 20)
    card = SourceCard(record(), settings)
    qtbot.addWidget(card)

    assert card.primary_row is not card.action_row
    assert card.minimumWidth() >= 280
    assert [card.amount_combo.itemData(i) for i in range(card.amount_combo.count())] == [
        5,
        10,
        20,
        30,
    ]
    assert card.amount_combo.currentData() == 20


def test_selection_is_only_a_view_state_and_collection_emits_exact_payload(tmp_path, qtbot) -> None:
    settings = UiSettingsStore(tmp_path / "ui.ini")
    item = record()
    card = SourceCard(item, settings)
    qtbot.addWidget(card)
    selections: list[tuple[int, bool]] = []
    collections: list[tuple[SourceRecord, int]] = []
    card.selection_changed.connect(
        lambda source_id, selected: selections.append((source_id, selected))
    )
    card.collect_requested.connect(lambda source, maximum: collections.append((source, maximum)))

    card.selected_checkbox.setChecked(True)
    card.amount_combo.setCurrentIndex(card.amount_combo.findData(30))
    card.collect_button.click()

    assert selections == [(7, True)]
    assert collections == [(item, 30)]
    assert settings.source_count(7) == 30


def test_disabled_card_is_muted_non_collectable_and_can_be_enabled_or_removed(
    tmp_path, qtbot
) -> None:
    settings = UiSettingsStore(tmp_path / "ui.ini")
    card = SourceCard(record(enabled=False), settings)
    qtbot.addWidget(card)
    enabled: list[tuple[int, bool]] = []
    removed: list[int] = []
    card.enabled_requested.connect(lambda source_id, value: enabled.append((source_id, value)))
    card.remove_requested.connect(removed.append)

    assert card.property("muted") is True
    assert not card.collect_button.isEnabled()
    assert card.enabled_button.text() == "Enable"
    card.enabled_button.click()
    card.remove_button.click()

    assert enabled == [(7, True)]
    assert removed == [7]
