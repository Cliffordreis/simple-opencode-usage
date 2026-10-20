#!/usr/bin/env python3
"""simple-opencode-usage — cartão flutuante com o uso do OpenCode Go no Linux.

Mostra os medidores Rolling, Weekly e Monthly em uma janela sempre visível,
com ícone na bandeja (StatusNotifierItem + DBusMenu), configurações de
tamanho, transparência, intervalo de atualização e início automático.

A chave de API nunca é gravada no projeto: ela é lida, em ordem, de
OPENCODE_GO_API_KEY / OPENCODE_API_KEY, do config local
(~/.config/simple-opencode-usage/config.json) ou do banco de credenciais do
OpenCode (~/.local/share/opencode/opencode.db).

Licença: MIT.
"""
from __future__ import annotations

import argparse
import json
import os
import queue
import signal
import sqlite3
import sys
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional

import tkinter as tk
import tkinter.font as tkfont

APP_ID = "simple-opencode-usage"
APP_NAME = "OpenCode Go Usage"
VERSION = "1.0.0"

USAGE_URL = "https://opencode.ai/inference/go/v1/usage"
CONSOLE_URL = "https://opencode.ai/console/go"
USER_AGENT = f"{APP_ID}/{VERSION}"

XDG_CONFIG = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config")
XDG_DATA = Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local" / "share")
XDG_RUNTIME = Path(os.environ.get("XDG_RUNTIME_DIR") or "/tmp")

CONFIG_DIR = XDG_CONFIG / APP_ID
CONFIG_PATH = CONFIG_DIR / "config.json"
LEGACY_CONFIG_PATH = XDG_CONFIG / "opencode-usage" / "config.json"
AUTOSTART_PATH = XDG_CONFIG / "autostart" / f"{APP_ID}.desktop"
OPENCODE_DB = XDG_DATA / "opencode" / "opencode.db"
LOCK_PATH = XDG_RUNTIME / f"{APP_ID}-{os.getuid()}.lock"

BASE_W, BASE_H = 300, 192
COMPACT_W, COMPACT_H = 190, 38

C = {
    "bg": "#15181e",
    "border": "#2a303a",
    "bar_bg": "#262c36",
    "fg": "#e8ecf1",
    "muted": "#aab3c0",
    "dim": "#7c8695",
    "green": "#4ade80",
    "amber": "#fbbf24",
    "red": "#f87171",
    "accent": "#38bdf8",
}

DEFAULTS: dict[str, Any] = {
    "scale": 1.0,
    "opacity": 0.96,
    "interval": 30,
    "position": None,
    "api_key": None,
    "start_minimized": False,
    "window_type": "dock",
}

METERS = (
    ("rolling", "Rolling usage"),
    ("weekly", "Weekly usage"),
    ("monthly", "Monthly usage"),
)


# --------------------------------------------------------------------------
# Config
# --------------------------------------------------------------------------
class Config:
    def __init__(self) -> None:
        self.data: dict[str, Any] = dict(DEFAULTS)
        self.load()

    def load(self) -> None:
        for path in (LEGACY_CONFIG_PATH, CONFIG_PATH):
            try:
                raw = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            if isinstance(raw, dict):
                for key, value in raw.items():
                    if key in DEFAULTS or key == "api_key":
                        self.data[key] = value
        self.data["scale"] = _clamp(_num(self.data.get("scale"), 1.0), 0.8, 1.6)
        self.data["opacity"] = _clamp(_num(self.data.get("opacity"), 0.96), 0.3, 1.0)
        self.data["interval"] = int(_clamp(_num(self.data.get("interval"), 30), 10, 300))

    def save(self) -> None:
        payload = dict(self.data)
        if not payload.get("api_key"):
            payload.pop("api_key", None)
        try:
            CONFIG_DIR.mkdir(parents=True, exist_ok=True)
            tmp = CONFIG_PATH.with_suffix(".tmp")
            tmp.write_text(json.dumps(payload, indent=2), encoding="utf-8")
            tmp.replace(CONFIG_PATH)
        except OSError:
            pass


def _num(value: Any, default: float) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


# --------------------------------------------------------------------------
# API key resolution (nunca gravada no projeto)
# --------------------------------------------------------------------------
def key_from_env() -> tuple[Optional[str], str]:
    for name in ("OPENCODE_GO_API_KEY", "OPENCODE_API_KEY"):
        value = (os.environ.get(name) or "").strip()
        if value:
            return value, f"ambiente · {name}"
    return None, ""


def key_from_config(cfg: Config) -> tuple[Optional[str], str]:
    value = str(cfg.data.get("api_key") or "").strip()
    if value:
        return value, "configuração local"
    return None, ""


def key_from_db() -> tuple[Optional[str], str]:
    path = OPENCODE_DB
    if not path.is_file():
        return None, ""
    try:
        con = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=2.0)
        try:
            cur = con.execute(
                "SELECT * FROM credential WHERE integration_id = 'opencode-go' LIMIT 1"
            )
            row = cur.fetchone()
            columns = [d[0] for d in cur.description or []]
        finally:
            con.close()
    except sqlite3.Error:
        return None, ""
    if not row:
        return None, ""
    record = dict(zip(columns, row))
    for column in ("value", "data", "credential", "secret", "api_key", "token"):
        raw = record.get(column)
        if not isinstance(raw, str) or not raw.strip():
            continue
        text = raw.strip()
        if text.startswith("{"):
            try:
                parsed = json.loads(text)
            except ValueError:
                parsed = None
            if isinstance(parsed, dict):
                for key, value in parsed.items():
                    if isinstance(value, str) and "key" in key.lower() and value.strip():
                        return value.strip(), "credencial do OpenCode"
                for value in parsed.values():
                    if isinstance(value, str) and value.startswith("oc_"):
                        return value.strip(), "credencial do OpenCode"
        if text.startswith("oc_"):
            return text, "credencial do OpenCode"
    return None, ""


def resolve_api_key(cfg: Config) -> tuple[Optional[str], str]:
    for finder in (lambda c: key_from_env(), key_from_config, lambda c: key_from_db()):
        key, source = finder(cfg)
        if key:
            return key, source
    return None, "não encontrada"


# --------------------------------------------------------------------------
# Uso: rede e formatação
# --------------------------------------------------------------------------
def fetch_usage(api_key: str) -> dict[str, Any]:
    request = urllib.request.Request(
        USAGE_URL,
        headers={
            "Authorization": f"Bearer {api_key}",
            "User-Agent": USER_AGENT,
            "Accept": "application/json",
        },
    )
    with urllib.request.urlopen(request, timeout=15) as response:
        payload = json.load(response)
    usage = payload.get("usage") or {}
    result: dict[str, Any] = {}
    for name in ("rolling", "weekly", "monthly"):
        item = usage.get(name) or {}
        percent = item.get("percent")
        result[name] = {
            "percent": int(round(float(percent))) if percent is not None else None,
            "resets": _parse_iso(item.get("resetsAt")),
        }
    return result


def _parse_iso(text: Any) -> Optional[datetime]:
    if not isinstance(text, str) or not text:
        return None
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None


def fmt_reset(moment: Optional[datetime]) -> str:
    if moment is None:
        return "—"
    seconds = int((moment - datetime.now(timezone.utc)).total_seconds())
    if seconds <= 0:
        return "agora"
    days, rem = divmod(seconds, 86400)
    hours, rem = divmod(rem, 3600)
    minutes = rem // 60
    if days:
        return f"{days}d {hours}h"
    if hours:
        return f"{hours}h {minutes:02d}min"
    return f"{minutes}min"


def bar_color(percent: Optional[int]) -> str:
    if percent is None:
        return C["dim"]
    if percent >= 85:
        return C["red"]
    if percent >= 60:
        return C["amber"]
    return C["green"]


# --------------------------------------------------------------------------
# Ícone do aplicativo (tema do usuário, para rodar direto do código)
# --------------------------------------------------------------------------
def asset_path(name: str) -> Optional[Path]:
    here = Path(__file__).resolve().parent
    candidates = [
        here / "assets" / name,
        Path("/usr/share") / APP_ID / name,
        XDG_DATA / APP_ID / name,
        Path("/usr/share/icons/hicolor/64x64/apps") / f"{APP_ID}.png",
    ]
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    return None


def ensure_user_icon() -> None:
    """Copia o ícone para o tema do usuário (necessário quando roda do código-fonte)."""
    for size, source in (
        (64, asset_path(f"{APP_ID}-64.png")),
        (48, asset_path(f"{APP_ID}-48.png")),
    ):
        if source is None:
            continue
        target = XDG_DATA / "icons" / "hicolor" / f"{size}x{size}" / "apps" / f"{APP_ID}.png"
        try:
            if target.is_file() and target.read_bytes() == source.read_bytes():
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(source.read_bytes())
        except OSError:
            pass


# --------------------------------------------------------------------------
# Autostart
# --------------------------------------------------------------------------
def launch_parts() -> list[str]:
    argv0 = Path(sys.argv[0]).resolve()
    if argv0.name == APP_ID and os.access(argv0, os.X_OK):
        return [str(argv0)]
    return [sys.executable, str(Path(__file__).resolve())]


def _quote_exec_arg(part: str) -> str:
    """Aspas do campo Exec do .desktop (não é shell)."""
    if not any(ch in part for ch in " \t\n\"'\\><~|&;$*?#()`"):
        return part
    escaped = (
        part.replace("\\", "\\\\")
        .replace('"', '\\"')
        .replace("`", "\\`")
        .replace("$", "\\$")
    )
    return f'"{escaped}"'


def desktop_exec(minimized: bool) -> str:
    parts = launch_parts() + (["--minimized"] if minimized else [])
    return " ".join(_quote_exec_arg(part) for part in parts)


def autostart_enabled() -> bool:
    return AUTOSTART_PATH.is_file()


def set_autostart(enabled: bool, minimized: bool = False) -> bool:
    try:
        if enabled:
            AUTOSTART_PATH.parent.mkdir(parents=True, exist_ok=True)
            AUTOSTART_PATH.write_text(
                "[Desktop Entry]\n"
                "Type=Application\n"
                f"Name={APP_NAME}\n"
                "Comment=Medidores de uso do OpenCode Go\n"
                f"Exec={desktop_exec(minimized)}\n"
                f"Icon={APP_ID}\n"
                "Terminal=false\n"
                "X-GNOME-Autostart-enabled=true\n",
                encoding="utf-8",
            )
        elif AUTOSTART_PATH.is_file():
            AUTOSTART_PATH.unlink()
    except OSError:
        return False
    return autostart_enabled() == enabled


# --------------------------------------------------------------------------
# Bandeja: StatusNotifierItem + DBusMenu (thread própria com GLib)
# --------------------------------------------------------------------------
SNI_XML = """
<node>
  <interface name="org.kde.StatusNotifierItem">
    <method name="Activate"><arg name="x" type="i" direction="in"/><arg name="y" type="i" direction="in"/></method>
    <method name="SecondaryActivate"><arg name="x" type="i" direction="in"/><arg name="y" type="i" direction="in"/></method>
    <method name="ContextMenu"><arg name="x" type="i" direction="in"/><arg name="y" type="i" direction="in"/></method>
    <property name="Category" type="s" access="read"/>
    <property name="Id" type="s" access="read"/>
    <property name="Title" type="s" access="read"/>
    <property name="Status" type="s" access="read"/>
    <property name="IconName" type="s" access="read"/>
    <property name="IconThemePath" type="as" access="read"/>
    <property name="Menu" type="o" access="read"/>
    <property name="ItemIsMenu" type="b" access="read"/>
    <signal name="NewIcon"/>
    <signal name="NewStatus"><arg name="status" type="s"/></signal>
    <signal name="NewTitle"/>
  </interface>
  <interface name="org.freedesktop.DBus.Properties">
    <method name="Get"><arg name="interface_name" type="s" direction="in"/><arg name="property_name" type="s" direction="in"/><arg name="value" type="v" direction="out"/></method>
    <method name="GetAll"><arg name="interface_name" type="s" direction="in"/><arg name="properties" type="a{sv}" direction="out"/></method>
    <method name="Set"><arg name="interface_name" type="s" direction="in"/><arg name="property_name" type="s" direction="in"/><arg name="value" type="v" direction="in"/></method>
    <signal name="PropertiesChanged"><arg name="interface_name" type="s"/><arg name="changed_properties" type="a{sv}"/><arg name="invalidated_properties" type="as"/></signal>
  </interface>
</node>
"""

MENU_XML = """
<node>
  <interface name="com.canonical.dbusmenu">
    <method name="GetLayout">
      <arg name="parentId" type="i" direction="in"/>
      <arg name="recursionDepth" type="i" direction="in"/>
      <arg name="propertyNames" type="as" direction="in"/>
      <arg name="revision" type="u" direction="out"/>
      <arg name="layout" type="(ia{sv}av)" direction="out"/>
    </method>
    <method name="GetGroupProperties">
      <arg name="ids" type="ai" direction="in"/>
      <arg name="propertyNames" type="as" direction="in"/>
      <arg name="properties" type="a(ia{sv})" direction="out"/>
    </method>
    <method name="GetProperty">
      <arg name="id" type="i" direction="in"/>
      <arg name="name" type="s" direction="in"/>
      <arg name="value" type="v" direction="out"/>
    </method>
    <method name="Event">
      <arg name="id" type="i" direction="in"/>
      <arg name="eventId" type="s" direction="in"/>
      <arg name="data" type="v" direction="in"/>
      <arg name="timestamp" type="u" direction="in"/>
    </method>
    <method name="AboutToShow">
      <arg name="id" type="i" direction="in"/>
      <arg name="needUpdate" type="b" direction="out"/>
    </method>
    <method name="AboutToShowGroup">
      <arg name="ids" type="ai" direction="in"/>
      <arg name="updatesNeeded" type="ai" direction="out"/>
      <arg name="idErrors" type="ai" direction="out"/>
    </method>
    <signal name="LayoutUpdated"><arg name="revision" type="u"/><arg name="parent" type="i"/></signal>
  </interface>
</node>
"""

MENU_TOGGLE = 1
MENU_SETTINGS = 2
MENU_REFRESH = 3
MENU_SEPARATOR = 4
MENU_QUIT = 5


class TrayService:
    """Expõe o ícone na bandeja. Toda a vida do D-Bus acontece nesta thread."""

    def __init__(self, commands: "queue.Queue[tuple]", events: "queue.Queue[tuple]") -> None:
        self.commands = commands
        self.events = events
        self.thread = threading.Thread(target=self._run, name="tray", daemon=True)
        self.visible = True
        self.revision = 1
        self.conn = None
        self.loop = None

    def start(self) -> None:
        self.thread.start()

    def _run(self) -> None:
        try:
            import gi

            gi.require_version("Gio", "2.0")
            from gi.repository import Gio, GLib
        except Exception as exc:  # pragma: no cover - depende do sistema
            self.events.put(("unavailable", f"python3-gi indisponível ({exc})"))
            return

        self._Gio, self._GLib = Gio, GLib
        try:
            conn = Gio.bus_get_sync(Gio.BusType.SESSION, None)
        except Exception as exc:
            self.events.put(("unavailable", f"sem barramento D-Bus ({exc})"))
            return
        self.conn = conn

        sni_info = Gio.DBusNodeInfo.new_for_xml(SNI_XML)
        menu_info = Gio.DBusNodeInfo.new_for_xml(MENU_XML)
        bus_name = f"org.kde.StatusNotifierItem-{os.getpid()}-1"
        self.bus_name = bus_name
        Gio.bus_own_name_on_connection(conn, bus_name, Gio.BusNameOwnerFlags.NONE, None, None)

        conn.register_object(
            "/StatusNotifierItem", sni_info.interfaces[0], self._sni_method, self._sni_property, None
        )
        conn.register_object(
            "/StatusNotifierItem", sni_info.interfaces[1], self._props_method, None, None
        )
        conn.register_object(
            "/MenuBar", menu_info.interfaces[0], self._menu_method, None, None
        )

        try:
            conn.call_sync(
                "org.kde.StatusNotifierWatcher",
                "/StatusNotifierWatcher",
                "org.kde.StatusNotifierWatcher",
                "RegisterStatusNotifierItem",
                GLib.Variant("(s)", (bus_name,)),
                None,
                Gio.DBusCallFlags.NONE,
                5000,
                None,
            )
        except Exception as exc:
            self.events.put(("unavailable", f"sem bandeja StatusNotifier ({exc})"))
            return

        self.events.put(("ready",))
        loop = GLib.MainLoop()
        self.loop = loop
        GLib.timeout_add(120, self._drain)
        loop.run()

    # -- comandos vindos da interface Tk ----------------------------------
    def _drain(self) -> bool:
        try:
            while True:
                command = self.commands.get_nowait()
                action = command[0]
                if action == "set_visible":
                    self.visible = bool(command[1])
                    self._invalidate()
                elif action == "quit":
                    if self.loop is not None:
                        self.loop.quit()
                    return False
        except queue.Empty:
            pass
        return True

    def _invalidate(self) -> None:
        self.revision += 1
        if self.conn is not None:
            try:
                self.conn.emit_signal(
                    None,
                    "/MenuBar",
                    "com.canonical.dbusmenu",
                    "LayoutUpdated",
                    self._GLib.Variant("(ui)", (self.revision, 0)),
                )
            except Exception:
                pass

    # -- itens de menu -----------------------------------------------------
    def _menu_items(self) -> list[tuple]:
        return [
            (
                MENU_TOGGLE,
                {
                    "label": self._GLib.Variant("s", "Recolher" if self.visible else "Mostrar"),
                    "enabled": self._GLib.Variant("b", True),
                    "visible": self._GLib.Variant("b", True),
                    "type": self._GLib.Variant("s", "standard"),
                },
                [],
            ),
            (
                MENU_SETTINGS,
                {
                    "label": self._GLib.Variant("s", "Configurações…"),
                    "enabled": self._GLib.Variant("b", True),
                    "visible": self._GLib.Variant("b", True),
                    "type": self._GLib.Variant("s", "standard"),
                },
                [],
            ),
            (
                MENU_REFRESH,
                {
                    "label": self._GLib.Variant("s", "Atualizar agora"),
                    "enabled": self._GLib.Variant("b", True),
                    "visible": self._GLib.Variant("b", True),
                    "type": self._GLib.Variant("s", "standard"),
                },
                [],
            ),
            (
                MENU_SEPARATOR,
                {
                    "visible": self._GLib.Variant("b", True),
                    "type": self._GLib.Variant("s", "separator"),
                },
                [],
            ),
            (
                MENU_QUIT,
                {
                    "label": self._GLib.Variant("s", "Fechar"),
                    "enabled": self._GLib.Variant("b", True),
                    "visible": self._GLib.Variant("b", True),
                    "type": self._GLib.Variant("s", "standard"),
                },
                [],
            ),
        ]

    def _item_props(self, item_id: int) -> dict:
        for ident, props, _children in self._menu_items():
            if ident == item_id:
                return props
        return {}

    # -- chamadas D-Bus ----------------------------------------------------
    def _sni_method(self, conn, sender, path, interface, method, params, invocation) -> None:
        if method in ("Activate", "SecondaryActivate"):
            self.events.put(("toggle",))
        invocation.return_value(None)

    def _sni_property(self, conn, sender, path, interface, name):
        values = {
            "Category": self._GLib.Variant("s", "ApplicationStatus"),
            "Id": self._GLib.Variant("s", APP_ID),
            "Title": self._GLib.Variant("s", APP_NAME),
            "Status": self._GLib.Variant("s", "Active"),
            "IconName": self._GLib.Variant("s", APP_ID),
            "IconThemePath": self._GLib.Variant("as", []),
            "Menu": self._GLib.Variant("o", "/MenuBar"),
            "ItemIsMenu": self._GLib.Variant("b", False),
        }
        return values.get(name)

    def _props_method(self, conn, sender, path, interface, method, params, invocation) -> None:
        args = params.unpack()
        if method == "Get":
            value = self._sni_property(conn, sender, path, interface, args[1])
            invocation.return_value(None if value is None else self._GLib.Variant("(v)", (value,)))
        elif method == "GetAll":
            values = {
                name: self._sni_property(conn, sender, path, interface, name)
                for name in (
                    "Category",
                    "Id",
                    "Title",
                    "Status",
                    "IconName",
                    "IconThemePath",
                    "Menu",
                    "ItemIsMenu",
                )
            }
            invocation.return_value(self._GLib.Variant("(a{sv})", (values,)))
        elif method == "Set":
            invocation.return_value(None)
        else:
            invocation.return_value(None)

    def _menu_method(self, conn, sender, path, interface, method, params, invocation) -> None:
        args = params.unpack()
        if method == "GetLayout":
            children = [
                self._GLib.Variant("(ia{sv}av)", item) for item in self._menu_items()
            ]
            root = (
                0,
                {"children-display": self._GLib.Variant("s", "submenu")},
                children,
            )
            invocation.return_value(self._GLib.Variant("(u(ia{sv}av))", (self.revision, root)))
        elif method == "GetGroupProperties":
            ids, _names = args
            props = [(int(i), self._item_props(int(i))) for i in ids if int(i) != 0]
            invocation.return_value(self._GLib.Variant("(a(ia{sv}))", (props,)))
        elif method == "GetProperty":
            item_id, name = args
            value = self._item_props(int(item_id)).get(name)
            if value is None:
                value = self._GLib.Variant("s", "")
            invocation.return_value(self._GLib.Variant("(v)", (value,)))
        elif method == "Event":
            item_id, event_id = int(args[0]), str(args[1])
            if event_id == "clicked":
                mapping = {
                    MENU_TOGGLE: "toggle",
                    MENU_SETTINGS: "settings",
                    MENU_REFRESH: "refresh",
                    MENU_QUIT: "quit",
                }
                action = mapping.get(item_id)
                if action:
                    self.events.put((action,))
            invocation.return_value(None)
        elif method == "AboutToShow":
            invocation.return_value(self._GLib.Variant("(b)", (True,)))
        elif method == "AboutToShowGroup":
            invocation.return_value(self._GLib.Variant("(aiai)", ([], [])))
        else:
            invocation.return_value(None)


# --------------------------------------------------------------------------
# Coleta de dados
# --------------------------------------------------------------------------
class UsageWorker(threading.Thread):
    def __init__(self, app: "App") -> None:
        super().__init__(name="usage", daemon=True)
        self.app = app
        self.stop_event = threading.Event()
        self.wake = threading.Event()
        self.api_key: Optional[str] = app.api_key # type: ignore[assignment]

    def stop(self) -> None:
        self.stop_event.set()
        self.wake.set()

    def refresh_now(self) -> None:
        self.wake.set()

    def run(self) -> None:
        while not self.stop_event.is_set():
            if self.app.demo:
                self.app.results.put(("demo", _demo_payload(), time.time()))
            elif not self.api_key:
                self.app.results.put(("nokey", None, time.time()))
            else:
                try:
                    self.app.results.put(("ok", fetch_usage(self.api_key), time.time()))
                except urllib.error.HTTPError as exc:
                    hint = "chave recusada" if exc.code in (401, 403) else f"HTTP {exc.code}"
                    self.app.results.put(("err", hint, time.time()))
                except Exception as exc:  # rede fora do ar etc.
                    self.app.results.put(("err", type(exc).__name__, time.time()))
            interval = int(self.app.config.data.get("interval", 30))
            self.wake.wait(interval)
            self.wake.clear()


def _demo_payload() -> dict[str, Any]:
    now = datetime.now(timezone.utc)
    return {
        "rolling": {"percent": 2, "resets": now + timedelta(hours=3, minutes=12)},
        "weekly": {"percent": 2, "resets": now + timedelta(days=2, hours=4)},
        "monthly": {"percent": 4, "resets": now + timedelta(days=12, hours=6)},
    }


# --------------------------------------------------------------------------
# Interface principal
# --------------------------------------------------------------------------
class App:
    def __init__(self, options: argparse.Namespace) -> None:
        self.options = options
        self.demo = bool(options.demo)
        self.config = Config()
        self.api_key, self.key_source = (None, "demonstração" if self.demo else "não encontrada")
        if not self.demo:
            self.api_key, self.key_source = resolve_api_key(self.config)

        self.results: "queue.Queue[tuple]" = queue.Queue()
        self.tray_commands: "queue.Queue[tuple]" = queue.Queue()
        self.tray_events: "queue.Queue[tuple]" = queue.Queue()
        self.tray = TrayService(self.tray_commands, self.tray_events)
        self.tray_available = False
        self.pending_start_minimized = bool(options.minimized)

        self.visible = True
        self.compact = False
        self.scale = float(self.config.data["scale"])
        self.last_status = "Conectando…"
        self.last_status_color = C["dim"]
        self.last_update: Optional[float] = None
        self.usage: dict[str, Any] = {}
        self.settings_window: Optional["SettingsWindow"] = None
        self._quit = False
        self._drag = None

        self.root = tk.Tk(className=APP_ID)
        self.root.title(APP_NAME)
        self.root.configure(bg=C["bg"])
        self.root.overrideredirect(True)
        self._apply_window_type()
        self.root.attributes("-topmost", True)
        self.root.attributes("-alpha", float(self.config.data["opacity"]))
        self.canvas = tk.Canvas(self.root, bg=C["bg"], highlightthickness=0, bd=0)
        self.canvas.pack(fill="both", expand=True)
        self._build_fonts()
        self._bind_events()
        self.root.geometry(self._geometry(*self._scaled_dims()))
        self._draw()

        ensure_user_icon()
        self._install_signals()

        self.worker = UsageWorker(self)
        self.worker.start()
        self.tray.start()

        self.root.after(150, self._pump)
        self.root.after(250, self._signal_tick)
        if options.settings:
            self.root.after(300, self.open_settings)

    # -- janela -----------------------------------------------------------
    def _apply_window_type(self) -> None:
        window_type = str(self.config.data.get("window_type") or "dock")
        if window_type == "override":
            return
        try:
            self.root.attributes("-type", window_type)
        except tk.TclError:
            pass

    def _geometry(self, width: int, height: int) -> str:
        position = self.config.data.get("position")
        if isinstance(position, list) and len(position) == 2:
            x, y = int(position[0]), int(position[1])
        else:
            x = self.root.winfo_screenwidth() - int(width) - 24
            y = 24
        x = max(0, min(x, self.root.winfo_screenwidth() - width))
        y = max(0, min(y, self.root.winfo_screenheight() - height))
        return f"{width}x{height}+{x}+{y}"

    def _scaled_dims(self) -> tuple[int, int]:
        if self.compact:
            return int(COMPACT_W * self.scale), int(COMPACT_H * self.scale)
        return int(BASE_W * self.scale), int(BASE_H * self.scale)

    def _build_fonts(self) -> None:
        size = self.scale
        self.font_title = tkfont.Font(family="Inter", size=max(8, round(11 * size)), weight="bold")
        self.font_label = tkfont.Font(family="Inter", size=max(7, round(9 * size)))
        self.font_percent = tkfont.Font(family="Inter", size=max(8, round(10 * size)), weight="bold")
        self.font_small = tkfont.Font(family="Inter", size=max(6, round(8 * size)))
        self.font_icon = tkfont.Font(family="Inter", size=max(9, round(12 * size)))
        self.font_pill = tkfont.Font(family="Inter", size=max(7, round(9 * size)), weight="bold")

    def _bind_events(self) -> None:
        self.canvas.bind("<Button-1>", self._on_press)
        self.canvas.bind("<B1-Motion>", self._on_drag)
        self.canvas.bind("<ButtonRelease-1>", self._on_release)
        self.canvas.bind("<Double-Button-1>", self._on_double_click)
        self.canvas.bind("<Button-3>", self._on_right_click)
        self.root.bind("<Escape>", lambda _e: self.minimize())
        self.root.bind("<Control-q>", lambda _e: self.quit())
        self.root.protocol("WM_DELETE_WINDOW", self.quit)

    # -- desenho -----------------------------------------------------------
    def _S(self, value: float) -> int:
        return int(round(value * self.scale))

    def _draw(self) -> None:
        self.canvas.delete("all")
        if self.compact:
            self._draw_compact()
        else:
            self._draw_card()

    def _draw_card(self) -> None:
        w, h = self._scaled_dims()
        S = self._S
        self.canvas.configure(width=w, height=h)
        self.canvas.create_rectangle(1, 1, w - 1, h - 1, fill=C["bg"], outline=C["border"], width=1)
        self.canvas.create_rectangle(1, 1, w - 1, S(30), fill=C["bg"], outline="")
        self.canvas.create_text(S(16), S(17), anchor="w", text="OpenCode Go", fill=C["fg"], font=self.font_title)
        self.canvas.create_text(
            S(16), S(33), anchor="w", text="medidores de uso", fill=C["dim"], font=self.font_small
        )

        self._buttons = self._button_boxes(w)
        self.canvas.create_text(
            self._buttons["gear"][0] + (self._buttons["gear"][2] - self._buttons["gear"][0]) // 2,
            S(17),
            text="⚙",
            fill=C["muted"],
            font=self.font_icon,
            tags="gear",
        )
        self.canvas.create_text(
            self._buttons["min"][0] + (self._buttons["min"][2] - self._buttons["min"][0]) // 2,
            S(17),
            text="–",
            fill=C["muted"],
            font=self.font_icon,
            tags="min",
        )
        self.canvas.create_text(
            self._buttons["close"][0] + (self._buttons["close"][2] - self._buttons["close"][0]) // 2,
            S(17),
            text="×",
            fill=C["muted"],
            font=self.font_icon,
            tags="close",
        )

        for index, (name, label) in enumerate(METERS):
            top = S(50 + index * 40)
            data = self.usage.get(name) or {}
            percent = data.get("percent")
            self.canvas.create_text(S(16), top, anchor="w", text=label, fill=C["muted"], font=self.font_label)
            badge = "—" if percent is None else f"{int(percent)}%"
            self.canvas.create_text(w - S(16), top, anchor="e", text=badge, fill=bar_color(percent), font=self.font_percent)
            bar_left, bar_right = S(16), w - S(16)
            bar_top, bar_bottom = top + S(10), top + S(17)
            self.canvas.create_rectangle(bar_left, bar_top, bar_right, bar_bottom, fill=C["bar_bg"], outline="")
            if percent:
                fill_width = int((bar_right - bar_left) * _clamp(percent, 0, 100) / 100.0)
                if fill_width > 2:
                    self.canvas.create_rectangle(
                        bar_left, bar_top, bar_left + fill_width, bar_bottom, fill=bar_color(percent), outline=""
                    )
            self.canvas.create_text(
                S(16),
                top + S(26),
                anchor="w",
                text=f"Resets in {fmt_reset(data.get('resets'))}",
                fill=C["dim"],
                font=self.font_small,
            )

        self.canvas.create_text(
            S(16), h - S(14), anchor="w", text=self.last_status, fill=self.last_status_color, font=self.font_small
        )

    def _draw_compact(self) -> None:
        w, h = self._scaled_dims()
        self.canvas.configure(width=w, height=h)
        self.canvas.create_rectangle(1, 1, w - 1, h - 1, fill=C["bg"], outline=C["border"], width=1)
        rolling = (self.usage.get("rolling") or {}).get("percent")
        weekly = (self.usage.get("weekly") or {}).get("percent")
        monthly = (self.usage.get("monthly") or {}).get("percent")
        self.canvas.create_oval(self._S(12), h // 2 - self._S(4), self._S(20), h // 2 + self._S(4), fill=bar_color(monthly), outline="")
        text = "  ".join(f"{value if value is not None else '—'}%" for value in (rolling, weekly, monthly))
        self.canvas.create_text(self._S(28), h // 2, anchor="w", text=text, fill=C["fg"], font=self.font_pill)
        self.canvas.create_text(w - self._S(12), h // 2, anchor="e", text="↗", fill=C["dim"], font=self.font_small)

    def _button_boxes(self, w: int) -> dict[str, tuple[int, int, int, int]]:
        S = self._S
        return {
            "close": (w - S(28), 0, w, S(32)),
            "min": (w - S(56), 0, w - S(28), S(32)),
            "gear": (w - S(84), 0, w - S(56), S(32)),
        }

    def _refresh(self) -> None:
        self.canvas.configure(bg=C["bg"])
        if not self.compact:
            self._draw()
        elif self.visible:
            self._draw()

    # -- eventos de ponteiro ----------------------------------------------
    def _on_press(self, event: "tk.Event") -> None:
        self._drag = None
        if self.compact:
            return
        for name, (x0, y0, x1, y1) in self._button_boxes(self._scaled_dims()[0]).items():
            if x0 <= event.x <= x1 and y0 <= event.y <= y1:
                if name == "close":
                    self.quit()
                elif name == "min":
                    self.minimize()
                elif name == "gear":
                    self.open_settings()
                return
        self._drag = (event.x_root - self.root.winfo_x(), event.y_root - self.root.winfo_y())

    def _on_drag(self, event: "tk.Event") -> None:
        if not self._drag:
            return
        x = event.x_root - self._drag[0]
        y = event.y_root - self._drag[1]
        self.root.geometry(f"+{x}+{y}")

    def _on_release(self, event: "tk.Event") -> None:
        if self.compact:
            self.restore()
            return
        if self._drag:
            self._drag = None
            self.config.data["position"] = [self.root.winfo_x(), self.root.winfo_y()]
            self.config.save()

    def _on_double_click(self, event: "tk.Event") -> None:
        if self.compact:
            self.restore()
            return
        self._open_console()

    def _on_right_click(self, event: "tk.Event") -> None:
        menu = tk.Menu(
            self.root,
            tearoff=0,
            bg=C["bg"],
            fg=C["fg"],
            activebackground=C["border"],
            activeforeground=C["fg"],
            bd=0,
        )
        menu.add_command(label="Configurações…", command=self.open_settings)
        menu.add_command(label="Atualizar agora", command=self.worker.refresh_now)
        if self.tray_available:
            menu.add_separator()
            menu.add_command(label="Recolher para a bandeja", command=self.minimize)
        menu.add_separator()
        menu.add_command(label="Abrir console do OpenCode", command=self._open_console)
        menu.add_command(label="Fechar", command=self.quit)
        try:
            menu.tk_popup(event.x_root, event.y_root)
        finally:
            menu.grab_release()

    def _open_console(self) -> None:
        try:
            import webbrowser

            webbrowser.open(CONSOLE_URL)
        except Exception:
            pass

    # -- mostrar/recolher --------------------------------------------------
    def minimize(self) -> None:
        if self.tray_available:
            self.visible = False
            self.root.withdraw()
            self.tray_commands.put(("set_visible", False))
        else:
            self.compact = True
            self.visible = True
            self._build_fonts()
            self.root.geometry(self._geometry(*self._scaled_dims()))
            self._draw()

    def restore(self) -> None:
        if self.compact:
            self.compact = False
            self._build_fonts()
            self.root.geometry(self._geometry(*self._scaled_dims()))
            self._draw()
        self.visible = True
        try:
            self.root.deiconify()
        except tk.TclError:
            pass
        self.root.lift()
        self.tray_commands.put(("set_visible", True))

    def toggle(self) -> None:
        if self.visible:
            self.minimize()
        else:
            self.restore()

    # -- ciclo de vida -----------------------------------------------------
    def _install_signals(self) -> None:
        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                signal.signal(sig, lambda *_a: setattr(self, "_signal_quit", True))
            except (ValueError, OSError):
                pass
        self._signal_quit = False

    def _signal_tick(self) -> None:
        # Garante que handlers de sinal rodem: o Tcl chama Python a cada 250 ms.
        if self._signal_quit:
            self.quit()
            return
        self.root.after(250, self._signal_tick)

    def _pump(self) -> None:
        try:
            while True:
                kind, payload, stamp = self.results.get_nowait()
                if kind in ("ok", "demo"):
                    self.usage = payload
                    self.last_update = stamp
                    suffix = " · demonstração" if kind == "demo" else f" · {self.key_source}"
                    self.last_status = f"Atualizado {time.strftime('%H:%M:%S')}{suffix}"
                    self.last_status_color = C["dim"]
                elif kind == "nokey":
                    self.last_status = "Sem chave — abra as configurações (⚙)"
                    self.last_status_color = C["amber"]
                else:
                    self.last_status = f"Falha na atualização: {payload}"
                    self.last_status_color = C["red"]
                self._refresh()
        except queue.Empty:
            pass

        try:
            while True:
                event = self.tray_events.get_nowait()
                action = event[0]
                if action == "ready":
                    self.tray_available = True
                    if self.pending_start_minimized:
                        self.pending_start_minimized = False
                        self.minimize()
                elif action == "unavailable":
                    self.tray_available = False
                    self.last_status = f"Bandeja indisponível ({event[1]})"
                    self.last_status_color = C["amber"]
                    self._refresh()
                    if self.pending_start_minimized:
                        self.pending_start_minimized = False
                        self.minimize()
                elif action == "toggle":
                    self.toggle()
                elif action == "settings":
                    self.open_settings()
                elif action == "refresh":
                    self.worker.refresh_now()
                elif action == "quit":
                    self.quit()
        except queue.Empty:
            pass

        if self.last_update and time.time() - self.last_update > int(self.config.data["interval"]) * 2.5 + 10:
            if not self.last_status.startswith("Dados antigos"):
                self.last_status = "Dados antigos — sem resposta do servidor"
                self.last_status_color = C["amber"]
                self._refresh()

        self.root.after(150, self._pump)

    def quit(self) -> None:
        if self._quit:
            return
        self._quit = True
        try:
            self.config.data["position"] = [self.root.winfo_x(), self.root.winfo_y()]
            self.config.save()
        except Exception:
            pass
        self.tray_commands.put(("quit",))
        self.worker.stop()
        try:
            self.root.destroy()
        except tk.TclError:
            pass

    # -- configurações -----------------------------------------------------
    def open_settings(self) -> None:
        if self.settings_window is not None and self.settings_window.alive:
            self.settings_window.focus()
            return
        self.settings_window = SettingsWindow(self)

    def apply_scale(self, scale: float) -> None:
        self.scale = _clamp(scale, 0.8, 1.6)
        self._build_fonts()
        self.root.geometry(self._geometry(*self._scaled_dims()))
        self._draw()

    def apply_opacity(self, opacity: float) -> None:
        try:
            self.root.attributes("-alpha", _clamp(opacity, 0.3, 1.0))
        except tk.TclError:
            pass

    def set_api_key(self, key: Optional[str]) -> None:
        key = (key or "").strip() or None
        self.config.data["api_key"] = key
        if key:
            self.api_key, self.key_source = key, "configuração local"
        else:
            self.api_key, self.key_source = resolve_api_key(self.config)
        self.worker.api_key = self.api_key
        self.worker.refresh_now()


# --------------------------------------------------------------------------
# Janela de configurações
# --------------------------------------------------------------------------
class SettingsWindow:
    def __init__(self, app: App) -> None:
        self.app = app
        self.alive = True
        self.test_queue: "queue.Queue[tuple]" = queue.Queue()

        top = tk.Toplevel(app.root)
        self.top = top
        top.title("Configurações — OpenCode Go Usage")
        top.configure(bg=C["bg"])
        top.resizable(False, False)
        top.attributes("-topmost", True)
        top.protocol("WM_DELETE_WINDOW", self.close)

        pad = {"padx": 18, "pady": 6}
        width = 430

        tk.Label(top, text="Configurações", bg=C["bg"], fg=C["fg"], font=("Inter", 13, "bold")).pack(
            anchor="w", padx=18, pady=(16, 2)
        )
        tk.Label(
            top,
            text="A chave nunca é enviada a lugar nenhum além da API do OpenCode\ne nunca é gravada no repositório do projeto.",
            bg=C["bg"],
            fg=C["dim"],
            font=("Inter", 8),
            justify="left",
        ).pack(anchor="w", padx=18, pady=(0, 10))

        # --- API key ------------------------------------------------------
        tk.Label(top, text="CHAVE DE API", bg=C["bg"], fg=C["accent"], font=("Inter", 8, "bold")).pack(
            anchor="w", padx=18
        )
        entry_row = tk.Frame(top, bg=C["bg"])
        entry_row.pack(fill="x", padx=18, pady=(4, 2))
        self.key_var = tk.StringVar(value=app.api_key or "")
        self.key_entry = tk.Entry(
            entry_row,
            textvariable=self.key_var,
            show="•",
            bg=C["bar_bg"],
            fg=C["fg"],
            insertbackground=C["fg"],
            relief="flat",
            font=("DejaVu Sans Mono", 9),
        )
        self.key_entry.pack(side="left", fill="x", expand=True, ipady=5)
        self.show_var = tk.BooleanVar(value=False)
        tk.Checkbutton(
            entry_row,
            text="mostrar",
            variable=self.show_var,
            command=self._toggle_key_visibility,
            bg=C["bg"],
            fg=C["muted"],
            selectcolor=C["bar_bg"],
            activebackground=C["bg"],
            activeforeground=C["fg"],
            highlightthickness=0,
            bd=0,
            font=("Inter", 8),
        ).pack(side="left", padx=(8, 0))

        status_row = tk.Frame(top, bg=C["bg"])
        status_row.pack(fill="x", padx=18, pady=(0, 12))
        self.key_status = tk.Label(
            status_row,
            text=f"Origem atual: {app.key_source}",
            bg=C["bg"],
            fg=C["dim"],
            font=("Inter", 8),
        )
        self.key_status.pack(side="left")
        tk.Button(
            status_row,
            text="Testar",
            command=self._test_key,
            bg=C["border"],
            fg=C["fg"],
            activebackground=C["bar_bg"],
            activeforeground=C["fg"],
            relief="flat",
            bd=0,
            padx=12,
            font=("Inter", 8),
        ).pack(side="right")

        # --- aparência ----------------------------------------------------
        tk.Label(top, text="APARÊNCIA", bg=C["bg"], fg=C["accent"], font=("Inter", 8, "bold")).pack(
            anchor="w", padx=18
        )
        self.scale_var = tk.IntVar(value=int(round(float(app.config.data["scale"]) * 100)))
        self.scale_label = tk.Label(top, text=f"Tamanho: {self.scale_var.get()}%", bg=C["bg"], fg=C["muted"], font=("Inter", 9))
        self.scale_label.pack(anchor="w", padx=18, pady=(6, 0))
        tk.Scale(
            top,
            from_=80,
            to=160,
            resolution=5,
            orient="horizontal",
            showvalue=False,
            variable=self.scale_var,
            command=self._on_scale,
            bg=C["bg"],
            fg=C["fg"],
            troughcolor=C["bar_bg"],
            activebackground=C["accent"],
            highlightthickness=0,
            bd=0,
            length=width - 60,
        ).pack(anchor="w", padx=18)

        self.opacity_var = tk.IntVar(value=int(round(float(app.config.data["opacity"]) * 100)))
        self.opacity_label = tk.Label(
            top, text=f"Transparência: {self.opacity_var.get()}%", bg=C["bg"], fg=C["muted"], font=("Inter", 9)
        )
        self.opacity_label.pack(anchor="w", padx=18, pady=(6, 0))
        tk.Scale(
            top,
            from_=30,
            to=100,
            resolution=1,
            orient="horizontal",
            showvalue=False,
            variable=self.opacity_var,
            command=self._on_opacity,
            bg=C["bg"],
            fg=C["fg"],
            troughcolor=C["bar_bg"],
            activebackground=C["accent"],
            highlightthickness=0,
            bd=0,
            length=width - 60,
        ).pack(anchor="w", padx=18, pady=(0, 6))

        # --- atualização --------------------------------------------------
        tk.Label(top, text="ATUALIZAÇÃO", bg=C["bg"], fg=C["accent"], font=("Inter", 8, "bold")).pack(
            anchor="w", padx=18, pady=(8, 0)
        )
        interval_row = tk.Frame(top, bg=C["bg"])
        interval_row.pack(fill="x", padx=18, pady=(6, 10))
        tk.Label(interval_row, text="Intervalo (segundos):", bg=C["bg"], fg=C["muted"], font=("Inter", 9)).pack(side="left")
        self.interval_var = tk.IntVar(value=int(app.config.data["interval"]))
        tk.Spinbox(
            interval_row,
            from_=10,
            to=300,
            increment=5,
            textvariable=self.interval_var,
            width=5,
            bg=C["bar_bg"],
            fg=C["fg"],
            buttonbackground=C["border"],
            insertbackground=C["fg"],
            relief="flat",
            font=("Inter", 9),
        ).pack(side="left", padx=8)

        # --- sistema ------------------------------------------------------
        tk.Label(top, text="SISTEMA", bg=C["bg"], fg=C["accent"], font=("Inter", 8, "bold")).pack(
            anchor="w", padx=18
        )
        self.autostart_var = tk.BooleanVar(value=autostart_enabled())
        self.autostart_check = tk.Checkbutton(
            top,
            text="Iniciar com o computador",
            variable=self.autostart_var,
            bg=C["bg"],
            fg=C["fg"],
            selectcolor=C["bar_bg"],
            activebackground=C["bg"],
            activeforeground=C["fg"],
            highlightthickness=0,
            bd=0,
            font=("Inter", 9),
            anchor="w",
        )
        self.autostart_check.pack(anchor="w", padx=18, pady=(6, 0))
        self.minimized_var = tk.BooleanVar(value=bool(app.config.data.get("start_minimized")))
        tk.Checkbutton(
            top,
            text="Iniciar recolhido na bandeja",
            variable=self.minimized_var,
            bg=C["bg"],
            fg=C["fg"],
            selectcolor=C["bar_bg"],
            activebackground=C["bg"],
            activeforeground=C["fg"],
            highlightthickness=0,
            bd=0,
            font=("Inter", 9),
            anchor="w",
        ).pack(anchor="w", padx=18, pady=(2, 12))

        # --- botões -------------------------------------------------------
        buttons = tk.Frame(top, bg=C["bg"])
        buttons.pack(fill="x", padx=18, pady=(0, 16))
        self.save_button = tk.Button(
            buttons,
            text="Salvar",
            command=self.save,
            bg=C["accent"],
            fg="#0b0e13",
            activebackground="#7dd3fc",
            activeforeground="#0b0e13",
            relief="flat",
            bd=0,
            padx=22,
            pady=6,
            font=("Inter", 9, "bold"),
        )
        self.save_button.pack(side="right")
        tk.Button(
            buttons,
            text="Fechar",
            command=self.close,
            bg=C["border"],
            fg=C["fg"],
            activebackground=C["bar_bg"],
            activeforeground=C["fg"],
            relief="flat",
            bd=0,
            padx=18,
            pady=6,
            font=("Inter", 9),
        ).pack(side="right", padx=(0, 8))

        top.update_idletasks()
        x = app.root.winfo_x() - width - 20
        if x < 10:
            x = max(10, app.root.winfo_x() + app.root.winfo_width() + 20 - width - app.root.winfo_width())
            x = max(10, min(x, app.root.winfo_screenwidth() - width - 20))
        y = app.root.winfo_y()
        top.geometry(f"+{x}+{max(10, y)}")
        top.after(200, lambda: self._poll_test())

    # -- ações -------------------------------------------------------------
    def _toggle_key_visibility(self) -> None:
        self.key_entry.configure(show="" if self.show_var.get() else "•")

    def _on_scale(self, _value: str) -> None:
        percent = int(self.scale_var.get())
        self.scale_label.configure(text=f"Tamanho: {percent}%")
        self.app.apply_scale(percent / 100.0)

    def _on_opacity(self, _value: str) -> None:
        percent = int(self.opacity_var.get())
        self.opacity_label.configure(text=f"Transparência: {percent}%")
        self.app.apply_opacity(percent / 100.0)

    def _test_key(self) -> None:
        key = (self.key_var.get() or "").strip() or self.app.api_key
        if not key:
            self.key_status.configure(text="Nenhuma chave para testar.", fg=C["amber"])
            return
        self.key_status.configure(text="Testando…", fg=C["dim"])

        def run() -> None:
            try:
                usage = fetch_usage(key)
                summary = " · ".join(
                    f"{name} {int((usage.get(name) or {}).get('percent') or 0)}%"
                    for name in ("rolling", "weekly", "monthly")
                )
                self.test_queue.put(("ok", f"Chave válida · {summary}"))
            except urllib.error.HTTPError as exc:
                self.test_queue.put(("err", f"Chave recusada (HTTP {exc.code})"))
            except Exception as exc:
                self.test_queue.put(("err", f"Falha de rede ({type(exc).__name__})"))

        threading.Thread(target=run, daemon=True).start()

    def _poll_test(self) -> None:
        if not self.alive:
            return
        try:
            while True:
                kind, message = self.test_queue.get_nowait()
                self.key_status.configure(text=message, fg=C["green"] if kind == "ok" else C["red"])
        except queue.Empty:
            pass
        self.top.after(200, self._poll_test)

    def save(self) -> None:
        app = self.app
        app.config.data["scale"] = _clamp(self.scale_var.get() / 100.0, 0.8, 1.6)
        app.config.data["opacity"] = _clamp(self.opacity_var.get() / 100.0, 0.3, 1.0)
        app.config.data["start_minimized"] = bool(self.minimized_var.get())
        typed_key = (self.key_var.get() or "").strip()
        if typed_key != (app.config.data.get("api_key") or ""):
            app.set_api_key(typed_key)
        try:
            interval = int(self.interval_var.get())
        except (tk.TclError, ValueError):
            interval = int(app.config.data["interval"])
        app.config.data["interval"] = int(_clamp(interval, 10, 300))
        autostart = bool(self.autostart_var.get())
        if not set_autostart(autostart, bool(self.minimized_var.get())):
            self.key_status.configure(text="Não foi possível alterar o autostart.", fg=C["red"])
        app.config.save()
        app.apply_scale(app.config.data["scale"])
        app.apply_opacity(app.config.data["opacity"])
        app.worker.refresh_now()
        try:
            self.save_button.configure(text="Salvo ✓")
            self.top.after(1200, lambda: self.save_button.configure(text="Salvar"))
        except tk.TclError:
            pass

    def focus(self) -> None:
        self.top.lift()
        self.top.focus_force()

    def close(self) -> None:
        try:
            self.save()
        except Exception:
            pass
        self.alive = False
        try:
            self.top.destroy()
        except tk.TclError:
            pass


# --------------------------------------------------------------------------
# Instância única
# --------------------------------------------------------------------------
def acquire_lock() -> Optional[Any]:
    """Mantém um flock durante toda a execução; o kernel libera ao sair."""
    import fcntl

    handle = None
    try:
        LOCK_PATH.parent.mkdir(parents=True, exist_ok=True)
        handle = open(LOCK_PATH, "w", encoding="utf-8")
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        handle.write(str(os.getpid()))
        handle.flush()
        return handle
    except OSError:
        if handle is not None:
            handle.close()
        return None


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------
def print_once(api_key: Optional[str], source: str) -> int:
    if not api_key:
        print(json.dumps({"error": "api key não encontrada", "source": source}), file=sys.stderr)
        return 2
    try:
        usage = fetch_usage(api_key)
    except urllib.error.HTTPError as exc:
        print(json.dumps({"error": f"HTTP {exc.code}"}), file=sys.stderr)
        return 1
    except Exception as exc:
        print(json.dumps({"error": type(exc).__name__}), file=sys.stderr)
        return 1
    payload = {
        name: {
            "percent": (usage.get(name) or {}).get("percent"),
            "resetsAt": (usage.get(name) or {}).get("resets").isoformat()
            if (usage.get(name) or {}).get("resets")
            else None,
        }
        for name in ("rolling", "weekly", "monthly")
    }
    print(json.dumps(payload, indent=2))
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(prog=APP_ID, description="Medidores de uso do OpenCode Go em uma janela flutuante.")
    parser.add_argument("--once", action="store_true", help="imprime o uso em JSON e sai")
    parser.add_argument("--demo", action="store_true", help="dados simulados (sem rede, sem chave)")
    parser.add_argument("--settings", action="store_true", help="abre as configurações ao iniciar")
    parser.add_argument("--minimized", action="store_true", help="inicia recolhido na bandeja")
    parser.add_argument("--version", action="version", version=f"%(prog)s {VERSION}")
    options = parser.parse_args()

    if options.once:
        config = Config()
        api_key, source = resolve_api_key(config)
        return print_once(api_key, source)

    config = Config()
    lock = acquire_lock()
    if lock is None:
        print(f"{APP_ID} já está em execução.", file=sys.stderr)
        return 3
    try:
        app = App(options)
        app.root.mainloop()
    finally:
        try:
            lock.close()
        except Exception:
            pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
