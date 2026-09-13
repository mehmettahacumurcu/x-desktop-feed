import json
import math
import time
import weakref
from collections.abc import Callable
from dataclasses import dataclass, replace
from typing import Any, Protocol, cast, runtime_checkable

from PySide6.QtCore import QObject, QTimer, QUrl, Signal
from PySide6.QtWebEngineCore import QWebEnginePage, QWebEngineProfile
from PySide6.QtWebEngineWidgets import QWebEngineView
from PySide6.QtWidgets import QWidget

from xfeed.collection import (
    CandidateObservation,
    DiscoveryReason,
    DiscoveryResult,
    qualifying_candidates,
)
from xfeed.sources import SourceProfile, canonicalize_profile


_EXTRACTION_TEMPLATE = r"""
(() => {
    const statusPattern = /^\/([^/]+)\/status\/(\d+)/;
    const profilePattern = /^\/([A-Za-z0-9_]{1,15})\/?$/;
    const articles = Array.from(document.querySelectorAll('article[data-testid="tweet"]'));
    const observations = [];
    for (const article of articles) {
        const timeElement = article.querySelector('time');
        const permalink = timeElement && timeElement.closest('a[href*="/status/"]');
        if (!permalink) continue;
        const parsed = new URL(permalink.href, window.location.origin);
        const outerMatch = parsed.pathname.match(statusPattern);
        if (!outerMatch) continue;

        const statusIds = new Set();
        for (const anchor of article.querySelectorAll('a[href*="/status/"]')) {
            const match = new URL(anchor.href, window.location.origin).pathname.match(statusPattern);
            if (match) statusIds.add(match[2]);
        }
        const hasSocialContext = Boolean(
            article.querySelector('[data-testid="socialContext"]')
        );
        const isReply = Array.from(article.querySelectorAll('a[href]')).some((anchor) => {
            if (anchor.closest('[data-testid="tweetText"], [data-testid="card.wrapper"]')) {
                return false;
            }
            const match = new URL(anchor.href, window.location.origin).pathname.match(profilePattern);
            const afterPermalink = Boolean(
                permalink.compareDocumentPosition(anchor) & Node.DOCUMENT_POSITION_FOLLOWING
            );
            return Boolean(match && afterPermalink);
        });
        observations.push({
            url: `${parsed.origin}/${outerMatch[1]}/status/${outerMatch[2]}`,
            post_id: outerMatch[2],
            author_handle: outerMatch[1],
            is_pinned: hasSocialContext,
            is_reply: isReply,
            is_repost: hasSocialContext,
            is_quote: Array.from(statusIds).some((id) => id !== outerMatch[2]),
        });
    }

    const host = window.location.hostname.toLowerCase();
    const pageSupported = host === 'x.com' || host === 'www.x.com';
    const path = window.location.pathname.toLowerCase();
    const hasLoginControls = Boolean(
        document.querySelector('[data-testid="loginButton"], a[href="/login"], form[action*="login"]')
    );
    const loginWall = path === '/login' || path.startsWith('/i/flow/login') || (
        hasLoginControls && observations.length === 0
    );
    const root = document.documentElement;
    const endReached = window.scrollY + window.innerHeight >= root.scrollHeight - 4;
    return {
        observations,
        login_wall: loginWall,
        end_reached: endReached,
        page_supported: pageSupported,
        page_url: window.location.href,
    };
})()
""".strip()


def build_extraction_script(_selected_handle: str) -> str:
    return f"JSON.stringify({_EXTRACTION_TEMPLATE})"


EXTRACTION_SCRIPT = build_extraction_script("openai")

SCROLL_SCRIPT = "window.scrollBy({top: Math.max(window.innerHeight * 0.8, 600), behavior: 'auto'});"


class ConnectableSignal(Protocol):
    def connect(self, callback: Callable[..., object]) -> object: ...

    def disconnect(self, callback: Callable[..., object]) -> object: ...


class PageDriver(Protocol):
    loadFinished: ConnectableSignal
    urlChanged: ConnectableSignal
    navigationDenied: ConnectableSignal

    def load(self, url: QUrl) -> None: ...

    def setUrl(self, url: QUrl) -> None: ...

    def runJavaScript(self, script: str, callback: Callable[[object], None]) -> None: ...

    def stop(self) -> None: ...

    def deleteLater(self) -> None: ...


class TimerDriver(Protocol):
    timeout: ConnectableSignal

    def setSingleShot(self, single_shot: bool) -> None: ...

    def start(self, interval: int) -> None: ...

    def stop(self) -> None: ...


@runtime_checkable
class ProfileCollector(Protocol):
    widget: QWidget
    progress: ConnectableSignal
    finished: ConnectableSignal

    def start(self, profile: SourceProfile) -> None: ...

    def cancel(self) -> None: ...


class SnapshotParseError(ValueError):
    """A rendered page snapshot does not match the collector boundary."""


@dataclass(frozen=True)
class PageSnapshot:
    observations: tuple[CandidateObservation, ...]
    login_wall: bool
    end_reached: bool
    page_supported: bool
    page_url: str


def parse_page_snapshot(payload: object) -> PageSnapshot:
    if not isinstance(payload, dict):
        raise SnapshotParseError("page snapshot must be a dictionary")

    observations_payload = payload.get("observations")
    if not isinstance(observations_payload, list):
        raise SnapshotParseError("snapshot observations must be a list")

    flags: dict[str, bool] = {}
    for field in ("login_wall", "end_reached", "page_supported"):
        value = payload.get(field)
        if not isinstance(value, bool):
            raise SnapshotParseError(f"snapshot {field} must be a boolean")
        flags[field] = value

    page_url = payload.get("page_url")
    if not isinstance(page_url, str) or not page_url:
        raise SnapshotParseError("snapshot page_url must be a non-empty string")

    observations: list[CandidateObservation] = []
    for order, raw_observation in enumerate(observations_payload):
        if not isinstance(raw_observation, dict):
            raise SnapshotParseError("each observation must be a dictionary")

        strings: dict[str, str] = {}
        for field in ("url", "post_id", "author_handle"):
            value = raw_observation.get(field)
            if not isinstance(value, str) or not value:
                raise SnapshotParseError(f"observation {field} must be a non-empty string")
            strings[field] = value

        booleans: dict[str, bool] = {}
        for field in ("is_pinned", "is_reply", "is_repost", "is_quote"):
            value = raw_observation.get(field)
            if not isinstance(value, bool):
                raise SnapshotParseError(f"observation {field} must be a boolean")
            booleans[field] = value

        observations.append(
            CandidateObservation(
                url=strings["url"],
                post_id=strings["post_id"],
                author_handle=strings["author_handle"],
                is_pinned=booleans["is_pinned"],
                is_reply=booleans["is_reply"],
                is_repost=booleans["is_repost"],
                is_quote=booleans["is_quote"],
                discovery_order=order,
            )
        )

    return PageSnapshot(
        observations=tuple(observations),
        login_wall=flags["login_wall"],
        end_reached=flags["end_reached"],
        page_supported=flags["page_supported"],
        page_url=page_url,
    )


def is_allowed_profile_url(value: str | QUrl, canonical_profile_url: str) -> bool:
    candidate = QUrl(value)
    canonical = QUrl(canonical_profile_url)
    return (
        candidate.isValid()
        and candidate.scheme().casefold() == "https"
        and candidate.host().casefold() == "x.com"
        and candidate.port(-1) == -1
        and not candidate.userInfo()
        and candidate.path().rstrip("/") == canonical.path().rstrip("/")
    )


def is_login_wall_url(value: str | QUrl) -> bool:
    candidate = QUrl(value)
    path = candidate.path().casefold().rstrip("/")
    return (
        candidate.isValid()
        and candidate.scheme().casefold() == "https"
        and candidate.host().casefold() in ("x.com", "www.x.com")
        and candidate.port(-1) == -1
        and not candidate.userInfo()
        and (path == "/login" or path == "/i/flow/login")
    )


class RestrictedPublicPage(QWebEnginePage):
    navigationDenied = Signal(QUrl)

    def __init__(
        self,
        profile: QWebEngineProfile,
        canonical_profile_url: str,
        parent: QObject | None = None,
    ) -> None:
        super().__init__(profile, parent)
        self._canonical_profile_url = canonical_profile_url

    def acceptNavigationRequest(
        self,
        url: QUrl | str,
        navigation_type: QWebEnginePage.NavigationType,
        is_main_frame: bool,
    ) -> bool:
        del navigation_type
        allowed = not is_main_frame or is_allowed_profile_url(url, self._canonical_profile_url)
        if not allowed:
            self.navigationDenied.emit(QUrl(url))
        return allowed

    def stop(self) -> None:
        self.triggerAction(QWebEnginePage.WebAction.Stop)


class PublicProfileCollector(QObject):
    progress = Signal(object)
    finished = Signal(object)

    def __init__(
        self,
        *,
        borrowed_profile: QWebEngineProfile | None = None,
        page_factory: Callable[[QUrl], PageDriver] | None = None,
        widget: QWidget | None = None,
        timer: TimerDriver | None = None,
        clock: Callable[[], float] = time.monotonic,
        maximum: int = 30,
        settle_ms: int = 750,
        maximum_no_progress: int = 3,
        timeout_ms: int = 30_000,
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent)
        if not 1 <= maximum <= 30:
            raise ValueError("maximum must be between 1 and 30")
        if settle_ms < 0:
            raise ValueError("settle_ms must not be negative")
        if maximum_no_progress < 1:
            raise ValueError("maximum_no_progress must be positive")
        if timeout_ms < 1:
            raise ValueError("timeout_ms must be positive")

        self.widget: QWidget
        self._borrowed_profile = borrowed_profile
        self._page_factory: Callable[[QUrl], PageDriver]
        if page_factory is None:
            view = widget if isinstance(widget, QWebEngineView) else QWebEngineView()
            self.widget = view
            self._page_factory = self._create_public_page
        else:
            self.widget = widget or QWidget()
            self._page_factory = page_factory

        self._timer = timer or cast(TimerDriver, QTimer(self))
        self._timer.setSingleShot(True)
        self._timer.timeout.connect(self._on_timer)
        self._clock = clock
        self._maximum = maximum
        self._settle_ms = settle_ms
        self._maximum_no_progress = maximum_no_progress
        self._timeout_ms = timeout_ms

        self._active = False
        self._generation = 0
        self._state = "idle"
        self._deadline = 0.0
        self._profile: SourceProfile | None = None
        self._page: PageDriver | None = None
        self._page_profile: QWebEngineProfile | None = None
        self._owns_page_profile = False
        self._load_callback: Callable[..., object] | None = None
        self._url_callback: Callable[..., object] | None = None
        self._navigation_denied_callback: Callable[..., object] | None = None
        self._used_pages: weakref.WeakSet[object] = weakref.WeakSet()
        self._candidates: list[CandidateObservation] = []
        self._seen_post_ids: set[str] = set()
        self._no_progress = 0
        self._empty_end_passes = 0

    def start(self, profile: SourceProfile) -> None:
        if self._active:
            self._generation += 1
            self._finish(
                DiscoveryReason.CANCELLED,
                "superseded by a new collection run",
            )
        self._generation += 1
        self._active = True
        self._state = "loading"
        self._deadline = self._clock() + self._timeout_ms / 1000
        self._candidates = []
        self._seen_post_ids = set()
        self._no_progress = 0
        self._empty_end_passes = 0

        try:
            self._profile = canonicalize_profile(profile.handle)
        except ValueError as error:
            self._finish(DiscoveryReason.ERROR, str(error))
            return

        generation = self._generation
        try:
            page = self._page_factory(QUrl(self._profile.profile_url))
            if page in self._used_pages:
                raise RuntimeError("page factory reused a previous collection page")
            self._used_pages.add(page)
            self._page = page
            self._load_callback = lambda succeeded: self._on_load_finished(
                generation, page, bool(succeeded)
            )
            self._url_callback = lambda url: self._on_url_changed(generation, page, QUrl(url))
            self._navigation_denied_callback = lambda url: self._on_navigation_denied(
                generation, page, QUrl(url)
            )
            page.loadFinished.connect(self._load_callback)
            page.urlChanged.connect(self._url_callback)
            page.navigationDenied.connect(self._navigation_denied_callback)
        except Exception as error:
            self._finish(DiscoveryReason.ERROR, f"public page setup failed: {error}")
            return

        self._start_watchdog()
        try:
            page.load(QUrl(self._profile.profile_url))
        except Exception as error:
            self._finish(DiscoveryReason.ERROR, f"profile load failed: {error}")

    def cancel(self) -> None:
        if not self._active:
            return
        self._generation += 1
        self._finish(DiscoveryReason.CANCELLED, "collection cancelled")

    def _on_load_finished(self, generation: int, page: PageDriver, succeeded: bool) -> None:
        if not self._accept_page_event(generation, page, "loading"):
            return
        self._timer.stop()
        if not succeeded:
            self._finish(DiscoveryReason.ERROR, "profile page failed to load")
            return
        if self._timed_out():
            self._finish(DiscoveryReason.TIMEOUT, "collection timed out")
            return
        self._schedule_settle()

    def _on_url_changed(self, generation: int, page: PageDriver, url: QUrl) -> None:
        if not self._accept_run_page(generation, page):
            return
        if self._profile is None or not is_allowed_profile_url(url, self._profile.profile_url):
            self._finish_navigation(url)

    def _on_navigation_denied(self, generation: int, page: PageDriver, url: QUrl) -> None:
        if not self._accept_run_page(generation, page):
            return
        self._finish_navigation(url)

    def _finish_navigation(self, url: QUrl) -> None:
        if is_login_wall_url(url):
            self._finish(DiscoveryReason.LOGIN_WALL, "X navigated to a login wall")
        else:
            self._finish(DiscoveryReason.ERROR, "navigation left the selected public profile")

    def _on_timer(self) -> None:
        if not self._active:
            return
        if self._timed_out():
            self._finish(DiscoveryReason.TIMEOUT, "collection timed out")
            return
        if self._state == "settling":
            self._extract()
            return
        self._start_watchdog()

    def _schedule_settle(self) -> None:
        if not self._active:
            return
        self._state = "settling"
        self._timer.start(min(self._settle_ms, self._remaining_ms()))

    def _start_watchdog(self) -> None:
        self._timer.start(self._remaining_ms())

    def _remaining_ms(self) -> int:
        return max(1, math.ceil((self._deadline - self._clock()) * 1000))

    def _timed_out(self) -> bool:
        return self._clock() >= self._deadline

    def _extract(self) -> None:
        generation = self._generation
        page = self._page
        if page is None:
            self._finish(DiscoveryReason.ERROR, "public page is unavailable")
            return
        self._state = "extracting"
        try:
            page.runJavaScript(
                build_extraction_script(self._profile.handle if self._profile is not None else ""),
                lambda payload: self._on_snapshot(generation, page, payload),
            )
        except Exception as error:
            self._finish(DiscoveryReason.ERROR, f"page extraction failed: {error}")
            return
        if self._active and self._state == "extracting":
            self._start_watchdog()

    def _on_snapshot(self, generation: int, page: PageDriver, payload: object) -> None:
        if not self._accept_page_event(generation, page, "extracting"):
            return
        self._timer.stop()
        if self._timed_out():
            self._finish(DiscoveryReason.TIMEOUT, "collection timed out")
            return
        try:
            if isinstance(payload, str):
                payload = json.loads(payload)
            snapshot = parse_page_snapshot(payload)
        except (json.JSONDecodeError, SnapshotParseError) as error:
            self._finish(DiscoveryReason.ERROR, f"malformed page snapshot: {error}")
            return

        if snapshot.login_wall:
            self._finish(DiscoveryReason.LOGIN_WALL, "X displayed a login wall")
            return
        if not snapshot.page_supported:
            self._finish(DiscoveryReason.ERROR, "X profile page is not supported")
            return
        if self._profile is None or not is_allowed_profile_url(
            snapshot.page_url, self._profile.profile_url
        ):
            self._finish(DiscoveryReason.ERROR, "page left the selected public profile")
            return

        added = self._add_qualifying(snapshot, generation)
        if not self._active or generation != self._generation:
            return
        self._no_progress = 0 if added else self._no_progress + 1
        if added or not snapshot.end_reached:
            self._empty_end_passes = 0
        else:
            self._empty_end_passes += 1
        if len(self._candidates) >= self._maximum:
            self._finish(DiscoveryReason.LIMIT)
        elif self._empty_end_passes >= max(2, self._maximum_no_progress):
            self._finish(DiscoveryReason.EXHAUSTED)
        elif not snapshot.end_reached and self._no_progress >= self._maximum_no_progress:
            self._finish(DiscoveryReason.NO_PROGRESS, "no new qualifying posts were rendered")
        else:
            self._scroll(generation, page)

    def _add_qualifying(self, snapshot: PageSnapshot, generation: int) -> int:
        if self._profile is None:
            return 0
        added = 0
        normalized = qualifying_candidates(snapshot.observations, self._profile.handle, maximum=30)
        for candidate in normalized:
            if candidate.post_id in self._seen_post_ids:
                continue
            candidate = replace(candidate, discovery_order=len(self._candidates))
            self._seen_post_ids.add(candidate.post_id)
            self._candidates.append(candidate)
            self.progress.emit(candidate)
            added += 1
            if not self._active or generation != self._generation:
                break
            if len(self._candidates) >= self._maximum:
                break
        return added

    def _scroll(self, generation: int, page: PageDriver) -> None:
        if not self._accept_page_event(generation, page, "extracting"):
            return
        self._state = "scrolling"
        try:
            page.runJavaScript(
                SCROLL_SCRIPT,
                lambda _payload: self._on_scrolled(generation, page),
            )
        except Exception as error:
            self._finish(DiscoveryReason.ERROR, f"page scroll failed: {error}")
            return
        if self._active and self._state == "scrolling":
            self._start_watchdog()

    def _on_scrolled(self, generation: int, page: PageDriver) -> None:
        if not self._accept_page_event(generation, page, "scrolling"):
            return
        self._timer.stop()
        if self._timed_out():
            self._finish(DiscoveryReason.TIMEOUT, "collection timed out")
            return
        self._schedule_settle()

    def _accept_page_event(self, generation: int, page: PageDriver, expected_state: str) -> bool:
        return self._accept_run_page(generation, page) and self._state == expected_state

    def _accept_run_page(self, generation: int, page: PageDriver) -> bool:
        return self._active and generation == self._generation and page is self._page

    def _finish(self, reason: DiscoveryReason, diagnostic: str | None = None) -> None:
        if not self._active:
            return
        self._active = False
        self._state = "idle"
        self._timer.stop()
        result = DiscoveryResult(tuple(self._candidates), reason, diagnostic)
        self._dispose_current_page()
        self.finished.emit(result)

    def _create_public_page(self, canonical_url: QUrl) -> PageDriver:
        if not isinstance(self.widget, QWebEngineView):
            raise RuntimeError("the default page factory requires a QWebEngineView")
        profile = self._borrowed_profile
        owns_profile = profile is None
        if profile is None:
            profile = QWebEngineProfile(self.widget)
            profile.setHttpCacheType(QWebEngineProfile.HttpCacheType.MemoryHttpCache)
            profile.setPersistentCookiesPolicy(
                QWebEngineProfile.PersistentCookiesPolicy.NoPersistentCookies
            )
        self._page_profile = profile
        self._owns_page_profile = owns_profile
        page = RestrictedPublicPage(profile, canonical_url.toString(), self.widget)
        self.widget.setPage(page)
        return cast(PageDriver, page)

    def _dispose_current_page(self) -> None:
        page = self._page
        self._page = None
        if page is not None:
            for signal, callback in (
                (page.loadFinished, self._load_callback),
                (page.urlChanged, self._url_callback),
                (page.navigationDenied, self._navigation_denied_callback),
            ):
                if callback is not None:
                    try:
                        signal.disconnect(callback)
                    except (RuntimeError, TypeError):
                        pass
            try:
                page.stop()
            except RuntimeError:
                pass
            if isinstance(self.widget, QWebEngineView):
                self.widget.setPage(None)  # type: ignore[arg-type]  # Qt accepts null to detach.
            page.deleteLater()
        self._load_callback = None
        self._url_callback = None
        self._navigation_denied_callback = None
        profile = self._page_profile
        owns_profile = self._owns_page_profile
        self._page_profile = None
        self._owns_page_profile = False
        if owns_profile and profile is not None:
            profile.deleteLater()


class AuthenticatedProfileCollector(PublicProfileCollector):
    def __init__(self, profile: QWebEngineProfile, **collector_options: Any) -> None:
        super().__init__(borrowed_profile=profile, **collector_options)
