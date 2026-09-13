from PySide6.QtWidgets import QApplication


APPLICATION_STYLESHEET = """
QWidget {
    color: #202534;
    font-family: "Segoe UI";
    font-size: 14px;
}
QMainWindow, QWidget#workspace, QStackedWidget {
    background: #F8FAFC;
}
QWidget#sidebar {
    background: #151A27;
    border: none;
}
QLabel#brandMark {
    color: #FFFFFF;
    font-size: 21px;
    font-weight: 700;
}
QLabel#brandCaption {
    color: #929AAF;
    font-size: 11px;
}
QListWidget#primaryNavigation {
    background: transparent;
    border: none;
    color: #AEB5C7;
    outline: none;
    padding: 4px 10px;
}
QListWidget#primaryNavigation::item {
    border-radius: 10px;
    margin: 3px 0;
    padding: 12px 14px;
}
QListWidget#primaryNavigation::item:hover {
    background: #202738;
    color: #FFFFFF;
}
QListWidget#primaryNavigation::item:selected {
    background: #2B3150;
    color: #FFFFFF;
    border-left: 3px solid #8A7CFF;
}
QLabel#pageTitle {
    color: #151A27;
    font-family: "Segoe UI Variable Display", "Segoe UI";
    font-size: 26px;
    font-weight: 700;
}
QLabel#pageSubtitle, QLabel#mutedLabel {
    color: #697386;
}
QFrame#card, QFrame#statusBanner, QFrame#emptyState {
    background: #FFFFFF;
    border: 1px solid #E0E5ED;
    border-radius: 14px;
}
QWidget#insightsCanvas, QWidget#insightsBody {
    background: #F8FAFC;
}
QFrame#insightsFilterBand, QFrame#summaryCard, QFrame#insightsSection,
QFrame#topicSpectrum, QFrame#errorState {
    background: #FFFFFF;
    border: 1px solid #E0E5ED;
    border-radius: 12px;
}
QLabel#controlLabel, QLabel#summaryLabel, QLabel#sectionSubtitle {
    color: #697386;
    font-size: 11px;
    font-weight: 600;
}
QLabel#analysisStatus {
    color: #4A5366;
    font-size: 12px;
}
QLabel#lastCompletedStatus {
    color: #697386;
    font-size: 11px;
}
QLabel#summaryValue {
    color: #151A27;
    font-family: "Segoe UI Variable Display", "Segoe UI";
    font-size: 21px;
    font-weight: 700;
}
QLabel#sectionTitle, QLabel#stateTitle {
    color: #202534;
    font-family: "Segoe UI Variable Display", "Segoe UI";
    font-size: 16px;
    font-weight: 700;
}
QLabel#topicMessage {
    color: #4A5366;
    background: #F8FAFC;
    border-radius: 7px;
    padding: 7px 9px;
}
QLabel#distributionLabel {
    color: #313849;
    font-size: 12px;
    font-weight: 600;
}
QProgressBar#distributionBar, QProgressBar#topicBar {
    min-height: 23px;
    max-height: 23px;
    border: none;
    border-radius: 5px;
    background: #EEF1F5;
    color: #202534;
    text-align: right;
    padding-right: 7px;
    font-size: 11px;
    font-weight: 600;
}
QProgressBar#distributionBar::chunk {
    background: #C7CDD8;
    border-radius: 5px;
}
QProgressBar#topicBar {
    min-height: 28px;
    max-height: 28px;
    background: #ECE9FF;
    color: #151A27;
}
QProgressBar#topicBar::chunk {
    background: #6D5DFC;
    border-radius: 5px;
}
QFrame#errorState {
    border-color: #E4BCC2;
}
QTabWidget#feedTabs::pane {
    background: #F8FAFC;
    border: none;
    border-top: 1px solid #E0E5ED;
}
QTabWidget#feedTabs QTabBar {
    background: #FFFFFF;
}
QTabWidget#feedTabs QTabBar::tab {
    min-height: 38px;
    padding: 0 22px;
    background: #FFFFFF;
    color: #697386;
    border: none;
    border-bottom: 3px solid transparent;
    font-family: "Segoe UI Variable Display", "Segoe UI";
    font-weight: 600;
}
QTabWidget#feedTabs QTabBar::tab:hover {
    background: #F8FAFC;
    color: #202534;
}
QTabWidget#feedTabs QTabBar::tab:selected {
    background: #ECE9FF;
    color: #151A27;
    border-bottom: 3px solid #6D5DFC;
}
QTabWidget#feedTabs QTabBar::tab:focus {
    outline: none;
    border: 2px solid #6D5DFC;
    border-bottom: 3px solid #6D5DFC;
}
QPushButton {
    min-height: 34px;
    padding: 0 14px;
    border: 1px solid #D5DAE4;
    border-radius: 9px;
    background: #FFFFFF;
    color: #2B3140;
    font-weight: 600;
}
QPushButton:hover { border-color: #9C91FF; background: #F7F5FF; }
QPushButton:pressed { background: #ECE9FF; }
QPushButton:focus { border: 2px solid #6D5DFC; }
QPushButton:disabled { color: #A5ACB9; background: #F1F3F6; border-color: #E4E7ED; }
QPushButton[primary="true"] { background: #6D5DFC; color: #FFFFFF; border-color: #6D5DFC; }
QPushButton[primary="true"]:hover { background: #5C4DE8; }
QPushButton[danger="true"] { color: #B74655; }
QLineEdit, QComboBox {
    min-height: 34px;
    padding: 0 10px;
    border: 1px solid #D5DAE4;
    border-radius: 9px;
    background: #FFFFFF;
    selection-background-color: #6D5DFC;
}
QLineEdit:focus, QComboBox:focus { border: 2px solid #6D5DFC; }
QScrollBar:vertical { background: transparent; width: 10px; margin: 2px; }
QScrollBar::handle:vertical { background: #C7CDD8; border-radius: 4px; min-height: 36px; }
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical { height: 0; }
"""


def apply_application_theme(application: QApplication) -> None:
    application.setStyle("Fusion")
    application.setStyleSheet(APPLICATION_STYLESHEET)
