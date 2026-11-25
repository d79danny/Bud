import subprocess
import sys
from pathlib import Path

import httpx
from PySide6.QtCore import QSettings, QTimer, QUrl
from PySide6.QtGui import QAction, QDesktopServices, QIcon, QPalette, QColor
from PySide6.QtWebEngineCore import QWebEnginePage, QWebEnginePermission, QWebEngineProfile
from PySide6.QtWebEngineWidgets import QWebEngineView
from PySide6.QtWidgets import QApplication, QFileDialog, QMainWindow, QMessageBox

from .config import Config


class BudPage(QWebEnginePage):
    def __init__(self, profile, port, parent):
        super().__init__(profile, parent)
        self.port = port
        self.permissionRequested.connect(self.permission)
        self.newWindowRequested.connect(self.open_window)

    def same_origin(self, url):
        return url.scheme() == "http" and url.host() == "127.0.0.1" and url.port() == self.port

    def acceptNavigationRequest(self, url, kind, _main):  # Qt overrides this name.
        if self.same_origin(url) or url.scheme() in {"about", "blob"}:
            return True
        if kind == self.NavigationType.NavigationTypeLinkClicked:
            if url.scheme() in {"http", "https", "otpauth"}:
                QDesktopServices.openUrl(url)
        return False

    def permission(self, request):
        if self.same_origin(request.origin()) and request.permissionType() in {
            QWebEnginePermission.PermissionType.MediaAudioCapture,
            QWebEnginePermission.PermissionType.Notifications,
        }:
            request.grant()
        else:
            request.deny()

    def open_window(self, request):
        url = request.requestedUrl()
        if self.same_origin(url):
            request.openIn(self)
        elif request.isUserInitiated() and url.scheme() in {"http", "https", "otpauth"}:
            QDesktopServices.openUrl(url)


class Window(QMainWindow):
    def __init__(self, config, port):
        super().__init__()
        self.setWindowTitle("BudAI")
        self.resize(1180, 760)
        self.settings = QSettings("BudAI", "Desktop")
        saved = self.settings.value("geometry")
        if saved is not None:
            self.restoreGeometry(saved)
        self.port = port
        self.owned = None
        self.attempt = 0
        self.start_failed = False
        self.url = f"http://127.0.0.1:{port}"
        self.setWindowIcon(QIcon(str(Path(__file__).with_name("web") / "budai-192.png")))
        store = config.data / "qt"
        store.mkdir(parents=True, mode=0o700, exist_ok=True)
        self.profile = QWebEngineProfile("BudAI", self)
        self.profile.setPersistentStoragePath(str(store / "storage"))
        self.profile.setCachePath(str(store / "cache"))
        self.profile.downloadRequested.connect(self.download)
        self.view = QWebEngineView(self)
        self.view.setPage(BudPage(self.profile, port, self.view))
        self.view.page().setBackgroundColor(QColor("#151515"))
        self.setCentralWidget(self.view)
        fullscreen = QAction("Toggle fullscreen", self, shortcut="F11")
        fullscreen.triggered.connect(lambda: self.showNormal() if self.isFullScreen() else self.showFullScreen())
        self.addAction(fullscreen)
        self.timer = QTimer(self)
        self.timer.setInterval(250)
        self.timer.timeout.connect(self.wait_for_api)
        self.timer.start()

    def wait_for_api(self):
        try:
            with httpx.Client(timeout=0.4, trust_env=False) as client:
                result = client.get(self.url + "/api/health")
            if result.is_success and result.json().get("application") == "BudAI":
                self.timer.stop()
                self.view.setUrl(QUrl(self.url))
                return
            raise RuntimeError(f"Port {self.port} is in use by another service")
        except httpx.TransportError:
            if self.owned is None:
                self.owned = subprocess.Popen([sys.executable, "-m", "budai", "serve", "--port", str(self.port)])
            if self.owned.poll() is not None:
                self.failure(f"The BudAI API exited with status {self.owned.returncode}. Start it from a terminal to inspect the error.")
        except (RuntimeError, ValueError) as error:
            self.failure(str(error))
        self.attempt += 1
        if self.attempt > 60:
            self.failure("BudAI did not report readiness within the startup deadline")

    def failure(self, detail):
        if self.start_failed:
            return
        self.start_failed = True
        self.timer.stop()
        QMessageBox.critical(self, "BudAI startup", detail)
        self.close()

    def download(self, request):
        name, _ = QFileDialog.getSaveFileName(self, "Save BudAI file", request.suggestedFileName())
        if name:
            target = Path(name)
            request.setDownloadDirectory(str(target.parent))
            request.setDownloadFileName(target.name)
            request.accept()
        else:
            request.cancel()

    def closeEvent(self, event):
        self.settings.setValue("geometry", self.saveGeometry())
        self.timer.stop()
        if self.owned and self.owned.poll() is None:
            self.owned.terminate()
            try:
                self.owned.wait(timeout=8)
            except subprocess.TimeoutExpired:
                self.owned.kill()
                self.owned.wait()
        self.view.deleteLater()
        event.accept()


def run(config: Config, port):
    app = QApplication([sys.argv[0]])
    app.setApplicationName("BudAI")
    palette = QPalette()
    palette.setColor(QPalette.ColorRole.Window, QColor("#151515"))
    palette.setColor(QPalette.ColorRole.WindowText, QColor("#e6e6e6"))
    app.setPalette(palette)
    window = Window(config, port)
    window.show()
    return app.exec()
