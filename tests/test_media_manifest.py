import pytest

from xfeed.domain import Availability, SavedPost
from xfeed.media import MediaOrigin
from xfeed.media_manifest import MediaManifestService


class RecordingMediaRepository:
    def __init__(self) -> None:
        self.calls: list[tuple[int, str, MediaOrigin, tuple[object, ...]]] = []

    def record_manifest(
        self,
        post_id: int,
        canonical_url: str,
        origin: MediaOrigin,
        photos: tuple[object, ...],
    ) -> tuple[object, ...]:
        self.calls.append((post_id, canonical_url, origin, photos))
        return ()


class FailingMediaRepository:
    def record_manifest(
        self,
        _post_id: int,
        _canonical_url: str,
        _origin: MediaOrigin,
        _photos: tuple[object, ...],
    ) -> tuple[object, ...]:
        raise RuntimeError("media database unavailable")


def post() -> SavedPost:
    return SavedPost(
        canonical_url="https://x.com/example/status/123",
        x_post_id="123",
        author_handle="example",
        author_name="Example",
        text="A post",
        published_at=None,
        embed_html="<blockquote></blockquote>",
        provider_json="{}",
        source_method="oembed",
        availability=Availability.AVAILABLE,
        id=101,
    )


def test_record_collection_manifest_records_an_empty_terminal_manifest() -> None:
    repository = RecordingMediaRepository()
    service = MediaManifestService(repository)  # type: ignore[arg-type]
    recorded: list[tuple[object, ...]] = []
    service.manifest_recorded.connect(recorded.append)
    saved = post()

    assert service.record_collection_manifest(saved, ()) == ()
    assert repository.calls == [(saved.id, saved.canonical_url, MediaOrigin.COLLECTION, ())]
    assert recorded == [()]


def test_record_collection_manifest_emits_a_diagnostic_when_repository_fails() -> None:
    service = MediaManifestService(FailingMediaRepository())  # type: ignore[arg-type]
    diagnostics: list[tuple[int, str]] = []
    service.manifest_failed.connect(lambda post_id, message: diagnostics.append((post_id, message)))

    with pytest.raises(RuntimeError, match="media database unavailable"):
        service.record_collection_manifest(post(), ())
    assert diagnostics == [(101, "media database unavailable")]


def test_record_manual_manifest_uses_manual_origin() -> None:
    repository = RecordingMediaRepository()
    service = MediaManifestService(repository)  # type: ignore[arg-type]
    saved = post()

    assert service.record_manual_manifest(saved, ()) == ()
    assert repository.calls == [(saved.id, saved.canonical_url, MediaOrigin.MANUAL, ())]
