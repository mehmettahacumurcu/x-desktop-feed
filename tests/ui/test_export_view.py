import csv
import json

import pytest
from PySide6.QtCore import Qt

from xfeed.db import Database
from xfeed.domain import Availability, CollectionTarget, PostDraft
from xfeed.repositories import CollectionRepository, PostRepository
from xfeed.session_repository import AppSessionRepository
from xfeed.ui.export_view import ExportView


@pytest.fixture
def database(tmp_path) -> Database:
    database = Database(tmp_path / "feed.sqlite3")
    database.migrate()
    return database


@pytest.fixture
def sessions(database: Database) -> AppSessionRepository:
    return AppSessionRepository(database)


def save_post(database: Database, handle: str, post_id: str) -> int:
    saved = PostRepository(database).insert(
        PostDraft(
            canonical_url=f"https://x.com/{handle}/status/{post_id}",
            x_post_id=post_id,
            author_handle=handle,
            author_name=handle.title(),
            text=f"text {post_id}",
            published_at="2026-02-01",
            embed_html="",
            provider_json="{}",
            source_method="fixture",
            availability=Availability.AVAILABLE,
        )
    )
    return saved.id


def test_export_view_lists_session_checked_and_exports_csv(
    database: Database, sessions: AppSessionRepository, tmp_path, qtbot
) -> None:
    active = sessions.begin("0.1.0")
    collections = CollectionRepository(database)
    run = collections.start(CollectionTarget.for_you(), session_id=active.id)
    collections.observe(run.id, save_post(database, "nasa", "31"))

    view = ExportView(database, sessions)
    qtbot.addWidget(view)

    assert view._list.count() == 1
    assert view.selected_session_ids() == (active.id,)
    assert view._export_button.isEnabled()
    assert view._empty.isHidden()

    destination = tmp_path / "sessions.csv"
    assert view.export_selected(destination, "csv") == 1
    with destination.open(encoding="utf-8-sig") as handle:
        rows = list(csv.reader(handle))
    assert rows == [
        ["url", "author", "text", "date"],
        ["https://x.com/nasa/status/31", "nasa", "text 31", "2026-02-01"],
    ]


def test_export_view_supports_uncheck_and_json(
    database: Database, sessions: AppSessionRepository, tmp_path, qtbot
) -> None:
    active = sessions.begin("0.1.0")
    collections = CollectionRepository(database)
    run = collections.start(CollectionTarget.following(), session_id=active.id)
    collections.observe(run.id, save_post(database, "esa", "32"))

    view = ExportView(database, sessions)
    qtbot.addWidget(view)

    view._list.item(0).setCheckState(Qt.CheckState.Unchecked)
    assert view.selected_session_ids() == ()
    assert not view._export_button.isEnabled()
    with pytest.raises(ValueError, match="Select at least one session"):
        view.export_selected(tmp_path / "empty.json", "json")

    view._set_all_checked()
    destination = tmp_path / "sessions.json"
    assert view.export_selected(destination, "json") == 1
    payload = json.loads(destination.read_text(encoding="utf-8"))
    assert payload[0]["url"] == "https://x.com/esa/status/32"


def test_export_view_shows_empty_state_without_collections(
    database: Database, sessions: AppSessionRepository, qtbot
) -> None:
    sessions.begin("0.1.0")

    view = ExportView(database, sessions)
    qtbot.addWidget(view)

    assert view._list.count() == 0
    assert not view._empty.isHidden()
    assert view._list.isHidden()
    assert not view._export_button.isEnabled()
    view.apply_language()
    assert view._export_button.text() != ""
