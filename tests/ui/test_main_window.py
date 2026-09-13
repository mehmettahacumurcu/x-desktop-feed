import pytest
from PySide6.QtWidgets import QWidget

from xfeed import i18n
from xfeed.ui.components import ConnectionStatusWidget
from xfeed.ui.main_window import MainWindow
from xfeed.ui.settings import UiSettingsStore
from xfeed.x_session import SessionState


def test_main_window_exposes_three_primary_destinations(qtbot):
    window = MainWindow()
    qtbot.addWidget(window)

    assert window.destination_names() == ("My Feed", "Sources", "Insights", "Export")
    assert window.current_destination() == "My Feed"
    window.navigate("Sources")
    assert window.current_destination() == "Sources"
    window.navigate("My Feed")
    assert window.current_destination() == "My Feed"


def test_main_window_exposes_injected_pages_by_destination(qtbot):
    sources = QWidget()
    feed = QWidget()
    insights = QWidget()
    window = MainWindow(pages={"Sources": sources, "My Feed": feed, "Insights": insights})
    qtbot.addWidget(window)

    assert window.page("My Feed") is feed
    assert window.page("Sources") is sources
    assert window.page("Insights") is insights


def test_main_window_rejects_unknown_page_lookup(qtbot):
    window = MainWindow()
    qtbot.addWidget(window)

    with pytest.raises(ValueError, match="Unknown destination: Missing"):
        window.page("Missing")


def test_main_window_enforces_minimum_and_restores_persisted_size(tmp_path, qtbot):
    settings = UiSettingsStore(tmp_path / "ui.ini")
    settings.set_window_size(1330, 810)

    window = MainWindow(settings=settings)
    qtbot.addWidget(window)

    assert window.minimumWidth() >= 960
    assert window.minimumHeight() >= 640
    assert window.width() == 1330
    assert window.height() == 810


@pytest.mark.parametrize(
    ("state", "copy", "can_connect", "can_disconnect"),
    [
        (SessionState.CHECKING, "Checking", False, False),
        (SessionState.SIGNED_OUT, "X signed out", True, False),
        (SessionState.SIGNING_IN, "Connecting", False, False),
        (SessionState.SIGNED_IN, "Opera connected", False, True),
        (SessionState.UNAVAILABLE, "Opera disconnected", True, False),
    ],
)
def test_connection_widget_renders_session_states_and_actions(
    state, copy, can_connect, can_disconnect, qtbot
):
    widget = ConnectionStatusWidget()
    qtbot.addWidget(widget)

    widget.set_session_state(state)

    assert copy in widget.status_label.text()
    assert widget.connect_button.isEnabled() is can_connect
    assert widget.disconnect_button.isEnabled() is can_disconnect


def test_main_window_relays_sidebar_connection_requests(qtbot):
    window = MainWindow()
    qtbot.addWidget(window)
    connected: list[bool] = []
    disconnected: list[bool] = []
    window.connect_requested.connect(lambda: connected.append(True))
    window.disconnect_requested.connect(lambda: disconnected.append(True))

    window.set_session_state(SessionState.SIGNED_OUT)
    window.connection_status.connect_button.click()
    window.set_session_state(SessionState.SIGNED_IN)
    window.connection_status.disconnect_button.click()

    assert connected == [True]
    assert disconnected == [True]


def test_main_window_language_selector_persists_selection(tmp_path, qtbot, monkeypatch):
    settings = UiSettingsStore(tmp_path / "ui.ini")
    monkeypatch.setattr(i18n, "_current", "en")
    window = MainWindow(settings=settings)
    qtbot.addWidget(window)

    assert window.language_combo.currentData() == "en"

    index = window.language_combo.findData("tr")
    window.language_combo.setCurrentIndex(index)

    assert i18n.current_language() == "tr"
    assert settings.language() == "tr"
    assert window.language_combo.currentData() == "tr"
