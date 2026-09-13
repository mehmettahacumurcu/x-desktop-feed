from collections.abc import Sequence
from pathlib import Path

from PySide6.QtCore import QSettings

from xfeed.domain import CollectionTargetKind
from xfeed.i18n import LANGUAGES


MY_FEED_COUNTS = (10, 20, 30, 50)
SOURCE_COUNTS = (5, 10, 20, 30)
AUTO_INTERVAL_MINUTES = (0, 5, 10, 30, 60)


def _feed_key(kind: CollectionTargetKind, leaf: str) -> str:
    if not isinstance(kind, CollectionTargetKind) or kind not in (
        CollectionTargetKind.FOR_YOU,
        CollectionTargetKind.FOLLOWING,
    ):
        raise ValueError("kind must be a home feed")
    return f"my_feed/{kind.value}/{leaf}"


class UiSettingsStore:
    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self._settings = QSettings(str(path), QSettings.Format.IniFormat)

    def my_feed_count(self) -> int:
        return self.feed_count(CollectionTargetKind.FOR_YOU)

    def set_my_feed_count(self, value: int) -> None:
        self.set_feed_count(CollectionTargetKind.FOR_YOU, value)

    def auto_interval_minutes(self) -> int:
        return self.feed_auto_interval_minutes(CollectionTargetKind.FOR_YOU)

    def set_auto_interval_minutes(self, value: int) -> None:
        self.set_feed_auto_interval_minutes(CollectionTargetKind.FOR_YOU, value)

    def feed_count(self, kind: CollectionTargetKind) -> int:
        legacy = self._stored_int("my_feed/count") if kind is CollectionTargetKind.FOR_YOU else None
        default = legacy if legacy in MY_FEED_COUNTS else 10
        return self._choice(_feed_key(kind, "count"), MY_FEED_COUNTS, default)

    def set_feed_count(self, kind: CollectionTargetKind, value: int) -> None:
        self._set_choice(_feed_key(kind, "count"), value, MY_FEED_COUNTS)

    def feed_auto_interval_minutes(self, kind: CollectionTargetKind) -> int:
        legacy = (
            self._stored_int("my_feed/auto_interval_minutes")
            if kind is CollectionTargetKind.FOR_YOU
            else None
        )
        default = legacy if legacy in AUTO_INTERVAL_MINUTES else 0
        return self._choice(
            _feed_key(kind, "auto_interval_minutes"),
            AUTO_INTERVAL_MINUTES,
            default,
        )

    def set_feed_auto_interval_minutes(
        self,
        kind: CollectionTargetKind,
        value: int,
    ) -> None:
        self._set_choice(
            _feed_key(kind, "auto_interval_minutes"),
            value,
            AUTO_INTERVAL_MINUTES,
        )

    def collect_all_count(self) -> int:
        return self._choice("sources/collect_all_count", SOURCE_COUNTS, 10)

    def set_collect_all_count(self, value: int) -> None:
        self._set_choice("sources/collect_all_count", value, SOURCE_COUNTS)

    def source_count(self, source_id: int) -> int:
        self._validate_source_id(source_id)
        return self._choice(f"sources/counts/{source_id}", SOURCE_COUNTS, 10)

    def set_source_count(self, source_id: int, value: int) -> None:
        self._validate_source_id(source_id)
        self._set_choice(f"sources/counts/{source_id}", value, SOURCE_COUNTS)

    def window_size(self) -> tuple[int, int]:
        width = self._positive_int("window/width", 1180)
        height = self._positive_int("window/height", 760)
        return width, height

    def set_window_size(self, width: int, height: int) -> None:
        self._validate_geometry(width, height)
        self._set_values(("window/width", width), ("window/height", height))

    def language(self) -> str:
        value = self._settings.value("ui/language")
        return value if value in LANGUAGES else "en"

    def set_language(self, code: str) -> None:
        if code not in LANGUAGES:
            raise ValueError("unknown language code")
        self._settings.setValue("ui/language", code)
        self._settings.sync()

    def source_splitter_sizes(self) -> tuple[int, int]:
        left = self._positive_int("sources/splitter_left", 340)
        right = self._positive_int("sources/splitter_right", 720)
        return left, right

    def set_source_splitter_sizes(self, left: int, right: int) -> None:
        self._validate_geometry(left, right)
        self._set_values(
            ("sources/splitter_left", left),
            ("sources/splitter_right", right),
        )

    def _choice(self, key: str, allowed: Sequence[int], default: int) -> int:
        value = self._stored_int(key)
        return value if value is not None and value in allowed else default

    def _set_choice(self, key: str, value: int, allowed: Sequence[int]) -> None:
        if type(value) is not int or value not in allowed:
            raise ValueError("value is not a supported choice")
        self._set_values((key, value))

    def _positive_int(self, key: str, default: int) -> int:
        value = self._stored_int(key)
        return value if value is not None and value > 0 else default

    def _stored_int(self, key: str) -> int | None:
        raw = self._settings.value(key)
        if isinstance(raw, bool) or raw is None:
            return None
        try:
            return int(raw)
        except (TypeError, ValueError):
            return None

    def _set_values(self, *entries: tuple[str, int]) -> None:
        for key, value in entries:
            self._settings.setValue(key, value)
        self._settings.sync()

    @staticmethod
    def _validate_source_id(source_id: int) -> None:
        if type(source_id) is not int or source_id < 1:
            raise ValueError("source_id must be a positive integer")

    @staticmethod
    def _validate_geometry(first: int, second: int) -> None:
        if type(first) is not int or first < 1 or type(second) is not int or second < 1:
            raise ValueError("geometry values must be positive integers")
