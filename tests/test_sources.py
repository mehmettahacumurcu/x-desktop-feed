import sqlite3

import pytest

from xfeed.db import Database
from xfeed.repositories import SourceRepository
from xfeed.sources import TimelineHtmlBuilder, canonicalize_profile


def source_repository(tmp_path) -> tuple[Database, SourceRepository]:
    database = Database(tmp_path / "feed.sqlite3")
    database.migrate()
    return database, SourceRepository(database)


def test_profile_handle_becomes_supported_timeline():
    profile = canonicalize_profile("@OpenAI")

    assert profile.handle == "openai"
    assert profile.profile_url == "https://x.com/openai"
    html = TimelineHtmlBuilder().render(profile)
    assert 'href="https://x.com/openai"' in html
    assert "platform.x.com/widgets.js" in html
    assert 'data-tweet-limit="20"' in html


@pytest.mark.parametrize(
    "value",
    [
        "https://x.com/OpenAI",
        "https://WWW.X.COM/OpenAI/",
        "https://twitter.com/OpenAI",
        "https://WWW.TWITTER.COM/OpenAI/",
    ],
)
def test_supported_profile_urls_are_canonicalized_case_insensitively(value):
    assert canonicalize_profile(value) == canonicalize_profile("@openai")


@pytest.mark.parametrize(
    "value",
    [
        "",
        "@",
        "has-a-dash",
        "sixteencharacters",
        "https://example.com/openai",
    ],
)
def test_invalid_profiles_are_rejected(value):
    with pytest.raises(ValueError):
        canonicalize_profile(value)


def test_duplicate_handles_are_rejected_case_insensitively(tmp_path):
    _, sources = source_repository(tmp_path)
    sources.add(canonicalize_profile("OpenAI"))

    with pytest.raises(sqlite3.IntegrityError):
        sources.add(canonicalize_profile("@OPENAI"))


def test_enabled_sources_are_listed_alphabetically_and_can_be_toggled(tmp_path):
    _, sources = source_repository(tmp_path)
    zebra = sources.add(canonicalize_profile("zebra"))
    alpha = sources.add(canonicalize_profile("Alpha"))
    middle = sources.add(canonicalize_profile("middle"))

    sources.set_enabled(middle.id, False)

    assert [source.id for source in sources.list_enabled()] == [alpha.id, zebra.id]
    sources.set_enabled(middle.id, True)
    assert [source.handle for source in sources.list_enabled()] == ["alpha", "middle", "zebra"]


def test_all_sources_include_disabled_records_in_alphabetical_order(tmp_path):
    _, sources = source_repository(tmp_path)
    zebra = sources.add(canonicalize_profile("zebra"))
    alpha = sources.add(canonicalize_profile("Alpha"))
    sources.set_enabled(alpha.id, False)

    records = sources.list_all()

    assert [(source.id, source.enabled) for source in records] == [
        (alpha.id, False),
        (zebra.id, True),
    ]


def test_source_can_be_deleted_after_refresh_history_exists(tmp_path):
    database, sources = source_repository(tmp_path)
    source = sources.add(canonicalize_profile("openai"))
    sources.record_refresh(source.id, "requested")

    sources.remove(source.id)

    assert sources.list_enabled() == []
    with database.connection() as connection:
        event_count = connection.execute("SELECT COUNT(*) FROM refresh_events").fetchone()[0]
    assert event_count == 0


def test_recording_refresh_updates_source_timestamp_and_history(tmp_path):
    database, sources = source_repository(tmp_path)
    source = sources.add(canonicalize_profile("openai"))

    sources.record_refresh(source.id, "requested", "user refresh")

    refreshed = sources.list_enabled()[0]
    assert refreshed.last_refresh_at is not None
    with database.connection() as connection:
        event = connection.execute(
            "SELECT started_at, finished_at, result, diagnostic FROM refresh_events"
        ).fetchone()
    assert event is not None
    assert event["started_at"] is not None
    assert event["finished_at"] == event["started_at"]
    assert event["result"] == "requested"
    assert event["diagnostic"] == "user refresh"
