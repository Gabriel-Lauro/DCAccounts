"""
DC Accounts - gerenciador de várias contas do Desktop Commander (Remote Device).

Cada conta guarda o seu próprio device.json em <pasta do app>\\profiles\\<id>\\.
Ao ativar uma conta, o app copia esse arquivo para ~/.desktop-commander-device/device.json
(o local onde o agente `remote` lê a sessão), inicia o agente e, ao parar, copia de volta
os tokens renovados. O ambiente (HOME/USERPROFILE) NÃO é alterado, então a máquina remota
continua vendo o seu usuário real. Só uma conta roda por vez.
"""
import json
import os
import re
import shlex
import shutil
import signal
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path

from PySide6.QtCore import QObject, Qt, QUrl, Signal
from PySide6.QtGui import QAction, QColor, QDesktopServices, QFont, QIcon, QPainter, QPixmap
from PySide6.QtWidgets import (
    QApplication,
    QFileDialog,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QMainWindow,
    QMenu,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QSplitter,
    QSystemTrayIcon,
    QVBoxLayout,
    QWidget,
)

APP_NAME = "DCAccounts"
IS_WIN = sys.platform == "win32"
ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")
URL_RE = re.compile(r"https?://[^\s\"'<>]+")

COLORS = {
    "stopped": "#8a8a8a",
    "starting": "#e0a800",
    "online": "#2e9e44",
    "error": "#d64545",
}
LABELS = {
    "stopped": "Parada",
    "starting": "Conectando...",
    "online": "Online",
    "error": "Erro / encerrada",
}


# ----------------------------------------------------------------------------
# Armazenamento
# ----------------------------------------------------------------------------
def app_root() -> Path:
    """Pasta do app: ao lado do .exe (compilado) ou ao lado do main.py (desenvolvimento)."""
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


ROOT = app_root()
CONFIG_FILE = ROOT / "accounts.json"
PROFILES_DIR = ROOT / "profiles"
ORIGINAL_BACKUP = ROOT / "original_device.json"


def migrate_legacy_data() -> None:
    """Traz os dados da versão antiga (%APPDATA%\\DCAccounts) para a pasta do app, uma única vez."""
    if CONFIG_FILE.exists():
        return
    base = os.environ.get("APPDATA")
    if not base:
        return
    old = Path(base) / APP_NAME
    if not (old / "accounts.json").exists():
        return
    try:
        shutil.copy2(old / "accounts.json", CONFIG_FILE)
        if (old / "profiles").exists():
            shutil.copytree(old / "profiles", PROFILES_DIR, dirs_exist_ok=True)
        if (old / "original_device.json").exists() and not ORIGINAL_BACKUP.exists():
            shutil.copy2(old / "original_device.json", ORIGINAL_BACKUP)
    except Exception:
        pass


def load_config() -> dict:
    try:
        return json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {"accounts": [], "device_command": None}


def save_config(cfg: dict) -> None:
    ROOT.mkdir(parents=True, exist_ok=True)
    CONFIG_FILE.write_text(json.dumps(cfg, indent=2, ensure_ascii=False), encoding="utf-8")


def profile_dir(account_id: str) -> Path:
    d = PROFILES_DIR / account_id
    d.mkdir(parents=True, exist_ok=True)
    return d


def session_dir(account_id: str) -> Path:
    return profile_dir(account_id) / ".desktop-commander-device"


def has_session(account_id: str) -> bool:
    return (session_dir(account_id) / "device.json").exists()


def real_session_file() -> Path:
    """Onde o agente realmente lê/grava a sessão: ~/.desktop-commander-device/device.json"""
    return Path.home() / ".desktop-commander-device" / "device.json"


def install_session(account_id: str) -> None:
    """Coloca a sessão da conta no local que o agente usa (ou limpa, se for conta nova)."""
    real = real_session_file()
    real.parent.mkdir(parents=True, exist_ok=True)
    backup = ORIGINAL_BACKUP
    if real.exists() and not backup.exists():
        shutil.copy2(real, backup)  # backup único da sessão que já existia antes do app
    stored = session_dir(account_id) / "device.json"
    if stored.exists():
        shutil.copy2(stored, real)
    elif real.exists():
        real.unlink()  # conta nova: força o fluxo de login em vez de reutilizar outra conta


def save_session(account_id: str) -> None:
    """Guarda de volta a sessão (tokens renovados) no perfil da conta."""
    real = real_session_file()
    if real.exists():
        dest = session_dir(account_id)
        dest.mkdir(parents=True, exist_ok=True)
        shutil.copy2(real, dest / "device.json")


# ----------------------------------------------------------------------------
# Comando do device
# ----------------------------------------------------------------------------
DEFAULT_COMMAND = "npx -y @wonderwhy-er/desktop-commander@latest remote"


def resolve_command(cmdline: str | None) -> list[str] | None:
    """Converte a linha de comando configurada em argumentos executáveis."""
    tokens = [t.strip('"') for t in shlex.split(cmdline or DEFAULT_COMMAND, posix=False)]
    if not tokens:
        return None

    # Windows: roda o npx pelo node direto (npx-cli.js) em vez do npx.cmd.
    # Assim não existe o prompt "Deseja finalizar o arquivo em lotes (S/N)?" ao dar Ctrl+C.
    if IS_WIN and tokens[0].lower() in ("npx", "npx.cmd"):
        node = shutil.which("node")
        if node:
            cli = Path(node).parent / "node_modules" / "npm" / "bin" / "npx-cli.js"
            if cli.exists():
                return [node, str(cli)] + tokens[1:]

    exe = shutil.which(tokens[0])
    if not exe:
        return None
    if IS_WIN and Path(exe).suffix.lower() in (".cmd", ".bat"):
        return ["cmd.exe", "/c", exe] + tokens[1:]
    return [exe] + tokens[1:]


# ----------------------------------------------------------------------------
# Execução do processo
# ----------------------------------------------------------------------------
def send_ctrl_c(pid: int) -> None:
    """Envia Ctrl+C ao console (oculto) do processo filho no Windows."""
    import ctypes

    k = ctypes.windll.kernel32
    k.FreeConsole()
    if k.AttachConsole(pid):
        k.SetConsoleCtrlHandler(None, True)  # ignora o Ctrl+C neste processo
        k.GenerateConsoleCtrlEvent(0, 0)  # CTRL_C_EVENT
        time.sleep(0.3)
        k.FreeConsole()
        k.SetConsoleCtrlHandler(None, False)


class Runner(QObject):
    line = Signal(str, str)  # account_id, texto
    exited = Signal(str, int)  # account_id, código

    def __init__(self):
        super().__init__()
        self.proc: subprocess.Popen | None = None
        self.account_id: str | None = None
        self.wrapped_in_cmd = False

    def is_running(self) -> bool:
        return self.proc is not None and self.proc.poll() is None

    def start(self, account_id: str, cmd: list[str]) -> None:
        install_session(account_id)
        env = os.environ.copy()  # home REAL: a máquina remota continua vendo o seu usuário
        env["NO_COLOR"] = "1"
        env["FORCE_COLOR"] = "0"

        kwargs: dict = {}
        if IS_WIN:
            si = subprocess.STARTUPINFO()
            si.dwFlags |= subprocess.STARTF_USESHOWWINDOW
            si.wShowWindow = 0  # SW_HIDE
            kwargs["startupinfo"] = si
            # console próprio e oculto: permite mandar Ctrl+C só para este filho
            kwargs["creationflags"] = subprocess.CREATE_NEW_CONSOLE
        else:
            kwargs["start_new_session"] = True

        self.proc = subprocess.Popen(
            cmd,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            env=env,
            cwd=str(Path.home()),
            **kwargs,
        )
        self.account_id = account_id
        self.wrapped_in_cmd = cmd[0].lower().endswith("cmd.exe")
        threading.Thread(target=self._read, args=(self.proc, account_id), daemon=True).start()

    def _read(self, proc: subprocess.Popen, account_id: str) -> None:
        try:
            for raw in iter(proc.stdout.readline, b""):
                text = ANSI_RE.sub("", raw.decode("utf-8", "replace")).rstrip()
                if text:
                    self.line.emit(account_id, text)
        except Exception as e:  # pragma: no cover
            self.line.emit(account_id, f"[erro de leitura: {e}]")
        code = proc.wait()
        try:
            save_session(account_id)
        except Exception as e:  # pragma: no cover
            self.line.emit(account_id, f"[aviso] não consegui salvar a sessão: {e}")
        self.exited.emit(account_id, code)

    def stop(self, blocking: bool = False) -> None:
        proc = self.proc
        if proc is None or proc.poll() is not None:
            return
        t = threading.Thread(target=self._terminate, args=(proc, self.wrapped_in_cmd), daemon=True)
        t.start()
        if blocking:
            t.join(15)

    @staticmethod
    def _terminate(proc: subprocess.Popen, wrapped_in_cmd: bool = False) -> None:
        try:
            if IS_WIN:
                send_ctrl_c(proc.pid)
                if wrapped_in_cmd:  # fallback: responde ao prompt do cmd (S = pt-BR, Y = inglês)
                    try:
                        proc.stdin.write(b"S\r\nY\r\n")
                        proc.stdin.flush()
                    except Exception:
                        pass
            else:
                proc.send_signal(signal.SIGINT)
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            if IS_WIN:
                subprocess.run(
                    ["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                    capture_output=True,
                    creationflags=subprocess.CREATE_NO_WINDOW,
                )
            else:
                proc.kill()
        except Exception:
            pass


# ----------------------------------------------------------------------------
# UI
# ----------------------------------------------------------------------------
def dot_icon(color: str) -> QIcon:
    pm = QPixmap(16, 16)
    pm.fill(Qt.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.Antialiasing)
    p.setBrush(QColor(color))
    p.setPen(Qt.NoPen)
    p.drawEllipse(2, 2, 12, 12)
    p.end()
    return QIcon(pm)


def app_icon(color: str = "#3b6fd4") -> QIcon:
    pm = QPixmap(64, 64)
    pm.fill(Qt.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.Antialiasing)
    p.setBrush(QColor(color))
    p.setPen(Qt.NoPen)
    p.drawEllipse(4, 4, 56, 56)
    p.setPen(QColor("white"))
    f = QFont()
    f.setBold(True)
    f.setPointSize(26)
    p.setFont(f)
    p.drawText(pm.rect(), Qt.AlignCenter, "D")
    p.end()
    return QIcon(pm)


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("DC Accounts - Desktop Commander")
        self.setWindowIcon(app_icon())
        self.resize(820, 520)

        self.cfg = load_config()
        self.status: dict[str, str] = {}
        self.last_url: dict[str, str] = {}
        self.opened_url: set[str] = set()
        self.had_session_at_start: dict[str, bool] = {}
        self.pending: str | None = None
        self.exit_requested = False

        self.runner = Runner()
        self.runner.line.connect(self.on_line)
        self.runner.exited.connect(self.on_exited)

        # --- widgets
        self.list = QListWidget()
        self.list.itemSelectionChanged.connect(self.update_buttons)
        self.list.itemDoubleClicked.connect(lambda _: self.activate_selected())

        self.btn_add = QPushButton("Adicionar conta")
        self.btn_remove = QPushButton("Remover")
        self.btn_rename = QPushButton("Renomear")
        self.btn_activate = QPushButton("Ativar / trocar para esta")
        self.btn_stop = QPushButton("Parar")
        self.btn_link = QPushButton("Abrir link de autorização")
        self.btn_logout = QPushButton("Apagar sessão (logout)")
        self.btn_cmd = QPushButton("Configurar comando do device...")

        self.btn_add.clicked.connect(self.add_account)
        self.btn_remove.clicked.connect(self.remove_account)
        self.btn_rename.clicked.connect(self.rename_account)
        self.btn_activate.clicked.connect(self.activate_selected)
        self.btn_stop.clicked.connect(self.stop_current)
        self.btn_link.clicked.connect(self.open_link)
        self.btn_logout.clicked.connect(self.logout_selected)
        self.btn_cmd.clicked.connect(self.pick_command)

        left = QVBoxLayout()
        left.addWidget(QLabel("Contas"))
        left.addWidget(self.list, 1)
        row1 = QHBoxLayout()
        row1.addWidget(self.btn_add)
        row1.addWidget(self.btn_rename)
        row1.addWidget(self.btn_remove)
        left.addLayout(row1)
        left.addWidget(self.btn_activate)
        row2 = QHBoxLayout()
        row2.addWidget(self.btn_stop)
        row2.addWidget(self.btn_link)
        left.addLayout(row2)
        left.addWidget(self.btn_logout)
        left.addWidget(self.btn_cmd)
        left_w = QWidget()
        left_w.setLayout(left)

        self.log = QPlainTextEdit()
        self.log.setReadOnly(True)
        self.log.setMaximumBlockCount(3000)
        self.log.setFont(QFont("Consolas", 9))
        right = QVBoxLayout()
        right.addWidget(QLabel("Log"))
        right.addWidget(self.log, 1)
        right_w = QWidget()
        right_w.setLayout(right)

        split = QSplitter()
        split.addWidget(left_w)
        split.addWidget(right_w)
        split.setStretchFactor(1, 1)
        self.setCentralWidget(split)

        self.status_label = QLabel("Nenhuma conta ativa")
        self.statusBar().addPermanentWidget(self.status_label)

        # --- tray
        self.tray = QSystemTrayIcon(app_icon(), self)
        self.tray.setToolTip("DC Accounts")
        self.tray_menu = QMenu()
        self.switch_menu = QMenu("Trocar para")
        self.tray_menu.aboutToShow.connect(self.rebuild_tray_menu)
        self.tray.setContextMenu(self.tray_menu)
        self.tray.activated.connect(self.on_tray_activated)
        self.tray.show()
        self.warned_tray = False

        self.refresh_list()
        self.update_buttons()
        self.rebuild_tray_menu()

    # ------------------------------------------------------------------ helpers
    def account(self, account_id: str) -> dict | None:
        for a in self.cfg["accounts"]:
            if a["id"] == account_id:
                return a
        return None

    def selected_id(self) -> str | None:
        it = self.list.currentItem()
        return it.data(Qt.UserRole) if it else None

    def name_of(self, account_id: str) -> str:
        a = self.account(account_id)
        return a["name"] if a else account_id[:8]

    def set_status(self, account_id: str, st: str) -> None:
        self.status[account_id] = st
        self.refresh_list()
        self.update_header()
        self.update_buttons()

    def update_header(self) -> None:
        if self.runner.is_running() and self.runner.account_id:
            st = self.status.get(self.runner.account_id, "starting")
            self.status_label.setText(f"Ativa: {self.name_of(self.runner.account_id)} ({LABELS[st]})")
            self.tray.setToolTip(f"DC Accounts - {self.name_of(self.runner.account_id)}")
            self.tray.setIcon(app_icon(COLORS[st]))
        else:
            self.status_label.setText("Nenhuma conta ativa")
            self.tray.setToolTip("DC Accounts - nenhuma conta ativa")
            self.tray.setIcon(app_icon("#8a8a8a"))

    def refresh_list(self) -> None:
        current = self.selected_id()
        self.list.blockSignals(True)
        self.list.clear()
        for a in self.cfg["accounts"]:
            st = self.status.get(a["id"], "stopped")
            logged = "sessão salva" if has_session(a["id"]) else "sem login"
            item = QListWidgetItem(dot_icon(COLORS[st]), f"{a['name']}  -  {LABELS[st]}  ({logged})")
            item.setData(Qt.UserRole, a["id"])
            self.list.addItem(item)
            if a["id"] == current:
                self.list.setCurrentItem(item)
        self.list.blockSignals(False)

    def update_buttons(self) -> None:
        sel = self.selected_id()
        running = self.runner.is_running()
        self.btn_remove.setEnabled(bool(sel))
        self.btn_rename.setEnabled(bool(sel))
        self.btn_activate.setEnabled(bool(sel) and not (running and self.runner.account_id == sel))
        self.btn_stop.setEnabled(running)
        self.btn_link.setEnabled(bool(sel) and sel in self.last_url)
        self.btn_logout.setEnabled(bool(sel))

    def log_line(self, text: str) -> None:
        self.log.appendPlainText(text)

    # ------------------------------------------------------------------ contas
    def add_account(self) -> None:
        name, ok = QInputDialog.getText(self, "Nova conta", "Nome da conta:")
        name = name.strip()
        if not ok or not name:
            return
        acc = {"id": uuid.uuid4().hex[:12], "name": name}
        self.cfg["accounts"].append(acc)
        save_config(self.cfg)
        profile_dir(acc["id"])
        self.refresh_list()
        r = QMessageBox.question(
            self,
            "Login",
            f"Conta '{name}' criada.\nIniciar agora para fazer o login (abre o link de autorização)?",
        )
        if r == QMessageBox.Yes:
            self.activate(acc["id"])

    def rename_account(self) -> None:
        sel = self.selected_id()
        if not sel:
            return
        name, ok = QInputDialog.getText(self, "Renomear", "Novo nome:", text=self.name_of(sel))
        if ok and name.strip():
            self.account(sel)["name"] = name.strip()
            save_config(self.cfg)
            self.refresh_list()

    def remove_account(self) -> None:
        sel = self.selected_id()
        if not sel:
            return
        r = QMessageBox.question(
            self,
            "Remover conta",
            f"Remover '{self.name_of(sel)}' e apagar a sessão salva dela?",
        )
        if r != QMessageBox.Yes:
            return
        if self.runner.is_running() and self.runner.account_id == sel:
            self.runner.stop(blocking=True)
        self.cfg["accounts"] = [a for a in self.cfg["accounts"] if a["id"] != sel]
        save_config(self.cfg)
        shutil.rmtree(PROFILES_DIR / sel, ignore_errors=True)
        self.status.pop(sel, None)
        self.last_url.pop(sel, None)
        self.refresh_list()
        self.update_buttons()

    def logout_selected(self) -> None:
        sel = self.selected_id()
        if not sel:
            return
        if self.runner.is_running() and self.runner.account_id == sel:
            QMessageBox.information(self, "Logout", "Pare a conta antes de apagar a sessão.")
            return
        d = session_dir(sel)
        if d.exists():
            shutil.rmtree(d, ignore_errors=True)
            self.log_line(f"[{self.name_of(sel)}] sessão apagada. Será pedido novo login.")
        self.refresh_list()

    # ------------------------------------------------------------------ execução
    def activate_selected(self) -> None:
        sel = self.selected_id()
        if sel:
            self.activate(sel)

    def activate(self, account_id: str) -> None:
        if self.runner.is_running():
            if self.runner.account_id == account_id:
                return
            self.pending = account_id
            self.log_line(f"--- trocando: parando '{self.name_of(self.runner.account_id)}' ---")
            self.runner.stop()
            return
        self.start_account(account_id)

    def start_account(self, account_id: str) -> None:
        cmd = resolve_command(self.cfg.get("device_command"))
        if not cmd:
            QMessageBox.warning(
                self,
                "Device não encontrado",
                "Não achei o comando configurado (por padrão, o 'npx').\n\n"
                "Instale o Node.js (https://nodejs.org), reabra o app e tente de novo.\n"
                "Teste no terminal:\n    npx -y @wonderwhy-er/desktop-commander@latest remote\n\n"
                "Se usar outro comando, ajuste em 'Configurar comando do device...'.",
            )
            return
        self.had_session_at_start[account_id] = has_session(account_id)
        self.opened_url.discard(account_id)
        self.last_url.pop(account_id, None)
        self.log_line(f"--- iniciando '{self.name_of(account_id)}' ---")
        try:
            self.runner.start(account_id, cmd)
        except Exception as e:
            self.set_status(account_id, "error")
            self.log_line(f"[erro ao iniciar] {e}")
            return
        self.set_status(account_id, "starting")

    def stop_current(self) -> None:
        self.pending = None
        self.runner.stop()

    def on_line(self, account_id: str, text: str) -> None:
        self.log_line(f"[{self.name_of(account_id)}] {text}")
        m = URL_RE.search(text)
        if m:
            url = m.group(0).rstrip(".,);]")
            self.last_url[account_id] = url
            # primeiro login: abre o navegador automaticamente
            if not self.had_session_at_start.get(account_id) and account_id not in self.opened_url:
                self.opened_url.add(account_id)
                QDesktopServices.openUrl(QUrl(url))
            self.update_buttons()
        low = text.lower()
        if self.status.get(account_id) == "starting" and any(
            k in low for k in ("connected", "online", "ready", "listening", "authenticated")
        ):
            if not any(bad in low for bad in ("disconnect", "fail", "error", "not connected")):
                self.set_status(account_id, "online")
        self.refresh_list()

    def on_exited(self, account_id: str, code: int) -> None:
        was_current = self.runner.account_id == account_id
        if was_current:
            self.runner.proc = None
        requested = self.pending is not None or self.exit_requested or code in (0, -2, 130, 0xC000013A)
        self.set_status(account_id, "stopped" if requested else "error")
        self.log_line(f"--- '{self.name_of(account_id)}' encerrada (código {code}) ---")
        if self.pending:
            nxt, self.pending = self.pending, None
            self.start_account(nxt)
        self.update_header()
        self.update_buttons()

    def open_link(self) -> None:
        sel = self.selected_id()
        if sel and sel in self.last_url:
            QDesktopServices.openUrl(QUrl(self.last_url[sel]))

    def pick_command(self) -> None:
        text, ok = QInputDialog.getText(
            self,
            "Comando do device",
            "Linha de comando usada para iniciar o agente remoto\n"
            "(se este comando aceitar uma flag de sessão persistente, adicione ao final):",
            text=self.cfg.get("device_command") or DEFAULT_COMMAND,
        )
        if ok and text.strip():
            self.cfg["device_command"] = text.strip()
            save_config(self.cfg)
            self.log_line(f"[config] comando = {text.strip()}")

    # ------------------------------------------------------------------ tray
    def rebuild_tray_menu(self) -> None:
        self.tray_menu.clear()
        act_open = QAction("Abrir janela", self.tray_menu)
        act_open.triggered.connect(self.show_window)
        self.tray_menu.addAction(act_open)

        self.tray_menu.addSeparator()
        for a in self.cfg["accounts"]:
            running_this = self.runner.is_running() and self.runner.account_id == a["id"]
            act = QAction(("● " if running_this else "    ") + a["name"], self.tray_menu)
            act.triggered.connect(lambda _=False, i=a["id"]: self.activate(i))
            self.tray_menu.addAction(act)
        self.tray_menu.addSeparator()

        act_stop = QAction("Parar conta atual", self.tray_menu)
        act_stop.setEnabled(self.runner.is_running())
        act_stop.triggered.connect(self.stop_current)
        self.tray_menu.addAction(act_stop)

        act_quit = QAction("Sair", self.tray_menu)
        act_quit.triggered.connect(self.quit_app)
        self.tray_menu.addAction(act_quit)

    def on_tray_activated(self, reason) -> None:
        if reason == QSystemTrayIcon.Trigger:
            self.show_window()

    def show_window(self) -> None:
        self.showNormal()
        self.raise_()
        self.activateWindow()

    def closeEvent(self, event) -> None:  # fecha para a bandeja
        event.ignore()
        self.hide()
        if not self.warned_tray:
            self.warned_tray = True
            self.tray.showMessage(
                "DC Accounts",
                "Continua rodando na bandeja. Use 'Sair' no menu para encerrar.",
                QSystemTrayIcon.Information,
                4000,
            )

    def quit_app(self) -> None:
        self.exit_requested = True
        self.pending = None
        self.runner.stop(blocking=True)
        self.tray.hide()
        QApplication.quit()


def make_icon_file(path: str) -> None:
    """Gera o .ico com o MESMO desenho do ícone da bandeja (usado pelo build.bat)."""
    pm = QPixmap(256, 256)
    pm.fill(Qt.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.Antialiasing)
    p.setBrush(QColor("#3b6fd4"))
    p.setPen(Qt.NoPen)
    p.drawEllipse(16, 16, 224, 224)
    p.setPen(QColor("white"))
    f = QFont()
    f.setBold(True)
    f.setPixelSize(104)
    p.setFont(f)
    p.drawText(pm.rect(), Qt.AlignCenter, "D")
    p.end()
    pm.save(path, "ICO")


def main() -> int:
    app = QApplication(sys.argv)
    if len(sys.argv) >= 3 and sys.argv[1] == "--make-icon":
        make_icon_file(sys.argv[2])
        return 0
    migrate_legacy_data()
    app.setQuitOnLastWindowClosed(False)
    app.setApplicationName(APP_NAME)
    if not QSystemTrayIcon.isSystemTrayAvailable():
        QMessageBox.warning(None, APP_NAME, "Bandeja do sistema indisponível; usando só a janela.")
    win = MainWindow()
    win.show()
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
