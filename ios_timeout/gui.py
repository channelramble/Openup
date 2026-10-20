"""Desktop recovery workspace. Device operations run in isolated terminal workers."""

import codecs
import json
import os
import platform
import sys
import threading
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
from . import desktop_updates

STYLE = """
QWidget { color: #233c47; font-size: 13px; }
QMainWindow, #workspace { background: #f4f6f4; }
#sidebar { background: #132c3a; }
#sidebar #brand { color: #f5faf9; font-size: 23px; font-weight: 700; }
#sidebar QLabel { color: #9cb3bd; }
#sidebar #edition { color: #70d7b9; font-size: 10px; font-weight: 700; letter-spacing: 2px; }
#sidebar QPushButton { background: transparent; color: #becdd2; border: 0; border-radius: 7px;
    padding: 13px 14px; text-align: left; font-weight: 500; }
#sidebar QPushButton:checked { color: #fff; background: #294654; border-left: 3px solid #6bd3b3; }
#sidebar QPushButton:hover { background: #203d4b; }
#pageTitle { color: #173340; font-size: 29px; font-weight: 700; }
#subtitle { color: #697e85; font-size: 13px; }
#card { background: #fff; border: 1px solid #dce5e1; border-radius: 12px; }
#cardTitle { font-size: 16px; font-weight: 650; color: #183d47; }
#eyebrow { color: #51877b; font-size: 10px; font-weight: 700; letter-spacing: 1px; }
#muted { color: #71838a; }
#notice { background: #eaf3ee; border: 1px solid #cee4d8; border-radius: 8px; padding: 12px; color: #3c685a; }
#warning { background: #fcf3e4; border: 1px solid #ecd8b6; border-radius: 8px; padding: 12px; color: #805e25; }
QPushButton { background: #fff; border: 1px solid #ccd9d4; border-radius: 7px; padding: 10px 16px; font-weight: 600; }
QPushButton:hover { background: #edf4f0; border-color: #94b8a7; }
QPushButton:pressed { background: #dfebe4; }
QPushButton:disabled { background: #edf0ee; color: #9ca9a3; border-color: #e0e7e2; }
QPushButton#primary { background: #24745f; color: #fff; border: 1px solid #24745f; }
QPushButton#primary:hover { background: #1b604e; }
QPushButton#primary:disabled { background: #b2c9bd; border-color: #b2c9bd; color: #f3f6f4; }
QPushButton#danger { color: #985144; }
QLineEdit, QComboBox, QSpinBox { background: #fff; border: 1px solid #ccd9d4; border-radius: 6px; padding: 8px; min-height: 20px; }
QLineEdit:focus, QComboBox:focus { border: 1px solid #3a9179; }
QComboBox QAbstractItemView { background: #fff; color: #233c47; selection-background-color: #dbece3; }
QCheckBox { spacing: 8px; padding: 4px 0; }
QTableWidget { background: #fff; border: 1px solid #dce5e1; border-radius: 7px; gridline-color: #eef2ef; selection-background-color: #dceee4; selection-color: #173d30; }
QHeaderView::section { background: #f1f5f2; border: 0; border-bottom: 1px solid #dde6e0; padding: 10px; color: #59746a; font-weight: 600; }
#console { background: #142c38; color: #d5e3e4; border: 0; border-radius: 9px; padding: 10px; font-family: Menlo, Consolas, monospace; font-size: 11px; }
#sessionTitle { font-weight: 650; color: #405f64; }
QProgressBar { background: #dce8e0; border: 0; border-radius: 2px; min-height: 4px; max-height: 4px; }
QProgressBar::chunk { background: #3b9e7f; }
QScrollArea { border: 0; background: transparent; }
QScrollBar:vertical { background: transparent; width: 8px; }
QScrollBar::handle:vertical { background: #c5d3cb; border-radius: 4px; min-height: 30px; }
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical { height: 0; }
QDialog { background: #f4f6f4; }
QToolTip { color: #233c47; background: #fff; border: 1px solid #bbcfc3; padding: 6px; }
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
    layout.setContentsMargins(22, 20, 22, 20)
    layout.setSpacing(13)
    layout.addWidget(label(title, "cardTitle"))
    if description:
        layout.addWidget(label(description, "muted"))
    return frame, layout


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
        self.setWindowTitle(APP_NAME)
        self.setWindowIcon(QIcon(str(Path(__file__).with_name("app-icon.svg"))))
        self.resize(1190, 860)
        self.setMinimumSize(1000, 730)
        self.settings = Settings()
        self.preferences = QSettings("channelramble", "Bruteforce Legacy iOS")
        self.busy = False
        self.operation_buttons = []
        self.task = None
        self.process = None
        self.pending_release = None
        self.output_decoder = codecs.getincrementaldecoder("utf-8")("replace")
        self.terminal_text = TerminalText()
        self.capture = ""
        self.session_log = SessionLog()
        self.backup_rows = []
        self.operation = ""
        self.operation_apply = False
        self.cancel_task = None
        self.build_ui()
        geometry = self.preferences.value("window/geometry")
        if geometry:
            self.restoreGeometry(geometry)
        if os.environ.get("BL_IOS_UPDATE_ERROR"):
            self.append_output(os.environ["BL_IOS_UPDATE_ERROR"] + "\n")
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
        sidebar.setFixedWidth(215)
        nav = QVBoxLayout(sidebar)
        nav.setContentsMargins(22, 32, 20, 22)
        nav.setSpacing(8)
        nav.addWidget(label("Bruteforce\nLegacy iOS", "brand"))
        nav.addSpacing(8)
        nav.addWidget(label("DESKTOP  /  " + __version__, "edition"))
        nav.addSpacing(35)
        self.nav_group = QButtonGroup(self)
        for index, title in enumerate(("Overview", "Timer reset", "Passcode recovery", "Backups", "Updates", "Diagnostics")):
            button = QPushButton(title)
            button.setCheckable(True)
            self.nav_group.addButton(button, index)
            nav.addWidget(button)
        self.nav_group.idToggled.connect(lambda index, checked: self.navigate(index) if checked else None)
        nav.addStretch()
        nav.addWidget(label("LOCAL WORKSPACE", "edition"))
        nav.addWidget(label("Backups stay on this computer.\nDevice work stays under your control."))
        nav.addSpacing(12)
        nav.addWidget(self.button("Documentation", lambda: QDesktopServices.openUrl(
            QUrl("https://github.com/channelramble/Bruteforce-Legacy-iOS#readme")), operation=False))
        layout.addWidget(sidebar)
        workspace = QWidget()
        workspace.setObjectName("workspace")
        work = QVBoxLayout(workspace)
        work.setContentsMargins(30, 26, 30, 20)
        work.setSpacing(15)
        top = QHBoxLayout()
        self.title = label("Your recovery workspace", "pageTitle")
        top.addWidget(self.title, 1)
        self.connection = label("USB not checked", "muted")
        top.addWidget(self.connection)
        work.addLayout(top)
        self.subtitle = label("", "subtitle")
        work.addWidget(self.subtitle)
        split = QSplitter(Qt.Orientation.Vertical)
        self.pages = QStackedWidget()
        for builder in (self.overview_page, self.reset_page, self.recovery_page,
                        self.backups_page, self.updates_page, self.diagnostics_page):
            page = builder()
            scroll = QScrollArea()
            scroll.setWidgetResizable(True)
            scroll.setWidget(page)
            self.pages.addWidget(scroll)
        split.addWidget(self.pages)
        session = QWidget()
        session_layout = QVBoxLayout(session)
        session_layout.setContentsMargins(0, 10, 0, 0)
        session_layout.setSpacing(8)
        session_header = QHBoxLayout()
        self.session_status = label("SESSION  ·  Ready", "sessionTitle")
        session_header.addWidget(self.session_status, 1)
        session_header.addWidget(self.button("Save output…", self.save_output, operation=False))
        session_header.addWidget(self.button("Clear", self.clear_output, operation=False))
        self.stop_button = self.button("Interrupt", self.interrupt, operation=False)
        self.stop_button.setObjectName("danger")
        self.stop_button.setEnabled(False)
        session_header.addWidget(self.stop_button)
        session_layout.addLayout(session_header)
        self.progress = QProgressBar()
        self.progress.setTextVisible(False)
        self.progress.setRange(0, 1)
        self.progress.setValue(0)
        session_layout.addWidget(self.progress)
        self.console = QPlainTextEdit()
        self.console.setObjectName("console")
        self.console.setReadOnly(True)
        self.console.setMaximumBlockCount(2500)
        self.console.setPlaceholderText("Operation output appears here. Nothing is saved unless you choose Save output.")
        self.console.setMinimumHeight(100)
        session_layout.addWidget(self.console, 1)
        input_row = QHBoxLayout()
        self.reply = QLineEdit()
        self.reply.setPlaceholderText("Reply to a backend prompt, then press Enter")
        self.reply.setEchoMode(QLineEdit.EchoMode.Password)
        self.reply.returnPressed.connect(self.send_input)
        self.reply.setEnabled(False)
        input_row.addWidget(self.reply, 1)
        self.hide_reply = QCheckBox("Hide reply")
        self.hide_reply.setChecked(True)
        self.hide_reply.toggled.connect(lambda checked: self.reply.setEchoMode(
            QLineEdit.EchoMode.Password if checked else QLineEdit.EchoMode.Normal))
        input_row.addWidget(self.hide_reply)
        self.send_button = self.button("Send", self.send_input, operation=False)
        self.send_button.setEnabled(False)
        input_row.addWidget(self.send_button)
        session_layout.addLayout(input_row)
        split.addWidget(session)
        split.setStretchFactor(0, 3)
        split.setStretchFactor(1, 2)
        split.setSizes([460, 240])
        work.addWidget(split, 1)
        layout.addWidget(workspace, 1)
        self.setCentralWidget(outer)
        self.navigate(0)

    def page(self):
        widget = QWidget()
        layout = QVBoxLayout(widget)
        layout.setContentsMargins(0, 0, 8, 0)
        layout.setSpacing(16)
        return widget, layout

    def overview_page(self):
        widget, layout = self.page()
        frame, body = card("Connected iPhone", "Connect by USB. Phones in DFU appear after an SSH ramdisk boots.")
        body.addWidget(label("USB DEVICE", "eyebrow"))
        row = QHBoxLayout()
        self.device_combo = QComboBox()
        self.device_combo.addItem("No phone detected", "")
        self.device_combo.currentIndexChanged.connect(self.device_changed)
        row.addWidget(self.device_combo, 1)
        row.addWidget(self.button("Refresh", self.scan))
        body.addLayout(row)
        self.device_detail = label("Choose a workflow below, or check this computer in Diagnostics.", "muted")
        body.addWidget(self.device_detail)
        layout.addWidget(frame)
        row = QHBoxLayout()
        for title, description, button, page in (
            ("Timer reset", "Prepare a ramdisk, review timer-state changes, then apply a backed-up reset.", "Open timer reset", 1),
            ("Passcode recovery", "Run the integrated legacy backend with its interactive prompts in this window.", "Open recovery", 2),
        ):
            box, inner = card(title, description)
            inner.addStretch()
            inner.addWidget(self.button(button, lambda checked=False, index=page: self.navigate(index), operation=False))
            row.addWidget(box, 1)
        layout.addLayout(row)
        layout.addWidget(label("Ready to launch · Python, Qt, and SSH libraries are included in desktop downloads.", "notice"))
        layout.addStretch()
        return widget

    def model_combo(self):
        combo = QComboBox()
        for model, (name, low, high) in SUPPORTED.items():
            combo.addItem(f"{name}  ·  {model}", model)
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
        frame, body = card("1  /  Prepare the phone", "Start here if the phone has not booted an SSH ramdisk.")
        self.reset_model = self.model_combo()
        self.reset_model.currentIndexChanged.connect(self.model_changed)
        body.addWidget(self.reset_model)
        row, self.ipsw_edit = self.file_row("Optional local 3G 3.1.3 / 7E18 IPSW")
        body.addLayout(row)
        body.addWidget(label("For iPhone 3G on iOS 4, the boot ramdisk uses n82ap / 7E18. Installed iOS stays in place.", "muted"))
        actions = QHBoxLayout()
        actions.addWidget(self.button("Prepare and inspect", lambda: self.start_operation("prepare"), primary=True))
        actions.addWidget(self.button("Connection options…", self.options_dialog))
        actions.addStretch()
        body.addLayout(actions)
        layout.addWidget(frame)
        frame, body = card("2  /  Review and reset", "The phone must be in an SSH ramdisk. Inspect first to see the exact proposed changes.")
        actions = QHBoxLayout()
        actions.addWidget(self.button("Inspect timer state", lambda: self.start_operation("inspect")))
        actions.addWidget(self.button("Apply timer reset…", lambda: self.start_operation("reset", apply=True), primary=True))
        actions.addStretch()
        body.addLayout(actions)
        body.addWidget(label("A verified backup is created before changes. After reboot, use the correct passcode.", "muted"))
        layout.addWidget(frame)
        layout.addStretch()
        return widget

    def recovery_page(self):
        widget, layout = self.page()
        frame, body = card("Legacy passcode recovery", "Uses the pinned 32bit-Bruteforce-Passcode backend. Its menus and prompts appear in the session below.")
        self.recovery_model = self.model_combo()
        self.recovery_model.currentIndexChanged.connect(self.recovery_model_changed)
        body.addWidget(self.recovery_model)
        row, self.armv6_edit = self.file_row("Custom ARMv6 tools folder (optional)", directory=True)
        body.addLayout(row)
        self.armv6_notice = label("iPhone 3G ARMv6 tools are included and selected automatically. Recovery boots an iOS 4.2.1 ramdisk without installing or changing the phone's iOS version.", "notice")
        body.addWidget(self.armv6_notice)
        body.addWidget(label("Backend preparation may request system tools or elevated access. Windows uses WSL 2 and USB passthrough.", "muted"))
        actions = QHBoxLayout()
        actions.addWidget(self.button("Download backend", lambda: self.start_operation("bruteforce", install_only=True)))
        actions.addWidget(self.button("Start recovery…", lambda: self.start_operation("bruteforce", apply=True), primary=True))
        body.addLayout(actions)
        layout.addWidget(frame)
        layout.addWidget(label("The backend has its own device workflow. Timer-reset transaction backups apply to the separate Timer reset workflow.", "notice"))
        layout.addStretch()
        return widget

    def backups_page(self):
        widget, layout = self.page()
        frame, body = card("Saved transactions", "Review backups from this computer, restore originals, or finish interrupted cleanup.")
        self.backup_table = QTableWidget(0, 4)
        self.backup_table.setHorizontalHeaderLabels(["Transaction", "Phone", "iOS", "Status"])
        self.backup_table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.backup_table.setSelectionMode(QTableWidget.SelectionMode.SingleSelection)
        self.backup_table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.backup_table.verticalHeader().hide()
        self.backup_table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        self.backup_table.horizontalHeader().setSectionResizeMode(3, QHeaderView.ResizeMode.Stretch)
        self.backup_table.setMinimumHeight(130)
        body.addWidget(self.backup_table)
        actions = QHBoxLayout()
        actions.addWidget(self.button("Refresh", self.refresh_backups))
        actions.addWidget(self.button("Choose folder…", self.choose_backup))
        actions.addWidget(self.button("Restore originals…", lambda: self.recover_backup("restore")))
        actions.addWidget(self.button("Finish cleanup…", lambda: self.recover_backup("finalize")))
        body.addLayout(actions)
        self.backup_note = label("", "muted")
        body.addWidget(self.backup_note)
        layout.addWidget(frame)
        layout.addWidget(self.button("Open backups folder", self.open_backups, operation=False))
        layout.addStretch()
        return widget

    def updates_page(self):
        widget, layout = self.page()
        frame, body = card("Keep the desktop app current", f"Installed version {__version__}. Updates include the full runtime and libraries.")
        self.update_info = label("Check for a new desktop release for this computer.", "notice")
        body.addWidget(self.update_info)
        self.token_edit = QLineEdit()
        self.token_edit.setEchoMode(QLineEdit.EchoMode.Password)
        self.token_edit.setPlaceholderText("GitHub token with read access to the private repository")
        body.addWidget(self.token_edit)
        body.addWidget(label("Your token stays in memory for this session. It is not saved or passed to device operations.", "muted"))
        self.prereleases = QCheckBox("Include prereleases")
        self.prereleases.setChecked(True)
        body.addWidget(self.prereleases)
        actions = QHBoxLayout()
        actions.addWidget(self.button("Check for updates", self.check_updates))
        self.install_button = self.button("Install update", self.install_update, primary=True)
        self.install_button.setEnabled(False)
        actions.addWidget(self.install_button)
        actions.addWidget(self.button("Roll back", self.rollback_update))
        body.addLayout(actions)
        self.relaunch_button = self.button("Relaunch app", self.relaunch, operation=False)
        self.relaunch_button.hide()
        body.addWidget(self.relaunch_button)
        layout.addWidget(frame)
        layout.addWidget(label("Downloads are checksum-verified, staged separately, and activated only after verification. Backups remain in your local workspace.", "muted"))
        layout.addStretch()
        return widget

    def diagnostics_page(self):
        widget, layout = self.page()
        frame, body = card("Computer and connection", f"{platform.system()} · {platform.machine()}   /   Desktop {__version__}")
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
        actions.addWidget(self.button("Run diagnostics", lambda: self.start_operation("doctor"), primary=True))
        actions.addWidget(self.button("Connection options…", self.options_dialog))
        actions.addStretch()
        body.addLayout(actions)
        layout.addWidget(frame)
        layout.addWidget(label("Hardware validation is pending. Physical recovery, timer clearance, and Windows/Linux USB workflows have not been verified on a phone.", "warning"))
        layout.addWidget(label("State and backups: " + str(state_directory()), "muted"))
        layout.addStretch()
        return widget

    def navigate(self, index):
        names = ["Your recovery workspace", "Reset the lockout timer", "Legacy passcode recovery",
                 "Backups & recovery", "Desktop updates", "Diagnostics"]
        descriptions = ["Connect your phone and choose where to start.",
                        "Prepare, inspect, and apply with a recoverable backup.",
                        "A guided session with the integrated recovery backend.",
                        "Keep the original files close at hand.",
                        "Verified downloads. A previous version to return to.",
                        "Check the computer, connection, and platform requirements."]
        self.pages.setCurrentIndex(index)
        self.nav_group.button(index).setChecked(True)
        self.title.setText(names[index])
        self.subtitle.setText(descriptions[index])

    def model_changed(self):
        model = self.reset_model.currentData()
        self.settings.model = model
        self.recovery_model.blockSignals(True)
        self.recovery_model.setCurrentIndex(self.recovery_model.findData(model))
        self.recovery_model.blockSignals(False)
        self.armv6_notice.setVisible(model == "iPhone1,2")
        self.ipsw_edit.setEnabled(model == "iPhone1,2")
        if model != "iPhone1,2":
            self.ipsw_edit.clear()

    def recovery_model_changed(self):
        self.reset_model.setCurrentIndex(self.reset_model.findData(self.recovery_model.currentData()))
        self.model_changed()

    def device_changed(self):
        self.settings.udid = self.device_combo.currentData() or ""

    def options_dialog(self):
        dialog = QDialog(self)
        dialog.setWindowTitle("Connection options")
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

    def set_busy(self, busy, message="Ready"):
        self.busy = busy
        for button in self.operation_buttons:
            button.setEnabled(not busy)
        self.install_button.setEnabled(not busy and self.pending_release is not None
            and desktop_updates.version_tuple(self.pending_release["version"]) > desktop_updates.version_tuple(__version__))
        self.session_status.setText("SESSION  ·  " + message)
        self.progress.setRange(0, 0 if busy else 1)
        self.progress.setValue(0)
        self.token_edit.setEnabled(not busy)
        self.prereleases.setEnabled(not busy)
        if not busy:
            self.stop_button.setEnabled(False)
            self.reply.clear()
            self.reply.setEnabled(False)
            self.send_button.setEnabled(False)

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
        self.connection.setText("Checking USB…")
        self.start_operation("devices")

    def start_operation(self, action, *, apply=False, backup=None, install_only=False):
        if self.busy:
            return
        self.settings.ipsw = self.ipsw_edit.text().strip()
        self.settings.armv6_tools = self.armv6_edit.text().strip()
        try:
            arguments = operation_arguments(action, self.settings, apply=apply, backup=backup, install_only=install_only)
        except (ResetError, ValueError) as exc:
            QMessageBox.information(self, "Choose recovery settings", str(exc))
            return
        if apply:
            description = {
                "reset": "Apply the timer-state reset to the selected phone? The app backs up original files before writing.",
                "restore": "Restore original files from this backup? The connected phone must match the saved identity.",
                "finalize": "Verify this transaction and finish its device-lock cleanup?",
                "bruteforce": "Start the recovery backend? It will guide you through DFU and its own device workflow.",
            }.get(action, "Start this device operation?")
            if QMessageBox.question(self, "Start " + action, description,
                                    QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
                                    QMessageBox.StandardButton.Cancel) != QMessageBox.StandardButton.Yes:
                return
        root = Path(__file__).resolve().parent.parent
        executable = Path(sys.executable)
        if executable.name.lower() == "pythonw.exe":
            executable = executable.with_name("python.exe")
        self.operation = action
        self.operation_apply = apply
        self.capture = ""
        self.terminal_text = TerminalText()
        self.output_decoder = codecs.getincrementaldecoder("utf-8")("replace")
        self.append_output(
            "\n› " + action.replace("_", " ").capitalize() + "\n"
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
        environment.insert("BL_IOS_APP_ROOT", str(root))
        environment.insert("PYTHONNOUSERSITE", "1")
        process.setProcessEnvironment(environment)
        process.setWorkingDirectory(str(root))
        process.setProcessChannelMode(QProcess.ProcessChannelMode.MergedChannels)
        process.readyReadStandardOutput.connect(self.process_output)
        process.finished.connect(self.process_finished)
        process.errorOccurred.connect(self.process_error)
        self.set_busy(True, action.capitalize() + " running")
        self.reply.setEnabled(True)
        self.send_button.setEnabled(True)
        self.stop_button.setEnabled(True)
        process.start()

    def process_output(self):
        if self.process is None:
            return
        data = bytes(self.process.readAllStandardOutput())
        text = self.terminal_text.feed(self.output_decoder.decode(data))
        self.capture = (self.capture + text)[-1024 * 1024:]
        self.append_output(text)

    def process_error(self, error):
        if error == QProcess.ProcessError.FailedToStart:
            self.append_output("Could not start the bundled operation process.\n")
            self.set_busy(False, "Could not start")
            self.process.deleteLater()
            self.process = None

    def process_finished(self, code, _status):
        self.process_output()
        self.append_output(self.terminal_text.feed(self.output_decoder.decode(b"", final=True)))
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
                self.device_detail.setText("Use Diagnostics to check the USB service and platform requirements.")
        self.append_output(f"\n{'Completed' if code == 0 else 'Stopped'} · exit status {code}\n")
        self.set_busy(False, "Completed" if code == 0 else "Needs attention")
        if self.process:
            self.process.deleteLater()
        self.process = None
        self.refresh_backups()

    def populate_devices(self, devices):
        selected = self.settings.udid
        self.device_combo.clear()
        if not devices:
            self.device_combo.addItem("No USB phone detected", "")
        elif len(devices) > 1:
            self.device_combo.addItem("Choose a phone", "")
        for device in devices:
            self.device_combo.addItem(device["udid"], device["udid"])
        if selected and self.device_combo.findData(selected) >= 0:
            self.device_combo.setCurrentIndex(self.device_combo.findData(selected))
        self.connection.setText(f"{len(devices)} USB phone{'s' if len(devices) != 1 else ''} connected" if devices else "No phone connected")
        self.device_detail.setText("Inspect the phone in its ramdisk to read its model and installed iOS." if devices else
                                   "DFU phones are not listed here. Use Timer reset to prepare an SSH ramdisk.")

    def send_input(self):
        if self.process is not None and self.busy:
            self.process.write((self.reply.text() + "\n").encode("utf-8"))
            self.reply.clear()

    def interrupt(self):
        if self.cancel_task is not None:
            self.cancel_task.set()
            self.stop_button.setEnabled(False)
            self.session_status.setText("SESSION  ·  Cancelling at the next safe point")
            return
        if self.process is not None and self.stop_button.isEnabled():
            if self.operation_apply or self.operation == "prepare":
                answer = QMessageBox.question(
                    self, "Interrupt operation?",
                    "Request a graceful interrupt and wait for cleanup? The phone may remain in its ramdisk.",
                    QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
                    QMessageBox.StandardButton.Cancel)
                if answer != QMessageBox.StandardButton.Yes:
                    return
            self.process.write(b"\x03")
            self.stop_button.setEnabled(False)
            self.session_status.setText("SESSION  ·  Waiting for the operation to stop")

    def save_output(self):
        path, _ = QFileDialog.getSaveFileName(self, "Save session output", "recovery-session.txt", "Text files (*.txt)")
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
                    cell.setForeground(QColor("#985144"))
                self.backup_table.setItem(row, column, cell)
        self.backup_note.setText(f"{len(self.backup_rows)} saved transaction(s)." if self.backup_rows else
                                 "No backups yet. A timer reset creates one before it changes files.")

    def choose_backup(self):
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
            self.backup_table.selectRow(row)

    def recover_backup(self, action):
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
        self.set_busy(True, message)
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
        task.progress.connect(lambda text: self.session_status.setText("SESSION  ·  " + text))
        task.result.connect(finished)
        task.failed.connect(self.task_failed)
        task.finished.connect(self.task_finished)
        task.finished.connect(task.deleteLater)
        self.stop_button.setEnabled(True)
        task.start()

    def task_finished(self):
        self.cancel_task = None
        self.task = None
        self.set_busy(False, "Ready")

    def task_failed(self, message):
        self.append_output(message + "\n")
        self.update_info.setText(message)

    def check_updates(self):
        token = self.token_edit.text().strip()
        prerelease = self.prereleases.isChecked()

        def checked(release):
            self.pending_release = release
            if release and desktop_updates.version_tuple(release["version"]) > desktop_updates.version_tuple(__version__):
                self.update_info.setText(f"Version {release['version']} is available for this computer.")
            else:
                self.update_info.setText(f"You're current. Installed desktop version: {__version__}.")

        self.run_task(lambda progress: desktop_updates.check(token, prerelease), checked, "Checking GitHub")

    def install_update(self):
        release = self.pending_release
        if release is None:
            return
        token = self.token_edit.text().strip()

        def installed(_folder):
            self.update_info.setText(f"Version {release['version']} is installed and verified. Relaunch to use it.")
            self.relaunch_button.show()
            self.pending_release = None
            self.token_edit.clear()

        self.run_task(lambda progress: desktop_updates.install(release, token, progress=progress),
                      installed, "Downloading desktop update")

    def rollback_update(self):
        try:
            changed = desktop_updates.rollback()
            self.update_info.setText("Previous installation selected. Relaunch the app." if changed else "No desktop update is active.")
            self.relaunch_button.setVisible(changed)
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
            QMessageBox.information(self, "Operation in progress", "Wait for this operation to finish, or use Interrupt and wait for cleanup before closing.")
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
    palette = QPalette()
    for role, color in (
        (QPalette.ColorRole.Window, "#f4f6f4"), (QPalette.ColorRole.WindowText, "#233c47"),
        (QPalette.ColorRole.Base, "#ffffff"), (QPalette.ColorRole.AlternateBase, "#edf3ef"),
        (QPalette.ColorRole.Text, "#233c47"), (QPalette.ColorRole.Button, "#ffffff"),
        (QPalette.ColorRole.ButtonText, "#233c47"), (QPalette.ColorRole.Highlight, "#24745f"),
        (QPalette.ColorRole.HighlightedText, "#ffffff"), (QPalette.ColorRole.ToolTipBase, "#ffffff"),
        (QPalette.ColorRole.ToolTipText, "#233c47"),
    ):
        palette.setColor(role, QColor(color))
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
