from pathlib import Path
from contextlib import closing
import sqlite3

import pytest

import xfeed.app as app


@pytest.fixture
def data_paths(tmp_path, monkeypatch):
    current = tmp_path / "XDesktopFeed"
    legacy = tmp_path / "Internship" / "XDesktopFeed"

    def user_path(appname, appauthor=None):
        assert appname == "XDesktopFeed"
        return current if appauthor is False else legacy

    monkeypatch.setattr(app, "user_data_path", user_path)
    return current, legacy


def prepare(path: Path) -> None:
    operation = getattr(app, "prepare_data_directory", None)
    assert callable(operation), "Startup must prepare the new data directory safely"
    operation(path)


def test_default_paths_drop_author_directory(data_paths):
    current, _legacy = data_paths
    assert app.default_database_path() == current / "feed.sqlite3"
    assert app.default_session_data_path() == current / "web-profile"
    assert app.default_media_path() == current / "media"
    assert app.default_ui_settings_path() == current / "ui.ini"
    assert app.default_bridge_data_path() == current / "web-profile" / "opera-bridge"


def test_first_install_creates_only_new_directory(data_paths):
    current, legacy = data_paths
    prepare(current)
    assert current.is_dir()
    assert not legacy.exists()


def test_upgrade_preserves_database_media_settings_and_pairing(data_paths):
    current, legacy = data_paths
    legacy.mkdir(parents=True)
    with closing(sqlite3.connect(legacy / "feed.sqlite3")) as connection:
        connection.execute("CREATE TABLE posts(text TEXT)")
        connection.execute("INSERT INTO posts VALUES ('retained post')")
        connection.commit()
    for relative, content in {
        "media/123/photo-0.jpg": b"photo data",
        "ui.ini": b"language=tr",
        "web-profile/opera-bridge/state.json": b'{"test":"pairing"}',
        "web-profile/reader-storage/state": b"reader state",
        "feed.sqlite3-wal": b"uncheckpointed bytes",
    }.items():
        path = legacy / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
    original = {p.relative_to(legacy): p.read_bytes() for p in legacy.rglob("*") if p.is_file()}
    prepare(current)
    assert {
        p.relative_to(legacy): p.read_bytes() for p in legacy.rglob("*") if p.is_file()
    } == original
    assert {
        p.relative_to(current): p.read_bytes() for p in current.rglob("*") if p.is_file()
    } == original
    prepare(current)
    assert (current / "feed.sqlite3").read_bytes() == original[Path("feed.sqlite3")]


def test_existing_destination_wins_without_merging_or_overwriting(data_paths):
    current, legacy = data_paths
    current.mkdir()
    legacy.mkdir(parents=True)
    (current / "ui.ini").write_text("new")
    (legacy / "ui.ini").write_text("old")
    prepare(current)
    assert (current / "ui.ini").read_text() == "new"
    assert (legacy / "ui.ini").read_text() == "old"


def test_failed_rename_preserves_old_data_and_does_not_create_empty_library(
    data_paths, monkeypatch
):
    current, legacy = data_paths
    legacy.mkdir(parents=True)
    (legacy / "feed.sqlite3").write_bytes(b"existing database")

    def deny(_self, _target):
        raise PermissionError("directory is in use")

    monkeypatch.setattr(Path, "rename", deny)
    with pytest.raises(OSError, match="directory is in use"):
        prepare(current)
    assert not current.exists()
    assert (legacy / "feed.sqlite3").read_bytes() == b"existing database"


def test_failed_copy_leaves_source_intact_without_publishing_partial_library(
    data_paths, monkeypatch
):
    import shutil

    current, legacy = data_paths
    legacy.mkdir(parents=True)
    (legacy / "ui.ini").write_text("keep")

    def fail_copy(source, destination):
        destination.mkdir()
        (destination / "partial").write_text("incomplete")
        raise OSError("disk full")

    monkeypatch.setattr(shutil, "copytree", fail_copy)
    with pytest.raises(OSError, match="disk full"):
        prepare(current)
    assert not current.exists()
    assert (legacy / "ui.ini").read_text() == "keep"
    assert not list(current.parent.glob(".xfeed-migration-*"))


def test_custom_database_location_does_not_move_user_data(data_paths, tmp_path):
    current, legacy = data_paths
    legacy.mkdir(parents=True)
    (legacy / "ui.ini").write_text("keep")
    prepare(tmp_path / "custom")
    assert (legacy / "ui.ini").read_text() == "keep"
    assert not current.exists()


def test_platform_with_identical_old_and_new_paths_needs_no_migration(tmp_path, monkeypatch):
    monkeypatch.setattr(app, "user_data_path", lambda *args, **kwargs: tmp_path)
    (tmp_path / "ui.ini").write_text("keep")
    prepare(tmp_path)
    assert (tmp_path / "ui.ini").read_text() == "keep"
