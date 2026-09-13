import json

from PySide6.QtCore import QUrl
from PySide6.QtWebEngineCore import QWebEnginePage, QWebEngineProfile
from PySide6.QtWebEngineWidgets import QWebEngineView
from PySide6.QtWidgets import QWidget

from xfeed.public_collector import (
    AuthenticatedProfileCollector,
    ProfileCollector,
    PublicProfileCollector,
    RestrictedPublicPage,
    EXTRACTION_SCRIPT,
)
from xfeed.sources import SourceProfile


class SignalDouble:
    def __init__(self) -> None:
        self.callbacks = []

    def connect(self, callback) -> None:
        self.callbacks.append(callback)

    def disconnect(self, callback) -> None:
        if callback in self.callbacks:
            self.callbacks.remove(callback)


class PageDouble:
    def __init__(self) -> None:
        self.loadFinished = SignalDouble()
        self.urlChanged = SignalDouble()
        self.navigationDenied = SignalDouble()

    def load(self, _url) -> None:
        pass

    def setUrl(self, _url) -> None:
        pass

    def runJavaScript(self, _script, _callback) -> None:
        pass

    def stop(self) -> None:
        pass

    def deleteLater(self) -> None:
        pass


class OfflineRestrictedPage(RestrictedPublicPage):
    def __init__(self, profile, canonical_profile_url, parent=None) -> None:
        super().__init__(profile, canonical_profile_url, parent)
        self.offline_loads: list[QUrl] = []

    def load(self, url: QUrl) -> None:
        self.offline_loads.append(QUrl(url))


class TimerDouble:
    def __init__(self) -> None:
        self.timeout = SignalDouble()

    def setSingleShot(self, _single_shot) -> None:
        pass

    def start(self, _interval) -> None:
        pass

    def stop(self) -> None:
        pass


def test_injected_collector_widget_is_visible_host_and_signals_are_qt_signals(qtbot):
    widget = QWidget()
    qtbot.addWidget(widget)
    collector = PublicProfileCollector(
        page_factory=lambda _url: PageDouble(),
        widget=widget,
        timer=TimerDouble(),
        clock=lambda: 0.0,
    )

    assert collector.widget is widget
    assert isinstance(collector, ProfileCollector)
    assert hasattr(collector.progress, "connect")
    assert hasattr(collector.finished, "connect")


def test_default_collector_factory_creates_restricted_off_record_pages_without_loading(qtbot):
    collector = PublicProfileCollector(settle_ms=10_000)
    assert isinstance(collector.widget, QWebEngineView)
    qtbot.addWidget(collector.widget)

    first_page = collector._create_public_page(QUrl("https://x.com/openai"))
    first_profile = first_page.profile()

    assert isinstance(first_page, RestrictedPublicPage)
    assert first_profile.isOffTheRecord()
    denied = []
    first_page.navigationDenied.connect(denied.append)
    assert (
        first_page.acceptNavigationRequest(
            QUrl("https://x.com/i/flow/login"),
            QWebEnginePage.NavigationType.NavigationTypeLinkClicked,
            True,
        )
        is False
    )
    assert denied == [QUrl("https://x.com/i/flow/login")]
    assert first_page.url().isEmpty()

    first_page.deleteLater()
    first_profile.deleteLater()
    second_page = collector._create_public_page(QUrl("https://x.com/openai"))
    second_profile = second_page.profile()

    assert second_page is not first_page
    assert second_profile is not first_profile
    assert second_profile.isOffTheRecord()
    assert second_page.url().isEmpty()
    second_page.deleteLater()
    second_profile.deleteLater()


def test_authenticated_collector_reuses_persistent_profile_with_fresh_page_each_run(
    tmp_path, qtbot
):
    profile = QWebEngineProfile("authenticated-collector-test")
    profile.setPersistentStoragePath(str(tmp_path / "storage"))
    profile.setCachePath(str(tmp_path / "cache"))
    view = QWebEngineView()
    offline_pages = []

    def page_factory(url: QUrl):
        page = OfflineRestrictedPage(profile, url.toString(), view)
        offline_pages.append(page)
        view.setPage(page)
        return page

    collector = AuthenticatedProfileCollector(
        profile,
        settle_ms=10_000,
        page_factory=page_factory,
        widget=view,
    )
    assert isinstance(collector.widget, QWebEngineView)
    qtbot.addWidget(collector.widget)

    collector.start(SourceProfile("openai", "https://x.com/openai"))
    first_page = collector.widget.page()

    assert isinstance(first_page, RestrictedPublicPage)
    assert first_page.profile() is profile
    assert first_page.offline_loads == [QUrl("https://x.com/openai")]

    collector.cancel()
    collector.start(SourceProfile("openai", "https://x.com/openai"))
    second_page = collector.widget.page()

    assert second_page is not first_page
    assert second_page.profile() is profile
    assert second_page.offline_loads == [QUrl("https://x.com/openai")]
    assert offline_pages == [first_page, second_page]

    collector.cancel()
    profile.deleteLater()


def execute_extraction(qtbot, article_content: str) -> dict[str, object]:
    profile = QWebEngineProfile()
    page = QWebEnginePage(profile)
    loaded: list[bool] = []
    page.loadFinished.connect(loaded.append)
    page.setHtml(
        "<article data-testid='tweet'>"
        "<a href='https://x.com/openai'>@openai</a>"
        "<a href='https://x.com/openai/status/123'><time>now</time></a>"
        f"{article_content}</article>",
        QUrl("https://x.com/openai"),
    )
    qtbot.waitUntil(lambda: bool(loaded), timeout=5_000)
    results: list[object] = []
    page.runJavaScript(EXTRACTION_SCRIPT, results.append)
    qtbot.waitUntil(lambda: bool(results), timeout=5_000)
    result = json.loads(str(results[0]))
    assert isinstance(result, dict)
    page.deleteLater()
    profile.deleteLater()
    return result


def first_observation(result: dict[str, object]) -> dict[str, object]:
    observations = result["observations"]
    assert isinstance(observations, list)
    observation = observations[0]
    assert isinstance(observation, dict)
    return observation


def test_localized_social_context_is_excluded_structurally(qtbot):
    result = execute_extraction(
        qtbot,
        "<div data-testid='socialContext'>Compartido por alguien</div>",
    )

    observation = first_observation(result)
    assert observation["is_pinned"] is True
    assert observation["is_repost"] is True


def test_reply_phrase_inside_tweet_text_is_not_classified_as_reply(qtbot):
    result = execute_extraction(
        qtbot,
        "<div data-testid='tweetText'>Replying to is merely quoted prose.</div>",
    )

    assert first_observation(result)["is_reply"] is False


def test_profile_link_outside_tweet_and_quote_content_is_structural_reply(qtbot):
    result = execute_extraction(
        qtbot,
        "<a href='https://x.com/someone_else'>@someone_else</a>"
        "<div data-testid='tweetText'>A localized reply body</div>",
    )

    assert first_observation(result)["is_reply"] is True


def test_quote_profile_link_does_not_make_outer_post_a_reply(qtbot):
    result = execute_extraction(
        qtbot,
        "<div data-testid='card.wrapper'>"
        "<a href='https://x.com/quoted'>@quoted</a>"
        "<a href='https://x.com/quoted/status/456'>quoted post</a>"
        "</div>",
    )

    observation = first_observation(result)
    assert observation["is_reply"] is False
    assert observation["is_quote"] is True


def test_self_reply_profile_link_after_permalink_is_classified_as_reply(qtbot):
    result = execute_extraction(
        qtbot,
        "<a href='https://x.com/openai'>@openai reply context</a>"
        "<div data-testid='tweetText'>Self reply body</div>",
    )

    assert first_observation(result)["is_reply"] is True


def test_profile_mention_inside_tweet_text_is_not_reply_context(qtbot):
    result = execute_extraction(
        qtbot,
        "<div data-testid='tweetText'>"
        "A standalone mention of <a href='https://x.com/someone_else'>@someone_else</a>"
        "</div>",
    )

    assert first_observation(result)["is_reply"] is False
