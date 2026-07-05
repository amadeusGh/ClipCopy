#!/usr/bin/env python3
"""Clipboard client for text-only synchronization over WebSockets."""

from __future__ import annotations

import argparse
import asyncio
import ctypes
import ctypes.util
import importlib
import json
import os
import queue
import select
import shutil
import signal
import ssl
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path

from clipcopy_common import ClipboardPayload, now_ms, payload_fingerprint, payload_summary
from clipcopy_logging import configure_logging, get_logger


class ClipboardUnavailableError(RuntimeError):
    """Raised when no usable clipboard backend is available."""


class WebSocketUnavailableError(RuntimeError):
    """Raised when the websockets dependency is missing."""


LOGGER = get_logger("clipcopy.client")


class XSelectionEvent(ctypes.Structure):
    _fields_ = [
        ("type", ctypes.c_int),
        ("serial", ctypes.c_ulong),
        ("send_event", ctypes.c_int),
        ("display", ctypes.c_void_p),
        ("requestor", ctypes.c_ulong),
        ("selection", ctypes.c_ulong),
        ("target", ctypes.c_ulong),
        ("property", ctypes.c_ulong),
        ("time", ctypes.c_ulong),
    ]


class XFixesSelectionNotifyEvent(ctypes.Structure):
    _fields_ = [
        ("type", ctypes.c_int),
        ("serial", ctypes.c_ulong),
        ("send_event", ctypes.c_int),
        ("display", ctypes.c_void_p),
        ("window", ctypes.c_ulong),
        ("subtype", ctypes.c_int),
        ("owner", ctypes.c_ulong),
        ("selection", ctypes.c_ulong),
        ("timestamp", ctypes.c_ulong),
        ("selection_timestamp", ctypes.c_ulong),
    ]


class XEvent(ctypes.Union):
    _fields_ = [
        ("type", ctypes.c_int),
        ("xselection", XSelectionEvent),
        ("xfixes_selection", XFixesSelectionNotifyEvent),
        ("pad", ctypes.c_long * 24),
    ]


class PropertyValue:
    def __init__(self, actual_type: int, actual_format: int, item_count: int, data: bytes) -> None:
        self.actual_type = actual_type
        self.actual_format = actual_format
        self.item_count = item_count
        self.data = data


def install_termination_handlers() -> None:
    """Translate SIGINT/SIGTERM into KeyboardInterrupt for clean shutdown."""

    def handler(signum, frame):
        raise KeyboardInterrupt()

    signal.signal(signal.SIGINT, handler)
    signal.signal(signal.SIGTERM, handler)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Sync text clipboard contents with a WebSocket server."
    )
    parser.add_argument(
        "--config",
        default="client_config.json",
        help="Path to client JSON config. Default: client_config.json",
    )
    parser.add_argument("--server-host", default=None, help="Server host.")
    parser.add_argument("--server-port", type=int, default=None, help="Server port.")
    parser.add_argument("--state-dir", default=None, help="Client state directory.")
    parser.add_argument("--auth-token", default=None, help="Shared auth token for the server.")
    parser.add_argument("--tls", action="store_true", help="Use TLS (wss://) for transport encryption.")
    parser.add_argument("--ca-cert", default=None, help="CA certificate for verifying the server cert.")
    parser.add_argument(
        "--insecure-tls",
        action="store_true",
        help="Disable TLS certificate verification. Testing only.",
    )
    parser.add_argument(
        "--no-auto-install",
        action="store_true",
        help="Disable best-effort installation of missing dependencies.",
    )

    subparsers = parser.add_subparsers(dest="command", required=True)

    watch_parser = subparsers.add_parser("watch", help="Watch local clipboard and push to server.")
    watch_parser.add_argument(
        "--interval",
        type=float,
        default=0.4,
        help="Polling interval for fallback clipboard backends.",
    )

    sync_parser = subparsers.add_parser(
        "sync",
        help="Watch local clipboard, push to server, and apply remote updates immediately.",
    )
    sync_parser.add_argument(
        "--interval",
        type=float,
        default=0.4,
        help="Polling interval for fallback clipboard backends.",
    )

    subparsers.add_parser(
        "paste",
        help="Fetch the latest payload from the server and apply it if it is fresher.",
    )
    return parser.parse_args()


def load_client_config(path: str) -> dict:
    config_path = Path(path)
    if not config_path.exists():
        return {}
    return json.loads(config_path.read_text(encoding="utf-8"))


def resolve_client_settings(args: argparse.Namespace) -> argparse.Namespace:
    config = load_client_config(args.config)
    args.server_host = args.server_host or config.get("server_host") or "127.0.0.1"
    args.server_port = args.server_port or int(config.get("server_port", 8765))
    args.state_dir = args.state_dir or config.get("state_dir") or ".clipcopy_state"
    args.auth_token = args.auth_token or config.get("auth_token")
    if not args.tls:
        args.tls = bool(config.get("tls", False))
    args.ca_cert = args.ca_cert or config.get("ca_cert")
    if not args.insecure_tls:
        args.insecure_tls = bool(config.get("insecure_tls", False))
    if hasattr(args, "interval") and args.interval == 0.4 and config.get("interval") is not None:
        args.interval = float(config["interval"])
    return args


def ensure_websockets(no_auto_install: bool):
    try:
        return importlib.import_module("websockets")
    except ImportError as exc:
        if no_auto_install:
            raise WebSocketUnavailableError("Python package 'websockets' is not installed.") from exc

    LOGGER.info("Installing missing Python dependency: websockets")
    result = subprocess.run(
        [sys.executable, "-m", "pip", "install", "--user", "websockets"],
        check=False,
    )
    if result.returncode != 0:
        raise WebSocketUnavailableError("Failed to install Python package 'websockets'.")
    return importlib.import_module("websockets")


def maybe_install_linux_clipboard_tools(no_auto_install: bool) -> None:
    if no_auto_install or not sys.platform.startswith("linux"):
        return

    session_type = os.environ.get("XDG_SESSION_TYPE", "").lower()
    package = "wl-clipboard" if session_type == "wayland" else "xclip"
    binary = "wl-paste" if session_type == "wayland" else "xclip"

    if shutil.which(binary) or not shutil.which("apt-get") or not sys.stdin.isatty():
        return

    LOGGER.info("Installing missing clipboard backend package: %s", package)
    subprocess.run(["sudo", "apt-get", "install", "-y", package], check=False)


def state_file_path(state_dir: Path) -> Path:
    return state_dir / "client_state.json"


def load_state(state_dir: Path) -> dict:
    path = state_file_path(state_dir)
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def save_state(state_dir: Path, payload: ClipboardPayload) -> None:
    state_dir.mkdir(parents=True, exist_ok=True)
    state = {
        "fingerprint": payload_fingerprint(payload),
        "created_at_ms": payload.created_at_ms,
        "payload": payload.to_dict(),
    }
    state_file_path(state_dir).write_text(
        json.dumps(state, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def print_payload(prefix: str, payload: ClipboardPayload) -> None:
    LOGGER.info("%s: %s", prefix, payload_summary(payload))


def server_url(args: argparse.Namespace) -> str:
    scheme = "wss" if args.tls else "ws"
    return f"{scheme}://{args.server_host}:{args.server_port}"


def build_client_ssl_context(args: argparse.Namespace) -> ssl.SSLContext | None:
    if not args.tls:
        return None
    if args.insecure_tls:
        return ssl._create_unverified_context()
    return ssl.create_default_context(cafile=args.ca_cert)


async def send_hello(websocket, args: argparse.Namespace, mode: str, client_id: str) -> None:
    await websocket.send(
        json.dumps(
            {
                "action": "hello",
                "mode": mode,
                "client_id": client_id,
                "auth_token": args.auth_token,
            },
            ensure_ascii=False,
        )
    )
    response = json.loads(await websocket.recv())
    if response.get("status") != "ok":
        raise RuntimeError(response.get("error", "Authentication failed."))
    LOGGER.info(
        "Authenticated with server using mode=%s, tls=%s, token=%s",
        mode,
        "enabled" if args.tls else "disabled",
        "provided" if args.auth_token else "not provided",
    )


def replace_queue_item(payload_queue: "queue.Queue[ClipboardPayload | None]", payload) -> None:
    while True:
        try:
            payload_queue.get_nowait()
        except queue.Empty:
            break
    payload_queue.put(payload)


class CommandClipboardWriter:
    """Write text payloads back into the local clipboard."""

    def __init__(self, managed_owner: bool) -> None:
        self.session_type = os.environ.get("XDG_SESSION_TYPE", "").lower()
        self.managed_owner = managed_owner
        self._owner_process: subprocess.Popen | None = None

    def close(self) -> None:
        process = self._owner_process
        self._owner_process = None
        if process is None or process.poll() is not None:
            return

        process.terminate()
        try:
            process.wait(timeout=2)
        except subprocess.TimeoutExpired:
            LOGGER.warning("Clipboard owner helper did not stop in time; sending SIGKILL")
            process.kill()
            process.wait(timeout=2)

    def apply(self, payload: ClipboardPayload) -> None:
        if payload.kind == "empty":
            self._write_text("")
            return
        if payload.kind != "text":
            raise ClipboardUnavailableError(f"Unsupported clipboard kind: {payload.kind}")
        self._write_text(payload.text or "")

    def _write_text(self, text: str) -> None:
        input_bytes = text.encode("utf-8")
        if self.session_type == "wayland" and shutil.which("wl-copy"):
            if self.managed_owner:
                self._start_managed_owner(["wl-copy", "--foreground"], input_bytes)
            else:
                self._run_command(["wl-copy"], input_bytes)
            return
        if shutil.which("xclip"):
            if self.managed_owner:
                self._start_managed_owner(
                    ["xclip", "-quiet", "-selection", "clipboard"],
                    input_bytes,
                )
            else:
                self._run_command(["xclip", "-selection", "clipboard"], input_bytes)
            return
        if shutil.which("xsel"):
            self._run_command(["xsel", "--clipboard", "--input"], input_bytes)
            return
        raise ClipboardUnavailableError("No backend available for writing text to clipboard.")

    def _run_command(self, command: list[str], input_bytes: bytes) -> None:
        result = subprocess.run(command, input=input_bytes, capture_output=True, check=False)
        if result.returncode != 0:
            stderr = result.stderr.decode("utf-8", errors="replace").strip()
            raise ClipboardUnavailableError(stderr or f"Clipboard command failed: {' '.join(command)}")

    def _start_managed_owner(self, command: list[str], input_bytes: bytes) -> None:
        self.close()
        process = subprocess.Popen(
            command,
            stdin=subprocess.PIPE,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        try:
            assert process.stdin is not None
            process.stdin.write(input_bytes)
            process.stdin.close()
        except Exception:
            process.kill()
            process.wait(timeout=2)
            raise
        self._owner_process = process
        LOGGER.info("Started managed clipboard owner helper: %s", " ".join(command))


class PollingClipboardReader:
    """Read the current clipboard contents through shell helpers."""

    def __init__(self) -> None:
        self.source, self._reader = self._pick_reader()

    def read(self) -> ClipboardPayload:
        text = self._reader()
        return ClipboardPayload(
            kind="text" if text else "empty",
            created_at_ms=now_ms(),
            text=text or None,
            source="local",
        )

    def _pick_reader(self):
        session_type = os.environ.get("XDG_SESSION_TYPE", "").lower()
        if session_type == "wayland" and shutil.which("wl-paste"):
            return "wl-paste", lambda: self._read_command(["wl-paste", "--no-newline"])
        if shutil.which("xclip"):
            return "xclip", lambda: self._read_command(["xclip", "-selection", "clipboard", "-out"])
        if shutil.which("xsel"):
            return "xsel", lambda: self._read_command(["xsel", "--clipboard", "--output"])
        raise ClipboardUnavailableError("No clipboard backend found. Install wl-clipboard, xclip, or xsel.")

    def _read_command(self, command: list[str]) -> str:
        result = subprocess.run(command, capture_output=True, check=False)
        if result.returncode != 0:
            stderr = result.stderr.decode("utf-8", errors="replace").strip()
            raise ClipboardUnavailableError(stderr or f"Clipboard command failed: {' '.join(command)}")
        return result.stdout.decode("utf-8", errors="replace")


class X11ClipboardWatcher:
    """Receive X11 clipboard owner-change events and read text payloads."""

    SELECTION_NOTIFY = 31
    XFIXES_SELECTION_NOTIFY = 0
    XFIXES_SET_SELECTION_OWNER_NOTIFY_MASK = 1 << 0
    XFIXES_SELECTION_WINDOW_DESTROY_NOTIFY_MASK = 1 << 1
    XFIXES_SELECTION_CLIENT_CLOSE_NOTIFY_MASK = 1 << 2
    ANY_PROPERTY_TYPE = 0
    CURRENT_TIME = 0
    NONE = 0
    FALSE = 0
    TRUE = 1
    LONG_LENGTH = 1024 * 1024

    def __init__(self) -> None:
        if not os.environ.get("DISPLAY"):
            raise ClipboardUnavailableError("DISPLAY is not set.")

        x11_name = ctypes.util.find_library("X11")
        xfixes_name = ctypes.util.find_library("Xfixes")
        if not x11_name or not xfixes_name:
            raise ClipboardUnavailableError("libX11 or libXfixes is unavailable.")

        self._x11 = ctypes.cdll.LoadLibrary(x11_name)
        self._xfixes = ctypes.cdll.LoadLibrary(xfixes_name)
        self._configure_signatures()
        if self._x11.XInitThreads() == 0:
            raise ClipboardUnavailableError("XInitThreads failed.")

        self._display = self._x11.XOpenDisplay(None)
        if not self._display:
            raise ClipboardUnavailableError("Failed to open X11 display.")
        self._stopping = threading.Event()
        self._connection_fd = self._x11.XConnectionNumber(self._display)

        self._root = self._x11.XDefaultRootWindow(self._display)
        self._window = self._x11.XCreateSimpleWindow(self._display, self._root, 0, 0, 1, 1, 0, 0, 0)
        if not self._window:
            self.close()
            raise ClipboardUnavailableError("Failed to create X11 helper window.")

        event_base = ctypes.c_int()
        error_base = ctypes.c_int()
        if not self._xfixes.XFixesQueryExtension(
            self._display, ctypes.byref(event_base), ctypes.byref(error_base)
        ):
            self.close()
            raise ClipboardUnavailableError("XFixes extension is not available.")

        self.source = "x11-xfixes"
        self._xfixes_event_base = event_base.value
        self._clipboard_atom = self._intern_atom("CLIPBOARD")
        self._utf8_atom = self._intern_atom("UTF8_STRING")
        self._string_atom = self._intern_atom("STRING")
        self._incr_atom = self._intern_atom("INCR")
        self._property_atom = self._intern_atom("CLIPCOPY_PROPERTY")

        mask = (
            self.XFIXES_SET_SELECTION_OWNER_NOTIFY_MASK
            | self.XFIXES_SELECTION_WINDOW_DESTROY_NOTIFY_MASK
            | self.XFIXES_SELECTION_CLIENT_CLOSE_NOTIFY_MASK
        )
        self._xfixes.XFixesSelectSelectionInput(self._display, self._window, self._clipboard_atom, mask)
        self._x11.XFlush(self._display)

    def close(self) -> None:
        if getattr(self, "_display", None) and getattr(self, "_window", None):
            self._x11.XDestroyWindow(self._display, self._window)
            self._window = None
        if getattr(self, "_display", None):
            self._x11.XCloseDisplay(self._display)
            self._display = None

    def stop(self) -> None:
        self._stopping.set()

    def watch(self, on_change) -> None:
        previous = None
        current = self.read_clipboard()
        fingerprint = payload_fingerprint(current)
        if fingerprint != previous:
            on_change(current)
            previous = fingerprint

        while True:
            if self._stopping.is_set():
                return
            self._wait_for_clipboard_change()
            if self._stopping.is_set():
                return
            current = self.read_clipboard()
            fingerprint = payload_fingerprint(current)
            if fingerprint != previous:
                on_change(current)
                previous = fingerprint

    def read_clipboard(self) -> ClipboardPayload:
        owner = self._x11.XGetSelectionOwner(self._display, self._clipboard_atom)
        if owner == self.NONE:
            return ClipboardPayload(kind="empty", created_at_ms=now_ms(), source="local")

        for target in (self._utf8_atom, self._string_atom):
            value = self._request_selection(target)
            if value.actual_format != 8:
                continue
            text = value.data.decode("utf-8", errors="replace")
            return ClipboardPayload(
                kind="text" if text else "empty",
                created_at_ms=now_ms(),
                text=text or None,
                source="local",
            )

        return ClipboardPayload(kind="unknown", created_at_ms=now_ms(), source="local")

    def _wait_for_clipboard_change(self) -> None:
        # XNextEvent blocks indefinitely, so poll the X11 socket in short intervals
        # and check the stop flag between waits for a clean shutdown.
        while not self._stopping.is_set():
            event = self._next_event(timeout=0.25)
            if event is None:
                continue
            if self._is_clipboard_notify(event):
                return

    def _request_selection(self, target: int) -> PropertyValue:
        self._x11.XDeleteProperty(self._display, self._window, self._property_atom)
        self._x11.XConvertSelection(
            self._display,
            self._clipboard_atom,
            target,
            self._property_atom,
            self._window,
            self.CURRENT_TIME,
        )
        self._x11.XFlush(self._display)

        while not self._stopping.is_set():
            event = self._next_event(timeout=0.25)
            if event is None:
                continue
            if event.type != self.SELECTION_NOTIFY:
                continue
            selection_event = event.xselection
            if selection_event.selection != self._clipboard_atom:
                continue
            if selection_event.property == self.NONE:
                return PropertyValue(self.NONE, 0, 0, b"")
            return self._read_property(selection_event.property)
        raise ClipboardUnavailableError("Clipboard watcher is stopping.")

    def _read_property(self, property_atom: int) -> PropertyValue:
        actual_type = ctypes.c_ulong()
        actual_format = ctypes.c_int()
        item_count = ctypes.c_ulong()
        bytes_after = ctypes.c_ulong()
        raw_data = ctypes.c_void_p()

        status = self._x11.XGetWindowProperty(
            self._display,
            self._window,
            property_atom,
            0,
            self.LONG_LENGTH,
            self.TRUE,
            self.ANY_PROPERTY_TYPE,
            ctypes.byref(actual_type),
            ctypes.byref(actual_format),
            ctypes.byref(item_count),
            ctypes.byref(bytes_after),
            ctypes.byref(raw_data),
        )
        if status != 0:
            raise ClipboardUnavailableError(f"XGetWindowProperty failed with status {status}.")

        try:
            if actual_type.value == self.NONE or not raw_data.value:
                return PropertyValue(actual_type.value, actual_format.value, 0, b"")
            if actual_type.value == self._incr_atom:
                raise ClipboardUnavailableError("Large incremental clipboard transfers are not supported.")
            if actual_format.value == 8:
                data = ctypes.string_at(raw_data.value, item_count.value)
            elif actual_format.value == 16:
                data = ctypes.string_at(raw_data.value, item_count.value * 2)
            elif actual_format.value == 32:
                data = ctypes.string_at(raw_data.value, item_count.value * ctypes.sizeof(ctypes.c_ulong))
            else:
                data = b""
            return PropertyValue(actual_type.value, actual_format.value, item_count.value, data)
        finally:
            if raw_data.value:
                self._x11.XFree(raw_data)

    def _intern_atom(self, name: str) -> int:
        atom = self._x11.XInternAtom(self._display, name.encode("ascii"), self.FALSE)
        if atom == self.NONE:
            raise ClipboardUnavailableError(f"Failed to intern atom {name}.")
        return atom

    def _is_clipboard_notify(self, event: XEvent) -> bool:
        if event.type != self._xfixes_event_base + self.XFIXES_SELECTION_NOTIFY:
            return False
        return event.xfixes_selection.selection == self._clipboard_atom

    def _next_event(self, timeout: float | None = None) -> XEvent | None:
        while self._x11.XPending(self._display) == 0:
            if self._stopping.is_set():
                return None
            wait_timeout = 0.25 if timeout is None else timeout
            readable, _, _ = select.select([self._connection_fd], [], [], wait_timeout)
            if not readable:
                return None
        event = XEvent()
        self._x11.XNextEvent(self._display, ctypes.byref(event))
        return event

    def _configure_signatures(self) -> None:
        self._x11.XInitThreads.argtypes = []
        self._x11.XInitThreads.restype = ctypes.c_int
        self._x11.XOpenDisplay.argtypes = [ctypes.c_char_p]
        self._x11.XOpenDisplay.restype = ctypes.c_void_p
        self._x11.XConnectionNumber.argtypes = [ctypes.c_void_p]
        self._x11.XConnectionNumber.restype = ctypes.c_int
        self._x11.XDefaultRootWindow.argtypes = [ctypes.c_void_p]
        self._x11.XDefaultRootWindow.restype = ctypes.c_ulong
        self._x11.XPending.argtypes = [ctypes.c_void_p]
        self._x11.XPending.restype = ctypes.c_int
        self._x11.XCreateSimpleWindow.argtypes = [
            ctypes.c_void_p,
            ctypes.c_ulong,
            ctypes.c_int,
            ctypes.c_int,
            ctypes.c_uint,
            ctypes.c_uint,
            ctypes.c_uint,
            ctypes.c_ulong,
            ctypes.c_ulong,
        ]
        self._x11.XCreateSimpleWindow.restype = ctypes.c_ulong
        self._x11.XDestroyWindow.argtypes = [ctypes.c_void_p, ctypes.c_ulong]
        self._x11.XDestroyWindow.restype = ctypes.c_int
        self._x11.XCloseDisplay.argtypes = [ctypes.c_void_p]
        self._x11.XCloseDisplay.restype = ctypes.c_int
        self._x11.XFlush.argtypes = [ctypes.c_void_p]
        self._x11.XFlush.restype = ctypes.c_int
        self._x11.XInternAtom.argtypes = [ctypes.c_void_p, ctypes.c_char_p, ctypes.c_int]
        self._x11.XInternAtom.restype = ctypes.c_ulong
        self._x11.XGetSelectionOwner.argtypes = [ctypes.c_void_p, ctypes.c_ulong]
        self._x11.XGetSelectionOwner.restype = ctypes.c_ulong
        self._x11.XDeleteProperty.argtypes = [ctypes.c_void_p, ctypes.c_ulong, ctypes.c_ulong]
        self._x11.XDeleteProperty.restype = ctypes.c_int
        self._x11.XConvertSelection.argtypes = [
            ctypes.c_void_p,
            ctypes.c_ulong,
            ctypes.c_ulong,
            ctypes.c_ulong,
            ctypes.c_ulong,
            ctypes.c_ulong,
        ]
        self._x11.XConvertSelection.restype = ctypes.c_int
        self._x11.XGetWindowProperty.argtypes = [
            ctypes.c_void_p,
            ctypes.c_ulong,
            ctypes.c_ulong,
            ctypes.c_long,
            ctypes.c_long,
            ctypes.c_int,
            ctypes.c_ulong,
            ctypes.POINTER(ctypes.c_ulong),
            ctypes.POINTER(ctypes.c_int),
            ctypes.POINTER(ctypes.c_ulong),
            ctypes.POINTER(ctypes.c_ulong),
            ctypes.POINTER(ctypes.c_void_p),
        ]
        self._x11.XGetWindowProperty.restype = ctypes.c_int
        self._x11.XNextEvent.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
        self._x11.XNextEvent.restype = ctypes.c_int
        self._x11.XFree.argtypes = [ctypes.c_void_p]
        self._x11.XFree.restype = ctypes.c_int
        self._xfixes.XFixesQueryExtension.argtypes = [
            ctypes.c_void_p,
            ctypes.POINTER(ctypes.c_int),
            ctypes.POINTER(ctypes.c_int),
        ]
        self._xfixes.XFixesQueryExtension.restype = ctypes.c_int
        self._xfixes.XFixesSelectSelectionInput.argtypes = [
            ctypes.c_void_p,
            ctypes.c_ulong,
            ctypes.c_ulong,
            ctypes.c_ulong,
        ]
        self._xfixes.XFixesSelectSelectionInput.restype = None


def create_reader():
    session_type = os.environ.get("XDG_SESSION_TYPE", "").lower()
    if sys.platform.startswith("linux") and session_type == "x11":
        return X11ClipboardWatcher()
    return PollingClipboardReader()


async def fetch_server_payload_with_auth(args: argparse.Namespace) -> ClipboardPayload | None:
    websockets = ensure_websockets(args.no_auto_install)
    ssl_context = build_client_ssl_context(args)
    async with websockets.connect(server_url(args), max_size=None, ssl=ssl_context) as websocket:
        await send_hello(websocket, args, "paste", "paste-client")
        await websocket.send(json.dumps({"action": "get_latest"}))
        response = json.loads(await websocket.recv())
        if response.get("status") != "ok":
            raise RuntimeError(response.get("error", "Failed to fetch clipboard payload."))
        return ClipboardPayload.from_dict(response.get("payload"))


class LiveClipboardClient:
    """Maintain a persistent sync session with the server."""

    def __init__(self, args: argparse.Namespace, mode: str) -> None:
        self.args = args
        self.mode = mode
        self.url = server_url(args)
        self.client_id = uuid.uuid4().hex
        self.reader = create_reader()
        self.writer = CommandClipboardWriter(managed_owner=True)
        self.state_dir = Path(args.state_dir)
        self.outbound: "queue.Queue[ClipboardPayload | None]" = queue.Queue(maxsize=1)
        self.stop_event = threading.Event()
        self.reader_thread: threading.Thread | None = None
        self.reader_started = False
        self.reader_failure: Exception | None = None
        self.lock = threading.Lock()
        self.last_seen_fingerprint: str | None = None
        self.suppressed_remote_fingerprint: str | None = None

    def close(self) -> None:
        self.stop_event.set()
        replace_queue_item(self.outbound, None)
        if isinstance(self.reader, X11ClipboardWatcher):
            self.reader.stop()
        if self.reader_thread and self.reader_thread.is_alive():
            self.reader_thread.join(timeout=2)
        self.writer.close()
        if isinstance(self.reader, X11ClipboardWatcher):
            self.reader.close()

    def run(self) -> int:
        LOGGER.info("Starting client in %s mode", self.mode)
        LOGGER.info("Target server: %s", self.url)
        LOGGER.info("Clipboard mode: text-only")
        LOGGER.info("Press Ctrl+C to stop")
        self._start_reader_thread()
        asyncio.run(self._run_async())
        self._raise_reader_failure_if_any()
        return 0

    def _start_reader_thread(self) -> None:
        if self.reader_started:
            return
        self.reader_started = True
        self.reader_thread = threading.Thread(target=self._reader_loop, daemon=True, name="clipcopy-reader")
        self.reader_thread.start()

    def _read_local_payload(self) -> ClipboardPayload:
        if isinstance(self.reader, X11ClipboardWatcher):
            return self.reader.read_clipboard()
        return self.reader.read()

    def _raise_reader_failure_if_any(self) -> None:
        if self.reader_failure is not None:
            raise self.reader_failure

    def _reader_loop(self) -> None:
        try:
            initial = self._read_local_payload()
            with self.lock:
                self.last_seen_fingerprint = payload_fingerprint(initial)
            save_state(self.state_dir, initial)

            def handle(payload: ClipboardPayload) -> None:
                if self.stop_event.is_set():
                    return

                fingerprint = payload_fingerprint(payload)
                with self.lock:
                    if fingerprint == self.suppressed_remote_fingerprint:
                        self.suppressed_remote_fingerprint = None
                        self.last_seen_fingerprint = fingerprint
                        save_state(self.state_dir, payload)
                        return
                if fingerprint == self.last_seen_fingerprint:
                    return
                self.last_seen_fingerprint = fingerprint

                if payload.kind == "unknown":
                    LOGGER.info("Ignoring unsupported local clipboard payload")
                    return

                payload.created_at_ms = now_ms()
                payload.source = "local"
                save_state(self.state_dir, payload)
                print_payload("Detected local clipboard update", payload)
                replace_queue_item(self.outbound, payload)

            if isinstance(self.reader, X11ClipboardWatcher):
                self.reader.watch(handle)
                return

            while not self.stop_event.is_set():
                handle(self.reader.read())
                time.sleep(self.args.interval)
        except Exception as exc:
            if self.stop_event.is_set():
                return
            self.reader_failure = exc
            self.stop_event.set()
            replace_queue_item(self.outbound, None)
            LOGGER.exception("Clipboard reader failed: %s", exc)

    async def _run_async(self) -> None:
        websockets = ensure_websockets(self.args.no_auto_install)
        ssl_context = build_client_ssl_context(self.args)
        pending: ClipboardPayload | None = None

        while not self.stop_event.is_set():
            self._raise_reader_failure_if_any()
            try:
                async with websockets.connect(
                    self.url,
                    max_size=None,
                    ping_interval=20,
                    ping_timeout=20,
                    ssl=ssl_context,
                ) as websocket:
                    await send_hello(websocket, self.args, self.mode, self.client_id)
                    LOGGER.info("Connected to server")

                    # Only the receiver task reads from the socket. Acks for outbound
                    # messages are forwarded back to the sender through this queue.
                    ack_queue: asyncio.Queue = asyncio.Queue()
                    sender_task = asyncio.create_task(self._sender_loop(websocket, ack_queue, pending))
                    receiver_task = asyncio.create_task(self._receiver_loop(websocket, ack_queue))
                    done, pending_tasks = await asyncio.wait(
                        [sender_task, receiver_task],
                        return_when=asyncio.FIRST_EXCEPTION,
                    )
                    for task in pending_tasks:
                        task.cancel()
                    for task in done:
                        result = task.result()
                        if task is sender_task:
                            pending = result
                    self._raise_reader_failure_if_any()
            except KeyboardInterrupt:
                raise
            except Exception as exc:
                if self.stop_event.is_set():
                    break
                LOGGER.warning("Connection error: %s. Reconnecting in 2 seconds.", exc)
                await asyncio.sleep(2)

    async def _sender_loop(self, websocket, ack_queue: asyncio.Queue, pending: ClipboardPayload | None):
        current = pending
        while not self.stop_event.is_set():
            if current is None:
                current = await asyncio.to_thread(self.outbound.get)
            if current is None:
                return None

            try:
                await websocket.send(
                    json.dumps(
                        {"action": "put", "client_id": self.client_id, "payload": current.to_dict()},
                        ensure_ascii=False,
                    )
                )
                response = await ack_queue.get()
                if response.get("status") != "ok":
                    raise RuntimeError(response.get("error", "Server rejected clipboard payload."))
                LOGGER.info("Payload sent to server successfully")
                current = None
            except Exception as exc:
                LOGGER.warning("Failed to send clipboard payload: %s", exc)
                return current
        return current

    async def _receiver_loop(self, websocket, ack_queue: asyncio.Queue) -> None:
        try:
            while not self.stop_event.is_set():
                message = json.loads(await websocket.recv())
                if message.get("action") == "put" or message.get("status") == "error":
                    await ack_queue.put(message)
                    continue
                if message.get("event") != "updated":
                    continue
                if self.mode != "sync":
                    continue
                if message.get("client_id") == self.client_id:
                    continue

                payload = ClipboardPayload.from_dict(message.get("payload"))
                if payload is None:
                    continue
                if payload.kind not in {"text", "empty"}:
                    LOGGER.info("Ignoring unsupported remote clipboard payload kind: %s", payload.kind)
                    continue

                fingerprint = payload_fingerprint(payload)
                with self.lock:
                    if fingerprint == self.last_seen_fingerprint:
                        continue
                    self.suppressed_remote_fingerprint = fingerprint
                    self.last_seen_fingerprint = fingerprint

                self.writer.apply(payload)
                save_state(self.state_dir, payload)
                print_payload("Applied remote clipboard update", payload)
        except Exception as exc:
            await ack_queue.put({"status": "error", "error": str(exc)})
            raise


def resolve_local_payload_timestamp(reader, state_dir: Path) -> ClipboardPayload:
    payload = reader.read_clipboard() if isinstance(reader, X11ClipboardWatcher) else reader.read()
    state = load_state(state_dir)
    if state.get("fingerprint") == payload_fingerprint(payload):
        payload.created_at_ms = int(state.get("created_at_ms", payload.created_at_ms))
    return payload


def run_paste(args: argparse.Namespace) -> int:
    reader = create_reader()
    writer = CommandClipboardWriter(managed_owner=False)
    state_dir = Path(args.state_dir)
    try:
        local_payload = resolve_local_payload_timestamp(reader, state_dir)
        server_payload = asyncio.run(fetch_server_payload_with_auth(args))
        if server_payload is None:
            LOGGER.info("Server does not have a clipboard payload yet")
            return 0
        if server_payload.kind not in {"text", "empty"}:
            LOGGER.info("Server clipboard contains an unsupported payload kind: %s", server_payload.kind)
            return 0

        if local_payload.created_at_ms >= server_payload.created_at_ms:
            print_payload("Kept local clipboard because it is newer", local_payload)
            return 0

        writer.apply(server_payload)
        save_state(state_dir, server_payload)
        print_payload("Applied clipboard from server because it is newer", server_payload)
        return 0
    finally:
        writer.close()
        if isinstance(reader, X11ClipboardWatcher):
            reader.close()


def main() -> int:
    configure_logging()
    install_termination_handlers()
    args = resolve_client_settings(parse_args())
    maybe_install_linux_clipboard_tools(args.no_auto_install)

    try:
        if args.command == "paste":
            return run_paste(args)
        client = LiveClipboardClient(args, args.command)
        try:
            return client.run()
        finally:
            client.close()
    except KeyboardInterrupt:
        LOGGER.info("Client stopped by signal")
        return 0
    except ClipboardUnavailableError as exc:
        LOGGER.error("Clipboard access is unavailable: %s", exc)
        return 1
    except WebSocketUnavailableError as exc:
        LOGGER.error("WebSocket support is unavailable: %s", exc)
        return 1
    except Exception as exc:
        LOGGER.exception("Fatal client error: %s", exc)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
