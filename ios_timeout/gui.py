"""Desktop recovery workspace. Device operations run in isolated terminal workers."""

import codecs
import json
import os
import platform
import sys
import threading
from datetime import datetime, timezone
from pathlib import Path

from PySide6.QtCore import QProcess, QProcessEnvironment, QSettings, Qt, QThread, QTimer, QUrl, Signal
from PySide6.QtGui import QColor, QDesktopServices, QFont, QIcon, QPalette, QTextCursor
from PySide6.QtWidgets import (
    QApplication, QButtonGroup, QCheckBox, QComboBox, QDialog, QDialogButtonBox,
    QFileDialog, QFormLayout, QFrame, QHBoxLayout, QHeaderView, QLabel, QLineEdit,
    QMainWindow, QMessageBox, QPlainTextEdit, QProgressBar, QPushButton, QScrollArea,
    QSizePolicy, QSpinBox, QSplitter, QStackedWidget, QTableWidget, QTableWidgetItem,
    QVBoxLayout, QWidget,
)

from . import APP_NAME, __version__
from .core import ResetError, SUPPORTED
from .desktop_model import Settings, TerminalText, backup_inventory, operation_arguments
from .host import state_directory
from .session_log import SessionLog
from .gui_flow import TimerFlow, action_copy
from . import desktop_updates
from .desktop_protocol import EventDecoder, encode_reply, encode_interrupt, encode_raw
from .desktop_results import summarize

STYLE = """
QWidget { color: #e6e6e6; font-size: 13px; }
QMainWindow, #workspace { background: #000000; }
#sidebar { background: #000000; border-right: 1px solid #303030; }
#sidebar #brand { color: #f5f5f5; font-size: 20px; font-weight: 700; }
#sidebar QLabel { color: #a3a3a3; }
#sidebar #edition { color: #999999; font-size: 11px; }
#sidebar QPushButton { background: transparent; color: #b3b3b3; border: 1px solid transparent;
    border-radius: 8px; padding: 11px 12px; text-align: left; font-weight: 500; }
#sidebar QPushButton:checked { color: #a9c6ff; background: #172c4c; font-weight: 700; }
#sidebar QPushButton:hover { background: #141414; }
#sidebar QPushButton:focus { border-color: #80aaff; }
#pageTitle { color: #f5f5f5; font-size: 28px; font-weight: 700; }
#subtitle { color: #a3a3a3; font-size: 13px; }
#card, #phoneBar, #activityPanel { background: #000000; border: 1px solid #303030; border-radius: 12px; }
#cardTitle { font-size: 19px; font-weight: 650; color: #f5f5f5; }
#eyebrow { color: #a3a3a3; font-size: 11px; font-weight: 600; }
#muted { color: #b3b3b3; }
#notice { background: #000000; border: 1px solid #31496b; border-radius: 8px; padding: 12px; color: #b4cef5; }
#warning { background: #000000; border: 1px solid #685138; border-radius: 8px; padding: 12px; color: #f0c78d; }
#connection { color: #b3b3b3; font-size: 12px; }
#step { color: #a3a3a3; background: #000000; border: 1px solid #303030; border-radius: 7px; padding: 9px; font-size: 12px; }
#step[active="true"] { color: #b7d0ff; border-color: #79a8ff; font-weight: 700; }
#step[complete="true"] { color: #9bd9be; border-color: #38634f; }
QPushButton { background: #000000; border: 1px solid #4a4a4a; border-radius: 7px; padding: 10px 15px; font-weight: 600; }
QPushButton:hover { background: #141414; border-color: #6d96dd; }
QPushButton:focus { border: 1px solid #91b6ff; }
QPushButton:pressed { background: #242424; }
QPushButton:disabled { background: #000000; color: #808080; border-color: #262626; }
QPushButton#primary { background: #285fcf; color: #ffffff; border: 1px solid #285fcf; }
QPushButton#primary:hover { background: #3471e6; }
QPushButton#primary:pressed { background: #1e4ca8; }
QPushButton#primary:disabled { background: #000000; border-color: #303030; color: #808080; }
QPushButton#quiet { background: transparent; color: #adcbf9; border: 1px solid transparent; }
QPushButton#quiet:hover { background: #141414; }
QPushButton#danger { color: #f4a398; }
#disclosure { background: transparent; border: 0; }
QPushButton#disclosureToggle { text-align: left; color: #b3b3b3; background: transparent; border: 0; padding: 9px 0; }
QLineEdit, QComboBox, QSpinBox { background: #000000; border: 1px solid #4a4a4a; border-radius: 6px; padding: 7px; min-height: 20px; }
QLineEdit:focus, QComboBox:focus, QSpinBox:focus { border: 1px solid #8db5ff; }
QLineEdit:disabled, QComboBox:disabled, QSpinBox:disabled { background: #000000; color: #808080; }
QComboBox QAbstractItemView { background: #000000; color: #e6e6e6; selection-background-color: #172c4c; }
QCheckBox { spacing: 8px; padding: 4px 0; }
QTableWidget { background: #000000; border: 1px solid #303030; border-radius: 7px; gridline-color: #262626; selection-background-color: #172c4c; selection-color: #b7d0ff; }
QHeaderView::section { background: #000000; border: 0; border-bottom: 1px solid #303030; padding: 10px; color: #b3b3b3; font-weight: 600; }
#console { background: #000000; color: #d4d4d4; border: 0; border-radius: 8px; padding: 10px; font-family: Menlo, Consolas, monospace; font-size: 11px; }
#sessionTitle { font-weight: 650; color: #f5f5f5; }
#sessionHint { color: #b3b3b3; font-size: 12px; }
QProgressBar { background: #262626; border: 0; border-radius: 2px; min-height: 3px; max-height: 3px; }
QProgressBar::chunk { background: #79a8ff; }
QScrollArea { border: 0; background: transparent; }
QScrollBar:vertical { background: transparent; width: 8px; }
QScrollBar::handle:vertical { background: #595959; border-radius: 4px; min-height: 30px; }
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical { height: 0; }
QDialog { background: #000000; }
QToolTip { color: #e6e6e6; background: #000000; border: 1px solid #666666; padding: 6px; }
#guidanceTitle { font-size: 20px; font-weight: 650; color: #f5f5f5; }
#guidanceText { background: #000000; color: #d4d4d4; font-size: 14px; }
#promptInput { font-size: 15px; padding: 10px; }
QCheckBox::indicator { width: 16px; height: 16px; border: 1px solid #737373; border-radius: 3px; background: #000000; }
QCheckBox::indicator:checked { background: #285fcf; border-color: #a9c6ff; }
"""


def label(text, name=None):
    item = QLabel(text)
    item.setTextFormat(Qt.TextFormat.PlainText)
    item.setWordWrap(True)
    if name:
        item.setObjectName(name)
    return item


def card(title, description=""):
    frame = QFrame()
    frame.setObjectName("card")
    layout = QVBoxLayout(frame)
    layout.setContentsMargins(20, 16, 20, 16)
    layout.setSpacing(11)
    layout.addWidget(label(title, "cardTitle"))
    if description:
        layout.addWidget(label(description, "muted"))
    return frame, layout


def dark_palette():
    """Keep native and stylesheet surfaces OLED black on every host theme."""
    palette = QPalette()
    for role, color in (
        (QPalette.ColorRole.Window, "#000000"), (QPalette.ColorRole.WindowText, "#e6e6e6"),
        (QPalette.ColorRole.Base, "#000000"), (QPalette.ColorRole.AlternateBase, "#000000"),
        (QPalette.ColorRole.Text, "#e6e6e6"), (QPalette.ColorRole.Button, "#000000"),
        (QPalette.ColorRole.ButtonText, "#e6e6e6"), (QPalette.ColorRole.Highlight, "#285fcf"),
        (QPalette.ColorRole.HighlightedText, "#ffffff"), (QPalette.ColorRole.ToolTipBase, "#000000"),
        (QPalette.ColorRole.ToolTipText, "#e6e6e6"), (QPalette.ColorRole.PlaceholderText, "#999999"),
        (QPalette.ColorRole.Light, "#666666"), (QPalette.ColorRole.Dark, "#000000"),
        (QPalette.ColorRole.Midlight, "#4a4a4a"), (QPalette.ColorRole.Mid, "#303030"),
        (QPalette.ColorRole.Shadow, "#000000"), (QPalette.ColorRole.Link, "#8db5ff"),
        (QPalette.ColorRole.LinkVisited, "#c4b5fd"),
    ):
        palette.setColor(role, QColor(color))
    for role in (QPalette.ColorRole.Text, QPalette.ColorRole.ButtonText, QPalette.ColorRole.WindowText):
        palette.setColor(QPalette.ColorGroup.Disabled, role, QColor("#808080"))
    return palette


class Disclosure(QFrame):
    """Advanced controls stay available without competing with the next step."""

    def __init__(self, title):
        super().__init__()
        self.setObjectName("disclosure")
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(3)
        self.toggle = QPushButton("›  " + title)
        self.toggle.setObjectName("disclosureToggle")
        self.toggle.setCheckable(True)
        self.content = QWidget()
        self.body = QVBoxLayout(self.content)
        self.body.setContentsMargins(0, 4, 0, 8)
        self.body.setSpacing(10)
        self.content.hide()
        def expand(checked):
            self.content.setVisible(checked)
            self.toggle.setText(("⌄  " if checked else "›  ") + title)
        self.toggle.toggled.connect(expand)
        layout.addWidget(self.toggle)
        layout.addWidget(self.content)

    def setExpanded(self, expanded):
        self.toggle.setChecked(expanded)


class Task(QThread):
    result = Signal(object)
    failed = Signal(str)
    progress = Signal(str)

    def __init__(self, function, parent=None):
        super().__init__(parent)
        self.function = function

    def run(self):
        try:
            self.result.emit(self.function(self.progress.emit))
        except Exception as exc:
            self.failed.emit(str(exc))


class MainWindow(QMainWindow):
    def __init__(self, *, auto_scan=True):
        super().__init__()
        self.setWindowTitle(f"{APP_NAME} · {__version__}")
        self.setWindowIcon(QIcon(str(Path(__file__).with_name("app-icon.svg"))))
        self.resize(1120, 800)
        self.setMinimumSize(940, 680)
        self.settings = Settings()
        self.preferences = QSettings("channelramble", "Openup")
        self.busy = False
        self.operation_buttons = []
        self.task = None
        self.process = None
        self.output_decoder = codecs.getincrementaldecoder("utf-8")("replace")
        self.terminal_text = TerminalText()
        self.capture = ""
        self.event_decoder = EventDecoder()
        self.active_prompt = None
        self.prompt_buttons = []
        self.session_log = SessionLog()
        self.backup_rows = []
        self.operation = ""
        self.operation_apply = False
        self.operation_install_only = False
        self.timer_flow = TimerFlow()
        self.interrupted = False
        self.task_outcome = "Ready"
        self.cancel_task = None
        self.build_ui()
        geometry = self.preferences.value("window/geometry")
        if geometry:
            self.restoreGeometry(geometry)
        if os.environ.get("BL_IOS_UPDATE_ERROR"):
            self.task_failed(os.environ["BL_IOS_UPDATE_ERROR"])
            self.navigate(4)
        self.refresh_backups()
        if auto_scan:
            QTimer.singleShot(200, self.scan)

    def button(self, text, handler, primary=False, operation=True):
        button = QPushButton(text)
        if primary:
            button.setObjectName("primary")
        button.clicked.connect(handler)
        if operation:
            self.operation_buttons.append(button)
        return button

    def build_ui(self):
        outer = QWidget()
        layout = QHBoxLayout(outer)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        sidebar = QFrame()
        sidebar.setObjectName("sidebar")
        sidebar.setFixedWidth(183)
        nav = QVBoxLayout(sidebar)
        nav.setContentsMargins(16, 28, 16, 20)
        nav.setSpacing(7)
        nav.addWidget(label("Openup", "brand"))
        nav.addWidget(label("Version " + __version__, "edition"))
        nav.addSpacing(28)
        self.nav_group = QButtonGroup(self)
        for index, title in enumerate(("Start", "Reset timer", "Recover passcode", "Backups", "Updates", "Help")):
            if index == 3:
                nav.addSpacing(20)
            button = QPushButton(title)
            button.setCheckable(True)
            self.nav_group.addButton(button, index)
            nav.addWidget(button)
        self.nav_group.idToggled.connect(lambda index, checked: self.navigate(index) if checked else None)
        nav.addStretch()
        nav.addWidget(label("Legacy iPhone recovery", "edition"))
        nav.addWidget(self.button("User guide ↗", lambda: QDesktopServices.openUrl(
            QUrl("https://github.com/channelramble/Openup#readme")), operation=False))
        layout.addWidget(sidebar)
        workspace = QWidget()
        workspace.setObjectName("workspace")
        work = QVBoxLayout(workspace)
        work.setContentsMargins(26, 24, 26, 20)
        work.setSpacing(14)
        self.title = label("What would you like to do?", "pageTitle")
        title_row = QHBoxLayout()
        title_row.addWidget(self.title, 1)
        self.update_shortcut = self.button(f"v{__version__} · Updates", lambda: self.navigate(4), operation=False)
        title_row.addWidget(self.update_shortcut)
        work.addLayout(title_row)
        self.subtitle = label("", "subtitle")
        work.addWidget(self.subtitle)

        phone_bar = QFrame()
        phone_bar.setObjectName("phoneBar")
        phone_layout = QVBoxLayout(phone_bar)
        phone_layout.setContentsMargins(16, 12, 16, 12)
        phone_layout.setSpacing(7)
        row = QHBoxLayout()
        model_column = QVBoxLayout()
        model_column.setSpacing(5)
        model_column.addWidget(label("Your iPhone", "eyebrow"))
        self.phone_model = self.model_combo()
        self.phone_model.setAccessibleName("iPhone model")
        self.phone_model.currentIndexChanged.connect(self.model_changed)
        model_column.addWidget(self.phone_model)
        row.addLayout(model_column, 4)
        usb_column = QVBoxLayout()
        usb_column.setSpacing(5)
        self.connection = label("USB not checked", "connection")
        usb_column.addWidget(self.connection)
        usb_row = QHBoxLayout()
        self.device_combo = QComboBox()
        self.device_combo.setAccessibleName("USB phone")
        self.device_combo.addItem("Connect your phone by USB", "")
        self.device_combo.currentIndexChanged.connect(self.device_changed)
        usb_row.addWidget(self.device_combo, 1)
        usb_row.addWidget(self.button("Refresh", self.scan))
        usb_column.addLayout(usb_row)
        row.addLayout(usb_column, 5)
        phone_layout.addLayout(row)
        self.device_detail = label("DFU phones may be connected without appearing here. Choose the model to continue.", "muted")
        phone_layout.addWidget(self.device_detail)
        work.addWidget(phone_bar)

        self.split = QSplitter(Qt.Orientation.Vertical)
        self.split.setChildrenCollapsible(False)
        self.pages = QStackedWidget()
        for builder in (self.overview_page, self.reset_page, self.recovery_page,
                        self.backups_page, self.updates_page, self.diagnostics_page):
            scroll = QScrollArea()
            scroll.setWidgetResizable(True)
            scroll.setWidget(builder())
            self.pages.addWidget(scroll)
        self.split.addWidget(self.pages)
        session = QFrame()
        session.setObjectName("activityPanel")
        session_layout = QVBoxLayout(session)
        session_layout.setContentsMargins(16, 12, 16, 12)
        session_layout.setSpacing(8)
        header = QHBoxLayout()
        status = QVBoxLayout()
        status.setSpacing(3)
        self.session_status = label("Ready when you are", "sessionTitle")
        self.session_hint = label("Choose a task to get started.", "sessionHint")
        status.addWidget(self.session_status)
        status.addWidget(self.session_hint)
        header.addLayout(status, 1)
        self.details_toggle = self.button("Advanced · terminal", lambda: None, operation=False)
        self.details_toggle.setCheckable(True)
        self.details_toggle.setObjectName("quiet")
        self.details_toggle.toggled.connect(self.set_details_visible)
        header.addWidget(self.button("Save log…", self.save_output, operation=False))
        header.addWidget(self.details_toggle)
        self.stop_button = self.button("Stop", self.interrupt, operation=False)
        self.stop_button.setObjectName("danger")
        self.stop_button.setEnabled(False)
        self.stop_button.hide()
        header.addWidget(self.stop_button)
        session_layout.addLayout(header)
        self.progress = QProgressBar()
        self.progress.setTextVisible(False)
        self.progress.setRange(0, 1)
        self.progress.setValue(0)
        self.progress.hide()
        session_layout.addWidget(self.progress)
        self.build_guidance(session_layout)
        self.details_panel = QWidget()
        details = QVBoxLayout(self.details_panel)
        details.setContentsMargins(0, 0, 0, 0)
        details.setSpacing(8)
        toolbar = QHBoxLayout()
        toolbar.addWidget(label("ADVANCED TERMINAL", "eyebrow"), 1)
        self.manual_input = QCheckBox("Enable manual input")
        self.manual_input.setChecked(False)
        self.manual_input.toggled.connect(self.sync_manual_input)
        toolbar.addWidget(self.manual_input)
        toolbar.addWidget(self.button("Clear", self.clear_output, operation=False))
        details.addLayout(toolbar)
        self.console = QPlainTextEdit()
        self.console.setObjectName("console")
        self.console.setReadOnly(True)
        self.console.setMaximumBlockCount(2500)
        self.console.setPlaceholderText("Details appear here when a task runs. Save log exports this session.")
        self.console.setMinimumHeight(100)
        details.addWidget(self.console, 1)
        self.reply_row = QWidget()
        input_row = QHBoxLayout(self.reply_row)
        input_row.setContentsMargins(0, 0, 0, 0)
        self.reply = QLineEdit()
        self.reply.setPlaceholderText("Advanced: send a manual reply to the backend")
        self.reply.setAccessibleName("Advanced terminal input")
        self.reply.setEchoMode(QLineEdit.EchoMode.Password)
        self.reply.returnPressed.connect(self.send_input)
        self.reply.setEnabled(False)
        input_row.addWidget(self.reply, 1)
        self.hide_reply = QCheckBox("Hide typing")
        self.hide_reply.setChecked(True)
        self.hide_reply.toggled.connect(lambda checked: self.reply.setEchoMode(
            QLineEdit.EchoMode.Password if checked else QLineEdit.EchoMode.Normal))
        input_row.addWidget(self.hide_reply)
        self.send_button = self.button("Send", self.send_input, operation=False)
        self.send_button.setEnabled(False)
        input_row.addWidget(self.send_button)
        self.reply_row.hide()
        details.addWidget(self.reply_row)
        session_layout.addWidget(self.details_panel, 1)
        self.details_panel.hide()
        self.split.addWidget(session)
        self.split.setStretchFactor(0, 1)
        self.split.setStretchFactor(1, 0)
        self.split.setSizes([500, 82])
        work.addWidget(self.split, 1)
        layout.addWidget(workspace, 1)
        self.setCentralWidget(outer)
        self.navigate(0)
        self.sync_timer_flow()

    def build_guidance(self, parent_layout):
        self.guidance_panel = QWidget()
        body = QVBoxLayout(self.guidance_panel)
        body.setContentsMargins(0, 6, 0, 4)
        body.setSpacing(10)
        self.guidance_title = label("", "guidanceTitle")
        body.addWidget(self.guidance_title)
        self.reported_passcode = label("", "notice")
        self.reported_passcode.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.reported_passcode.hide()
        body.addWidget(self.reported_passcode)
        self.guidance_scroll = QScrollArea()
        self.guidance_scroll.setWidgetResizable(True)
        self.guidance_scroll.setMinimumHeight(55)
        self.guidance_scroll.setMaximumHeight(155)
        self.guidance_text = label("", "guidanceText")
        self.guidance_text.setAlignment(Qt.AlignmentFlag.AlignTop)
        self.guidance_text.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.guidance_scroll.setWidget(self.guidance_text)
        body.addWidget(self.guidance_scroll, 1)
        self.prompt_controls = QWidget()
        self.prompt_layout = QVBoxLayout(self.prompt_controls)
        self.prompt_layout.setContentsMargins(0, 0, 0, 0)
        self.prompt_layout.setSpacing(8)
        self.prompt_input_row = QWidget()
        input_layout = QHBoxLayout(self.prompt_input_row)
        input_layout.setContentsMargins(0, 0, 0, 0)
        self.prompt_input = QLineEdit()
        self.prompt_input.setObjectName("promptInput")
        self.prompt_input.setAccessibleName("Response for the current step")
        self.prompt_input.returnPressed.connect(self.submit_prompt_text)
        input_layout.addWidget(self.prompt_input, 1)
        self.prompt_submit = self.button("Continue", self.submit_prompt_text, primary=True, operation=False)
        input_layout.addWidget(self.prompt_submit)
        self.prompt_layout.addWidget(self.prompt_input_row)
        self.prompt_choices = QWidget()
        self.choice_layout = QHBoxLayout(self.prompt_choices)
        self.choice_layout.setContentsMargins(0, 0, 0, 0)
        self.prompt_layout.addWidget(self.prompt_choices)
        body.addWidget(self.prompt_controls)
        self.prompt_controls.hide()
        self.prompt_input_row.hide()
        self.guidance_panel.hide()
        parent_layout.addWidget(self.guidance_panel)

    def size_session(self):
        height = max(300, self.split.height())
        wanted = 82
        if not self.guidance_panel.isHidden():
            wanted += 215 if self.active_prompt else 165
        if not self.details_panel.isHidden():
            wanted += 220
        # Keep the task controls reachable; both task and guidance areas scroll.
        wanted = min(wanted, max(180, height - 125))
        self.split.setSizes([max(100, height - wanted), wanted])
        if self.timer_flow.stage == "review":
            QTimer.singleShot(0, self.ensure_timer_action_visible)

    def sync_manual_input(self):
        enabled = self.busy and self.process is not None and self.manual_input.isChecked()
        self.reply_row.setVisible(self.manual_input.isChecked() and not self.details_panel.isHidden())
        self.reply.setEnabled(enabled)
        self.send_button.setEnabled(enabled)
        if not enabled:
            self.reply.clear()

    def clear_prompt(self):
        self.active_prompt = None
        self.prompt_input.clear()
        self.prompt_controls.hide()
        self.prompt_input_row.hide()
        self.prompt_buttons = []
        while self.choice_layout.count():
            item = self.choice_layout.takeAt(0)
            if item.widget():
                item.widget().setEnabled(False)
                item.widget().deleteLater()

    def show_prompt(self, event):
        if not self.busy or self.process is None or not event.get("id"):
            return
        self.clear_prompt()
        self.active_prompt = event
        prompt_id = event["id"]
        self.guidance_panel.show()
        self.guidance_title.setText(event.get("title") or "Your phone needs attention")
        self.guidance_text.setText(event.get("message") or "Choose the next step below.")
        self.guidance_scroll.verticalScrollBar().setValue(0)
        self.session_hint.setText("Waiting for your response below.")
        kind = event.get("kind", "text")
        self.prompt_controls.show()
        options = event.get("options") or []
        if kind == "continue" and not options:
            options = [{"label": "I'm ready · continue", "value": ""}]
        if options:
            if len(options) > 3:
                choices = QComboBox()
                choices.setAccessibleName("Choose the next step")
                for option in options:
                    choices.addItem(option["label"], option["value"])
                self.choice_layout.addWidget(choices, 1)
                submit = self.button("Continue", lambda: self.answer_prompt(choices.currentData(), prompt_id),
                                     primary=True, operation=False)
                self.choice_layout.addWidget(submit)
                self.prompt_buttons.append(submit)
            else:
                for option in options:
                    button = self.button(option["label"], lambda checked=False, value=option["value"]:
                                         self.answer_prompt(value, prompt_id), primary=len(options) == 1, operation=False)
                    self.choice_layout.addWidget(button)
                    self.prompt_buttons.append(button)
                self.choice_layout.addStretch()
            self.prompt_choices.show()
            self.prompt_buttons[0].setFocus(Qt.FocusReason.OtherFocusReason)
        else:
            self.prompt_choices.hide()
            self.prompt_input_row.show()
            self.prompt_input.setEchoMode(QLineEdit.EchoMode.Password if kind == "password" or event.get("secret")
                                          else QLineEdit.EchoMode.Normal)
            self.prompt_input.setPlaceholderText("Password" if kind == "password" else "Enter the requested value")
            self.prompt_input.setFocus(Qt.FocusReason.OtherFocusReason)
        self.size_session()
        if not self.isActiveWindow():
            QApplication.alert(self)

    def answer_prompt(self, value, expected_id=None):
        if not self.busy or self.process is None or self.active_prompt is None:
            return
        prompt_id = self.active_prompt["id"]
        if expected_id is not None and expected_id != prompt_id:
            return
        try:
            encoded = encode_reply(prompt_id, str(value))
        except ValueError as exc:
            self.session_hint.setText(str(exc))
            return
        self.process.write(encoded)
        self.clear_prompt()
        self.session_hint.setText("Response sent. Waiting for the next step…")
        self.guidance_title.setText("Continuing…")
        self.guidance_text.setText("Keep your phone connected. The next instruction will appear here.")
        self.size_session()

    def submit_prompt_text(self):
        self.answer_prompt(self.prompt_input.text())

    def set_details_visible(self, visible):
        self.details_toggle.blockSignals(True)
        self.details_toggle.setChecked(visible)
        self.details_toggle.setText("Close terminal" if visible else "Advanced · terminal")
        self.details_toggle.blockSignals(False)
        self.details_panel.setVisible(visible)
        if not visible:
            self.manual_input.setChecked(False)
        self.sync_manual_input()
        self.size_session()
        if visible and self.timer_flow.stage == "review" and self.pages.currentIndex() == 1:
            QTimer.singleShot(0, self.ensure_timer_action_visible)

    def ensure_timer_action_visible(self):
        if self.pages.currentIndex() == 1 and self.timer_flow.stage == "review":
            self.pages.widget(1).ensureWidgetVisible(self.timer_primary, 0, 8)

    def page(self):
        widget = QWidget()
        layout = QVBoxLayout(widget)
        layout.setContentsMargins(0, 0, 8, 0)
        layout.setSpacing(16)
        return widget, layout

    def overview_page(self):
        widget, layout = self.page()
        row = QHBoxLayout()
        row.setSpacing(16)
        for context, title, description, button, page in (
            ("I KNOW THE PASSCODE", "Reset a lockout timer",
             "Prepare the phone, review the proposed changes, then reset the timer. You will still need your passcode.",
             "Reset timer", 1),
            ("I FORGOT THE PASSCODE", "Recover a passcode",
             "Start a guided session with the legacy recovery tool. The app guides you through each step on screen.",
             "Recover passcode", 2),
        ):
            box, inner = card(title)
            inner.insertWidget(0, label(context, "eyebrow"))
            inner.addWidget(label(description, "muted"))
            inner.addStretch()
            inner.addWidget(self.button(button, lambda checked=False, index=page: self.navigate(index),
                                         primary=True, operation=False))
            row.addWidget(box, 1)
        layout.addLayout(row)
        frame, body = card("Before you start")
        body.addWidget(label("Connect one phone by USB and choose its model above. If it is in DFU mode, it may not appear in the USB list.", "muted"))
        help_button = self.button("Need help connecting?", lambda: self.navigate(5), operation=False)
        help_button.setObjectName("quiet")
        body.addWidget(help_button, 0, Qt.AlignmentFlag.AlignLeft)
        layout.addWidget(frame)
        layout.addStretch()
        return widget

    def model_combo(self):
        combo = QComboBox()
        for model, (name, low, high) in SUPPORTED.items():
            combo.addItem(name, model)
            combo.setItemData(combo.count() - 1, model, Qt.ItemDataRole.ToolTipRole)
        combo.setCurrentIndex(max(0, combo.findData(self.settings.model)))
        return combo

    def file_row(self, placeholder, directory=False):
        row = QHBoxLayout()
        edit = QLineEdit()
        edit.setPlaceholderText(placeholder)
        row.addWidget(edit, 1)

        def browse():
            value = QFileDialog.getExistingDirectory(self, placeholder) if directory else QFileDialog.getOpenFileName(
                self, placeholder, "", "iPhone firmware (*.ipsw);;All files (*)")[0]
            if value:
                edit.setText(value)

        row.addWidget(self.button("Browse…", browse))
        return row, edit

    def reset_page(self):
        widget, layout = self.page()
        steps = QHBoxLayout()
        self.timer_steps = []
        for text in ("1  Prepare", "2  Review & reset", "3  Done"):
            item = label(text, "step")
            steps.addWidget(item, 1)
            self.timer_steps.append(item)
        layout.addLayout(steps)
        frame, body = card("Prepare your phone")
        self.timer_title = body.itemAt(0).widget()
        self.timer_description = label("", "muted")
        body.addWidget(self.timer_description)
        self.timer_note = label("", "notice")
        body.addWidget(self.timer_note)
        actions = QHBoxLayout()
        self.timer_primary = self.button("Prepare phone", self.run_timer_step, primary=True)
        self.timer_secondary = self.button("Already prepared? Inspect phone", self.timer_secondary_action)
        self.timer_secondary.setObjectName("quiet")
        actions.addWidget(self.timer_primary)
        actions.addWidget(self.timer_secondary)
        actions.addStretch()
        body.addLayout(actions)
        layout.addWidget(frame)
        self.reset_advanced = Disclosure("Advanced options")
        advanced = self.reset_advanced.body
        advanced.addWidget(label("Use a downloaded firmware file (optional)", "eyebrow"))
        row, self.ipsw_edit = self.file_row("iPhone 3G 3.1.3 / 7E18 IPSW")
        self.ipsw_edit.textChanged.connect(self.invalidate_timer_flow)
        advanced.addLayout(row)
        advanced.addWidget(label("For iPhone 3G only. Leave this empty to download the boot files automatically. The installed iOS version stays in place.", "muted"))
        advanced.addWidget(self.button("Connection settings…", self.options_dialog), 0, Qt.AlignmentFlag.AlignLeft)
        layout.addWidget(self.reset_advanced)
        layout.addStretch()
        return widget

    def run_timer_step(self):
        if self.busy:
            return
        if self.timer_flow.stage == "review":
            self.start_operation("reset", apply=True)
        elif self.timer_flow.stage == "complete":
            self.navigate(3)
        else:
            self.start_operation("prepare")

    def timer_secondary_action(self):
        if self.busy:
            return
        if self.timer_flow.stage == "complete":
            self.invalidate_timer_flow()
        else:
            self.start_operation("inspect")

    def invalidate_timer_flow(self, *_args):
        self.timer_flow.invalidate()
        if hasattr(self, "timer_primary"):
            self.sync_timer_flow()

    def sync_timer_flow(self):
        stage = self.timer_flow.stage
        title, description, note, primary, secondary = {
            "prepare": ("Prepare your phone",
                        "Boot a temporary recovery system so the app can inspect the lockout state. Follow the on-screen steps.",
                        "This step inspects the phone. It does not apply a timer reset.",
                        "Prepare phone", "Already prepared? Inspect phone"),
            "review": ("Review before resetting",
                       "Inspection finished. Review the proposed changes in the result card below before continuing.",
                       "The next step backs up the original files and asks for confirmation before applying the reset.",
                       "Back up and reset timer…", "Inspect again"),
            "complete": ("Reset operation finished",
                         "Check the result below. After rebooting, use the phone's existing passcode.",
                         "Any backup created by this operation is available from Backups.",
                         "View backups", "Start again"),
        }[stage]
        self.timer_title.setText(title)
        self.timer_description.setText(description)
        self.timer_note.setText(note)
        self.timer_primary.setText(primary)
        self.timer_secondary.setText(secondary)
        self.timer_primary.setEnabled(not self.busy)
        self.timer_secondary.setEnabled(not self.busy)
        current = ("prepare", "review", "complete").index(stage)
        for index, step in enumerate(self.timer_steps):
            step.setProperty("active", index == current)
            step.setProperty("complete", index < current)
            step.style().unpolish(step)
            step.style().polish(step)
        if stage == "review":
            QTimer.singleShot(0, self.ensure_timer_action_visible)

    def recovery_page(self):
        widget, layout = self.page()
        frame, body = card("Start a recovery session",
                           "Connect one phone by USB and start recovery. Follow the DFU instructions here; recovery then runs automatically on the phone.")
        body.addWidget(label("1  Connect your phone     →     2  Follow the DFU steps     →     3  Read the passcode on your iPhone", "muted"))
        self.armv6_notice = label("iPhone 3G tools are included. No separate tools folder is needed.", "notice")
        body.addWidget(self.armv6_notice)
        self.recovery_start = self.button("Start recovery", lambda: self.start_operation("recover", apply=True), primary=True)
        body.addWidget(self.recovery_start, 0, Qt.AlignmentFlag.AlignLeft)
        body.addWidget(label("The first run downloads the recovery backend and boot files. The final passcode appears on the iPhone automatically. No Show password menu or terminal input is needed.", "muted"))
        layout.addWidget(frame)
        self.recovery_advanced = Disclosure("Advanced options")
        advanced = self.recovery_advanced.body
        advanced.addWidget(label("Custom iPhone 3G tools (optional)", "eyebrow"))
        row, self.armv6_edit = self.file_row("Custom ARMv6 tools folder", directory=True)
        advanced.addLayout(row)
        advanced.addWidget(label("Leave this empty to use the included tools. Windows recovery uses WSL 2 and USB passthrough.", "muted"))
        actions = QHBoxLayout()
        actions.addWidget(self.button("Download tools only", lambda: self.start_operation("recover", install_only=True)))
        actions.addWidget(self.button("Connection settings…", self.options_dialog))
        actions.addStretch()
        advanced.addLayout(actions)
        layout.addWidget(self.recovery_advanced)
        layout.addStretch()
        return widget

    def backups_page(self):
        widget, layout = self.page()
        frame, body = card("Your saved backups", "A timer reset saves the original files here before changing them.")
        self.backup_table = QTableWidget(0, 4)
        self.backup_table.setHorizontalHeaderLabels(["Transaction", "Phone", "iOS", "Status"])
        self.backup_table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.backup_table.setSelectionMode(QTableWidget.SelectionMode.SingleSelection)
        self.backup_table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.backup_table.verticalHeader().hide()
        self.backup_table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        self.backup_table.horizontalHeader().setSectionResizeMode(3, QHeaderView.ResizeMode.Stretch)
        self.backup_table.setMinimumHeight(130)
        self.backup_table.itemSelectionChanged.connect(self.update_action_states)
        body.addWidget(self.backup_table)
        actions = QHBoxLayout()
        actions.addWidget(self.button("Refresh", self.refresh_backups))
        actions.addWidget(self.button("Choose folder…", self.choose_backup))
        self.restore_button = self.button("Restore selected backup…", lambda: self.recover_backup("restore"), primary=True)
        actions.addWidget(self.restore_button)
        body.addLayout(actions)
        self.backup_note = label("", "muted")
        body.addWidget(self.backup_note)
        layout.addWidget(frame)
        more = Disclosure("More backup options")
        more.body.addWidget(self.button("Open backups folder", self.open_backups, operation=False), 0, Qt.AlignmentFlag.AlignLeft)
        self.finalize_button = self.button("Finish interrupted cleanup…", lambda: self.recover_backup("finalize"))
        more.body.addWidget(self.finalize_button, 0, Qt.AlignmentFlag.AlignLeft)
        layout.addWidget(more)
        layout.addStretch()
        return widget

    def updates_page(self):
        widget, layout = self.page()
        frame, body = card("App updates", f"Running version {__version__}. Updates take effect after you restart this app.")
        self.update_info = label("Download and install the latest version for this computer directly in Openup. No browser or sign-in needed.", "notice")
        body.addWidget(self.update_info)
        body.addWidget(label("Choose Update now. Openup checks, downloads, and verifies the update, then offers a restart when it is ready.", "muted"))
        self.update_now_button = self.button("Update now", self.update_now, primary=True)
        body.addWidget(self.update_now_button, 0, Qt.AlignmentFlag.AlignLeft)
        self.relaunch_button = self.button("Restart to finish update", self.relaunch, primary=True, operation=False)
        self.relaunch_button.hide()
        body.addWidget(self.relaunch_button)
        self.update_options = Disclosure("Advanced update options")
        options = self.update_options.body
        self.prereleases = QCheckBox("Include prereleases")
        self.prereleases.setChecked(True)
        options.addWidget(self.prereleases)
        options.addWidget(label("Openup currently publishes prereleases. Leave this enabled to receive the latest version.", "muted"))
        options.addWidget(self.button("Restore previous app version", self.rollback_update), 0, Qt.AlignmentFlag.AlignLeft)
        self.local_update_button = self.button("Install downloaded update…", self.install_downloaded_update)
        options.addWidget(self.local_update_button, 0, Qt.AlignmentFlag.AlignLeft)
        options.addWidget(label("For a local installation, select the desktop ZIP and its matching .zip.sha256 file.", "muted"))
        options.addWidget(self.button("Release notes and downloads ↗", lambda: QDesktopServices.openUrl(
            QUrl(f"https://github.com/{desktop_updates.REPO}/releases")), operation=False), 0, Qt.AlignmentFlag.AlignLeft)
        options.addWidget(label("GitHub access token (optional)", "eyebrow"))
        self.token_edit = QLineEdit()
        self.token_edit.setEchoMode(QLineEdit.EchoMode.Password)
        self.token_edit.setPlaceholderText("Leave empty for public downloads")
        options.addWidget(self.token_edit)
        options.addWidget(label("Your token stays in memory for this session. It is not saved or passed to device operations.", "muted"))
        body.addWidget(self.update_options)
        layout.addWidget(frame)
        layout.addWidget(label("Downloads are checksum-verified, staged separately, and activated only after verification. Backups remain in your local workspace.", "muted"))
        layout.addStretch()
        return widget

    def diagnostics_page(self):
        widget, layout = self.page()
        frame, body = card("Help with your connection", f"{platform.system()} · {platform.machine()}   /   App {__version__}")
        body.addWidget(label("The desktop download contains Python, Qt, and SSH libraries. It does not install packages when you open it.", "notice"))
        requirement = (
            "Windows: device SSH requires Apple Mobile Device Service. Ramdisk tools require WSL 2 and usbipd-win; attach the phone to WSL."
            if sys.platform == "win32" else
            "Linux: USB device operations require usbmuxd and appropriate USB permissions. The desktop uses your system's display service."
            if sys.platform.startswith("linux") else
            "macOS: USB SSH uses the system usbmuxd service. Recovery backends may request their own tools and administrator access."
        )
        body.addWidget(label(requirement, "muted"))
        actions = QHBoxLayout()
        actions.addWidget(self.button("Check this computer", lambda: self.start_operation("doctor"), primary=True))
        actions.addWidget(self.button("Connection settings…", self.options_dialog))
        actions.addStretch()
        body.addLayout(actions)
        layout.addWidget(frame)
        layout.addWidget(label("Hardware validation is pending. Physical recovery, timer clearance, and Windows/Linux USB workflows have not been verified on a phone.", "warning"))
        layout.addWidget(label("State and backups: " + str(state_directory()), "muted"))
        layout.addStretch()
        return widget

    def navigate(self, index):
        names = ["What would you like to do?", "Reset the lockout timer", "Recover a passcode",
                 "Backups", "Updates", "Help & connection"]
        descriptions = ["Choose the task that matches your situation.",
                        "Prepare your phone, review the changes, then reset.",
                        "Follow the on-screen steps to prepare your phone and view the result.",
                        "Restore original files or finish an interrupted operation.",
                        "Keep the app and its included tools up to date.",
                        "Check this computer and adjust connection settings."]
        self.pages.setCurrentIndex(index)
        self.nav_group.button(index).setChecked(True)
        self.title.setText(names[index])
        self.subtitle.setText(descriptions[index])

    def model_changed(self):
        model = self.phone_model.currentData()
        self.settings.model = model
        self.invalidate_timer_flow()
        self.armv6_notice.setVisible(model == "iPhone1,2")
        self.ipsw_edit.setEnabled(not self.busy and model == "iPhone1,2")
        if model != "iPhone1,2":
            self.ipsw_edit.clear()

    def device_changed(self):
        selected = self.device_combo.currentData() or ""
        if selected != self.settings.udid:
            self.settings.udid = selected
            self.invalidate_timer_flow()

    def options_dialog(self):
        if self.busy:
            return
        dialog = QDialog(self)
        dialog.setWindowTitle("Connection settings")
        dialog.setMinimumWidth(530)
        layout = QVBoxLayout(dialog)
        form = QFormLayout()
        port = QSpinBox()
        port.setRange(1, 65535)
        port.setValue(self.settings.port)
        form.addRow("SSH port", port)
        fields = {}
        for name, title in (("mount", "Mount known legacy volumes if needed"),
                            ("ask_password", "Prompt for a custom SSH password"),
                            ("trust_new_key", "Replace the selected phone's saved SSH key"),
                            ("reboot", "Reboot after verified reset / restore"),
                            ("wsl", "Use WSL for USB SSH operations (Windows)")):
            item = QCheckBox(title)
            item.setChecked(getattr(self.settings, name))
            item.setEnabled(name != "wsl" or sys.platform == "win32")
            fields[name] = item
            form.addRow(item)
        distro = QLineEdit(self.settings.wsl_distro)
        distro.setPlaceholderText("Default distribution")
        distro.setEnabled(sys.platform == "win32")
        form.addRow("WSL distribution", distro)
        kit = QLineEdit(self.settings.kit)
        kit.setPlaceholderText("Optional existing Legacy iOS Kit folder")
        form.addRow("Existing backend", kit)
        layout.addLayout(form)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(dialog.accept)
        buttons.rejected.connect(dialog.reject)
        layout.addWidget(buttons)
        if dialog.exec() == QDialog.DialogCode.Accepted:
            self.settings.port = port.value()
            for name, item in fields.items():
                setattr(self.settings, name, item.isChecked())
            self.settings.wsl_distro = distro.text().strip()
            self.settings.kit = kit.text().strip()
            self.invalidate_timer_flow()

    def update_action_states(self):
        if not hasattr(self, "update_now_button"):
            return
        self.update_now_button.setEnabled(not self.busy)
        self.update_now_button.setVisible(self.relaunch_button.isHidden())
        row = self.backup_table.currentRow()
        valid_backup = 0 <= row < len(self.backup_rows) and self.backup_rows[row]["valid"]
        self.restore_button.setEnabled(not self.busy and valid_backup)
        self.finalize_button.setEnabled(not self.busy and valid_backup)
        self.sync_timer_flow()

    def set_busy(self, busy, message="Ready"):
        self.busy = busy
        for button in self.operation_buttons:
            button.setEnabled(not busy)
        for field in (self.phone_model, self.device_combo, self.armv6_edit,
                      self.token_edit, self.prereleases, self.backup_table):
            field.setEnabled(not busy)
        self.ipsw_edit.setEnabled(not busy and self.settings.model == "iPhone1,2")
        self.relaunch_button.setEnabled(not busy)
        self.session_status.setText(message)
        self.progress.setRange(0, 0 if busy else 1)
        self.progress.setValue(0)
        self.progress.setVisible(busy)
        self.stop_button.setVisible(busy)
        if not busy:
            self.clear_prompt()
            self.stop_button.setEnabled(False)
            self.reply.clear()
            self.reply.setEnabled(False)
            self.send_button.setEnabled(False)
            self.reply_row.hide()
        self.sync_manual_input()
        self.update_action_states()

    def append_output(self, text):
        self.session_log.append(text)
        cursor = self.console.textCursor()
        cursor.movePosition(QTextCursor.MoveOperation.End)
        cursor.insertText(text)
        self.console.setTextCursor(cursor)
        self.console.ensureCursorVisible()

    def clear_output(self):
        self.console.clear()
        self.session_log.clear()

    def scan(self):
        if self.busy:
            return
        self.connection.setText("Checking USB…")
        self.start_operation("devices")

    def start_operation(self, action, *, apply=False, backup=None, install_only=False):
        if self.busy:
            return
        if action == "reset" and apply and not self.timer_flow.can_apply:
            QMessageBox.information(self, "Inspect the phone first",
                                    "Prepare or inspect this phone, then review the proposed changes before resetting.")
            return
        self.settings.ipsw = self.ipsw_edit.text().strip()
        self.settings.armv6_tools = self.armv6_edit.text().strip()
        try:
            arguments = operation_arguments(action, self.settings, apply=apply, backup=backup, install_only=install_only)
        except (ResetError, ValueError) as exc:
            QMessageBox.information(self, "Choose recovery settings", str(exc))
            return
        if apply and action != "recover":
            description = {
                "reset": "Apply the timer-state reset to the selected phone? The app backs up original files before writing.",
                "restore": "Restore original files from this backup? The connected phone must match the saved identity.",
                "finalize": "Verify this transaction and finish its device-lock cleanup?",
            }.get(action, "Start this device operation?")
            if QMessageBox.question(self, action_copy(action, apply=apply).title, description,
                                    QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
                                    QMessageBox.StandardButton.Cancel) != QMessageBox.StandardButton.Yes:
                return
        root = Path(__file__).resolve().parent.parent
        executable = Path(sys.executable)
        if executable.name.lower() == "pythonw.exe":
            executable = executable.with_name("python.exe")
        self.operation = action
        self.operation_apply = apply
        self.operation_install_only = install_only
        self.interrupted = False
        if action in {"restore", "finalize"} or (action == "recover" and not install_only):
            self.timer_flow.invalidate()
        self.timer_flow.start(action, apply=apply)
        self.sync_timer_flow()
        self.capture = ""
        self.terminal_text = TerminalText()
        self.event_decoder = EventDecoder()
        self.clear_prompt()
        self.reported_passcode.clear()
        self.reported_passcode.hide()
        self.output_decoder = codecs.getincrementaldecoder("utf-8")("replace")
        self.append_output(
            "\n› " + action.replace("_", " ").capitalize() + "\n"
            + f"Started: {datetime.now(timezone.utc).isoformat(timespec='seconds')}\n"
            + f"App: {APP_NAME} {__version__}\n"
            + f"Action: {action}\n"
            + f"Model: {self.settings.model or 'Automatic'}\n"
            + f"Platform: {platform.system()} {platform.release()} / {platform.machine()}\n\n"
        )
        process = QProcess(self)
        self.process = process
        process.setProgram(str(executable))
        process.setArguments(["-I", "-B", "-u", str(root / "desktop.py"), "--session", *arguments])
        environment = QProcessEnvironment.systemEnvironment()
        for key in ("PYTHONPATH", "PYTHONHOME", "GH_TOKEN", "GITHUB_TOKEN"):
            environment.remove(key)
        environment.insert("BL_IOS_DESKTOP", "1")
        environment.insert("BL_IOS_UI_PROTOCOL", "1")
        environment.insert("BL_IOS_APP_ROOT", str(root))
        environment.insert("PYTHONNOUSERSITE", "1")
        process.setProcessEnvironment(environment)
        process.setWorkingDirectory(str(root))
        process.setProcessChannelMode(QProcess.ProcessChannelMode.MergedChannels)
        process.readyReadStandardOutput.connect(self.process_output)
        process.finished.connect(self.process_finished)
        process.errorOccurred.connect(self.process_error)
        copy = action_copy(action, apply=apply, install_only=install_only)
        self.set_busy(True, copy.running)
        self.session_hint.setText("Follow the on-screen steps. You can stop this session at any time.")
        self.guidance_panel.setVisible(action != "devices")
        self.guidance_title.setText(copy.running)
        self.guidance_text.setText("Preparing this task. Instructions will appear here when your phone needs attention.")
        self.sync_manual_input()
        self.stop_button.setEnabled(True)
        self.size_session()
        process.start()

    def process_output(self):
        if self.process is None:
            return
        data = bytes(self.process.readAllStandardOutput())
        for event in self.event_decoder.feed(self.output_decoder.decode(data)):
            self.handle_session_event(event)

    def handle_session_event(self, event):
        kind = event.get("type")
        if kind == "output":
            text = self.terminal_text.feed(event.get("text", ""))
            self.capture = (self.capture + text)[-1024 * 1024:]
            self.append_output(text)
            # A found result must remain visible even while the backend menu is open.
            if self.operation == "recover" and "found" in self.capture[-512:]:
                complete_lines = self.capture[-8192:].rpartition("\n")[0]
                result = summarize(self.operation, complete_lines, None)
                if result.get("passcode"):
                    self.reported_passcode.setText("Reported passcode: " + result["passcode"])
                    self.reported_passcode.show()
        elif kind == "prompt":
            self.show_prompt(event)
        elif kind == "prompt_done":
            if self.active_prompt and event.get("id") == self.active_prompt.get("id"):
                self.clear_prompt()
        elif kind == "progress" and not self.active_prompt:
            self.guidance_panel.show()
            self.session_hint.setText("Follow the on-screen steps. You can stop this session at any time.")
            self.guidance_title.setText(event.get("title") or "Working on your phone")
            message = event.get("message", "")
            remaining = event.get("remaining")
            if remaining is not None:
                message += f"\n{remaining} seconds remaining"
            self.guidance_text.setText(message)
            self.size_session()

    def process_error(self, error):
        if error == QProcess.ProcessError.FailedToStart:
            self.append_output("Could not start the bundled operation process.\n")
            self.timer_flow.finish(self.operation, 1, apply=self.operation_apply)
            self.set_busy(False, "Could not start the task")
            self.session_hint.setText("Reopen the app and try again. Save the log if this continues.")
            self.guidance_panel.show()
            self.guidance_title.setText("The task could not start")
            self.guidance_text.setText("The bundled operation process did not launch. No device operation was started.")
            self.size_session()
            if self.process:
                self.process.deleteLater()
            self.process = None

    def show_result(self, code):
        result = summarize(self.operation, self.capture, code, interrupted=self.interrupted)
        self.guidance_panel.show()
        self.guidance_title.setText(result.get("title") or ("Session result" if code is not None else "Recovery result"))
        details = result.get("details", [])
        if isinstance(details, str):
            details = [details]
        self.guidance_text.setText("\n\n".join(filter(None, [result.get("message", ""), *details])))
        self.guidance_scroll.verticalScrollBar().setValue(0)
        if result.get("passcode"):
            self.reported_passcode.setText("Reported passcode: " + result["passcode"])
            self.reported_passcode.show()
        self.size_session()

    def process_finished(self, code, _status):
        self.process_output()
        for event in self.event_decoder.feed(self.output_decoder.decode(b"", final=True)):
            self.handle_session_event(event)
        for event in self.event_decoder.finish():
            self.handle_session_event(event)
        if self.operation == "devices":
            if code == 0:
                try:
                    start = self.capture.index("[")
                    devices, _ = json.JSONDecoder().raw_decode(self.capture[start:])
                    self.populate_devices(devices)
                except (ValueError, TypeError, KeyError):
                    self.connection.setText("USB list unavailable")
            else:
                self.connection.setText("USB service unavailable")
                self.device_detail.setText("Open Help to check the USB service and connection requirements.")
        self.append_output(f"\n{'Completed' if code == 0 else 'Stopped'} · exit status {code}\n")
        result = code if not self.interrupted else 130
        self.timer_flow.finish(self.operation, result, apply=self.operation_apply)
        copy = action_copy(self.operation, apply=self.operation_apply, install_only=self.operation_install_only)
        self.set_busy(False, "Task stopped" if self.interrupted else copy.success if code == 0 else copy.failure)
        self.session_hint.setText("Read the result below. Save log includes the full diagnostic session." if self.operation != "devices" else
                                  "Choose a task above. A phone in DFU may not appear in the USB list.")
        if self.operation != "devices" or code != 0:
            self.show_result(code)
        if self.process:
            self.process.deleteLater()
        self.process = None
        self.refresh_backups()

    def populate_devices(self, devices):
        selected = self.settings.udid
        self.device_combo.blockSignals(True)
        self.device_combo.clear()
        if not devices:
            self.device_combo.addItem("No USB phone listed", "")
        elif len(devices) > 1:
            self.device_combo.addItem("Choose a connected phone", "")
        for device in devices:
            udid = device["udid"]
            self.device_combo.addItem("USB phone · …" + udid[-8:], udid)
            self.device_combo.setItemData(self.device_combo.count() - 1, udid, Qt.ItemDataRole.ToolTipRole)
        if selected and self.device_combo.findData(selected) >= 0:
            self.device_combo.setCurrentIndex(self.device_combo.findData(selected))
        self.device_combo.blockSignals(False)
        self.device_changed()
        self.connection.setText(f"{len(devices)} USB phone{'s' if len(devices) != 1 else ''} listed" if devices else "No USB phone listed")
        self.device_detail.setText("Choose the matching model. Inspecting the phone reads its installed iOS and lockout state." if devices else
                                   "A phone in DFU may still be connected. Choose its model above and start a task.")

    def send_input(self):
        if self.process is not None and self.busy and self.manual_input.isChecked():
            try:
                encoded = encode_raw(self.reply.text())
            except ValueError as exc:
                self.session_hint.setText(str(exc))
                return
            self.process.write(encoded)
            self.clear_prompt()
            self.reply.clear()

    def interrupt(self):
        if self.cancel_task is not None:
            self.cancel_task.set()
            self.stop_button.setEnabled(False)
            self.session_status.setText("Stopping at the next safe point…")
            return
        if self.process is not None and self.stop_button.isEnabled():
            if self.operation_apply or self.operation == "prepare":
                answer = QMessageBox.question(
                    self, "Stop this operation?",
                    "Request a graceful interrupt and wait for cleanup? The phone may remain in its ramdisk.",
                    QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
                    QMessageBox.StandardButton.Cancel)
                if answer != QMessageBox.StandardButton.Yes:
                    return
            self.interrupted = True
            self.process.write(encode_interrupt())
            self.clear_prompt()
            self.stop_button.setEnabled(False)
            self.session_status.setText("Stopping — waiting for cleanup…")

    def save_output(self):
        path, _ = QFileDialog.getSaveFileName(self, "Save activity log", "recovery-session.txt", "Text files (*.txt)")
        if path:
            try:
                from .core import secure_write
                secure_write(Path(path), self.session_log.text().encode("utf-8"))
            except OSError as exc:
                QMessageBox.warning(self, "Could not save", str(exc))

    def refresh_backups(self):
        self.backup_rows = backup_inventory(state_directory() / "backups")
        self.backup_table.setRowCount(len(self.backup_rows))
        for row, item in enumerate(self.backup_rows):
            for column, key in enumerate(("name", "model", "version", "status")):
                cell = QTableWidgetItem(item[key])
                cell.setToolTip(item["path"] if key != "status" else item["status"])
                if not item["valid"]:
                    cell.setForeground(QColor("#f4a398"))
                self.backup_table.setItem(row, column, cell)
        self.backup_table.setVisible(bool(self.backup_rows))
        self.backup_note.setText(f"{len(self.backup_rows)} saved backup(s). Select one to restore its original files." if self.backup_rows else
                                 "No backups yet. Your first timer reset will create one automatically.")
        self.update_action_states()

    def choose_backup(self):
        if self.busy:
            return
        path = QFileDialog.getExistingDirectory(self, "Choose transaction folder containing manifest.json")
        if path:
            from .core import load_backup
            try:
                _, manifest, identity, _ = load_backup(path)
            except (ResetError, OSError, ValueError) as exc:
                QMessageBox.warning(self, "Invalid backup", str(exc))
                return
            self.backup_rows.append({"path": path, "name": Path(path).name, "model": identity.model,
                                     "version": identity.version, "status": manifest.get("status", "unknown"), "valid": True})
            row = self.backup_table.rowCount()
            self.backup_table.insertRow(row)
            for column, key in enumerate(("name", "model", "version", "status")):
                self.backup_table.setItem(row, column, QTableWidgetItem(self.backup_rows[-1][key]))
            self.backup_table.show()
            self.backup_table.selectRow(row)
            self.update_action_states()

    def recover_backup(self, action):
        if self.busy:
            return
        row = self.backup_table.currentRow()
        if row < 0 or row >= len(self.backup_rows) or not self.backup_rows[row]["valid"]:
            QMessageBox.information(self, "Choose a valid backup", "Select a verified transaction or choose its folder.")
            return
        self.start_operation(action, apply=True, backup=self.backup_rows[row]["path"])

    def open_backups(self):
        folder = state_directory() / "backups"
        folder.mkdir(parents=True, exist_ok=True, mode=0o700)
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(folder)))

    def run_task(self, function, finished, message):
        if self.busy:
            return
        self.task_outcome = "Finished"
        self.task_hint = "The result is shown on the Updates page."
        self.guidance_panel.hide()
        self.set_busy(True, message)
        self.size_session()
        self.session_hint.setText("You can keep browsing while this completes.")
        self.cancel_task = threading.Event()
        cancellation = self.cancel_task
        def cancellable(progress):
            def checked_progress(text):
                if cancellation.is_set():
                    raise ResetError("Operation cancelled. The active installation is unchanged.")
                progress(text)
            checked_progress(message)
            return function(checked_progress)
        task = Task(cancellable, self)
        self.task = task
        task.progress.connect(self.session_status.setText)
        task.result.connect(finished)
        task.failed.connect(self.task_failed)
        task.finished.connect(self.task_finished)
        task.finished.connect(task.deleteLater)
        self.stop_button.setEnabled(True)
        task.start()

    def task_finished(self):
        self.cancel_task = None
        self.task = None
        self.set_busy(False, self.task_outcome)
        self.session_hint.setText(self.task_hint)

    def task_failed(self, message):
        self.task_outcome = "Update needs attention"
        self.task_hint = "Read the message on Updates or save the activity log."
        self.session_status.setText(self.task_outcome)
        self.session_hint.setText(self.task_hint)
        self.append_output(message + "\n")
        self.update_info.setText(message)
        self.guidance_panel.show()
        self.guidance_title.setText("Update needs attention")
        self.guidance_text.setText(message)
        self.size_session()

    def update_now(self):
        if self.busy or not self.relaunch_button.isHidden():
            return
        token = self.token_edit.text().strip()
        prerelease = self.prereleases.isChecked()

        def installed(result):
            if result:
                self.update_ready(result["version"])
            else:
                self.task_outcome = "Update check finished"
                self.task_hint = f"No newer release is available with these update options. Running version {__version__}."
                self.update_info.setText(self.task_hint)
                self.token_edit.clear()

        self.update_info.setText("Checking for the latest version. Download and verification progress appears below.")
        self.run_task(lambda progress: desktop_updates.install_latest(token, prerelease, progress=progress),
                      installed, "Checking for the latest release")

    def install_downloaded_update(self):
        if self.busy:
            return
        archive, _ = QFileDialog.getOpenFileName(self, "Choose the downloaded desktop update", "", "Desktop release (*.zip)")
        if not archive:
            return
        checksum = archive + ".sha256"
        if not Path(checksum).is_file():
            checksum, _ = QFileDialog.getOpenFileName(
                self, "Choose the matching .zip.sha256 file", str(Path(archive).parent), "Release checksum (*.sha256)")
        if not checksum:
            return
        self.run_task(lambda progress: desktop_updates.install_downloaded(archive, checksum, progress=progress),
                      lambda result: self.update_ready(result["version"]), "Installing downloaded update")

    def update_ready(self, version):
        self.task_outcome = "Update ready to use"
        self.task_hint = f"Restart to use version {version}. This window is still running {__version__}."
        self.update_info.setText(self.task_hint)
        self.relaunch_button.setText(f"Restart and use {version}")
        self.relaunch_button.show()
        self.update_shortcut.setText(f"Restart · v{version}")
        self.token_edit.clear()
        self.update_action_states()

    def rollback_update(self):
        if self.busy:
            return
        try:
            changed = desktop_updates.rollback()
            self.update_info.setText("Previous installation selected. Relaunch the app." if changed else "No desktop update is active.")
            self.relaunch_button.setVisible(changed)
            self.update_action_states()
        except (ResetError, OSError, ValueError) as exc:
            self.task_failed(str(exc))

    def relaunch(self):
        if self.busy:
            return
        # Start the originally opened app; its bootstrap selects the active version.
        origin = Path(os.environ.get("BL_IOS_ORIGINAL_ROOT", Path(__file__).resolve().parent.parent))
        runtime = origin / "runtime" / ("pythonw.exe" if sys.platform == "win32" else "bin/python3")
        executable = runtime if runtime.is_file() else Path(sys.executable)
        script = origin / "desktop.py"
        started = QProcess.startDetached(str(executable), ["-I", "-B", str(script)], str(origin))[0]
        if started:
            self.close()
        else:
            self.update_info.setText("Could not relaunch. Close this window and open the original app.")

    def closeEvent(self, event):
        if self.busy:
            QMessageBox.information(self, "Operation in progress", "Wait for this operation to finish, or use Stop and wait for cleanup before closing.")
            event.ignore()
            return
        self.preferences.setValue("window/geometry", self.saveGeometry())
        self.token_edit.clear()
        event.accept()


def main():
    if "--self-test" in sys.argv:
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    application = QApplication(sys.argv[:1])
    application.setApplicationName(APP_NAME)
    application.setOrganizationName("channelramble")
    application.setStyle("Fusion")
    palette = dark_palette()
    application.setPalette(palette)
    application.setStyleSheet(STYLE)
    application.setFont(QFont("Segoe UI" if sys.platform == "win32" else "Helvetica Neue", 12))
    application.setWindowIcon(QIcon(str(Path(__file__).with_name("app-icon.svg"))))
    window = MainWindow(auto_scan="--self-test" not in sys.argv and "--screenshot" not in sys.argv)
    window.show()
    if "--self-test" in sys.argv:
        import paramiko
        from .dependencies import check_text
        from importlib import resources
        failures = check_text(resources.files("ios_timeout").joinpath("runtime-requirements.txt").read_text())
        if failures:
            raise RuntimeError("; ".join(failures))
        for index in range(window.pages.count()):
            window.navigate(index)
        window.navigate(0)
        print(json.dumps({"desktop": __version__, "python": platform.python_version(),
                          "ssh": paramiko.__version__, "pages": window.pages.count(), "result": "ok"}), flush=True)
        QTimer.singleShot(100, application.quit)
    if "--screenshot" in sys.argv:
        path = sys.argv[sys.argv.index("--screenshot") + 1]
        def capture():
            window.grab().save(path)
            application.quit()
        QTimer.singleShot(500, capture)
    return application.exec()
