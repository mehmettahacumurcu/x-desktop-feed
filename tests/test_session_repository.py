import pytest

from xfeed.db import Database
from xfeed.domain import AppSessionState, Availability, CollectionTarget, PostDraft
from xfeed.repositories import CollectionRepository, PostRepository, SourceRepository
from xfeed.session_repository import AppSessionRepository
from xfeed.sources import canonicalize_profile


@pytest.fixture
def database(tmp_path) -> Database:
    database = Database(tmp_path / "feed.sqlite3")
    database.migrate()
    return database


@pytest.fixture
def session_repository(database: Database) -> AppSessionRepository:
    return AppSessionRepository(database)


@pytest.fixture
def collections(database: Database) -> CollectionRepository:
    return CollectionRepository(database)


@pytest.fixture
def source(database: Database):
    return SourceRepository(database).add(canonicalize_profile("openai"))


def post() -> PostDraft:
    return PostDraft(
        canonical_url="https://x.com/openai/status/1",
        x_post_id="1",
        author_handle="openai",
        author_name="OpenAI",
        text="A post",
        published_at=None,
        embed_html="",
        provider_json="{}",
        source_method="fixture",
        availability=Availability.AVAILABLE,
    )


def test_begin_interrupts_old_open_session_before_creating_new(database: Database) -> None:
    repository = AppSessionRepository(database)

    first = repository.begin("0.1.0")
    second = repository.begin("0.1.0")

    interrupted = repository.by_id(first.id)
    assert interrupted is not None
    assert interrupted.state is AppSessionState.INTERRUPTED
    assert interrupted.closed_at is not None
    assert second.state is AppSessionState.OPEN


def test_close_marks_the_open_session_closed(session_repository: AppSessionRepository) -> None:
    active = session_repository.begin("0.1.0")

    closed = session_repository.close(active.id)

    assert closed.state is AppSessionState.CLOSED
    assert closed.closed_at is not None
    assert session_repository.by_id(active.id) == closed


def test_visible_hides_session_with_only_observed_source_runs(
    session_repository: AppSessionRepository,
    collections: CollectionRepository,
    source,
    database: Database,
) -> None:
    empty = session_repository.begin("0.1.0")
    run = collections.start(
        CollectionTarget.for_source(source.id, source.handle, source.profile_url),
        session_id=empty.id,
    )
    saved = PostRepository(database).insert(post())
    collections.observe(run.id, saved.id)

    assert session_repository.visible() == ()


def test_visible_includes_session_after_observing_for_you_post(
    session_repository: AppSessionRepository,
    collections: CollectionRepository,
    database: Database,
) -> None:
    active = session_repository.begin("0.1.0")
    run = collections.start(CollectionTarget.for_you(), session_id=active.id)
    saved = PostRepository(database).insert(post())
    collections.observe(run.id, saved.id)

    assert session_repository.visible() == (active,)
