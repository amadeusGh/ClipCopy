#!/usr/bin/env python3
"""Clipboard client — text-only sync over WebSockets (polling-based).

Single asyncio loop, no threads, no X11 event watcher. Reads the clipboard
via subprocess polling, sends changes to the server, and applies remote
updates in sync mode.
"""

from __future__ import annotations

import argparse
import asyncio
import importlib
import json
import os
import signal
import ssl
import subprocess
import sys
import time
import uuid
from pathlib import Path

from clipcopy_common import ClipboardPayload, now_ms, payload_fingerprint, payload_summary
from clipcopy_logging import configure_logging, get_logger


LOGGER = get_logger("clipcopy.client")


# ──────────────────────────────────── errors ────────────────────────────────────


class ClipboardUnavailableError(RuntimeError):
    """Raised when no usable clipboard backend is available."""


class WebSocketUnavailableError(RuntimeError):
    """Raised when the websockets dependency is missing."""


# ──────────────────────────── signal / arg / config ─────────────────────────────


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
        default=0.3,
        help="Polling interval in seconds (default: 0.3).",
    )

    sync_parser = subparsers.add_parser(
        "sync",
        help="Watch local clipboard, push to server, and apply remote updates.",
    )
    sync_parser.add_argument(
        "--interval",
        type=float,
        default=0.3,
        help="Polling interval in seconds (default: 0.3).",
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
    # Resolve relative paths against the config file directory so the
    # client works regardless of the current working directory.
    config_dir = Path(args.config).resolve().parent
    args.server_host = args.server_host or config.get("server_host") or "127.0.0.1"
    args.server_port = args.server_port or int(config.get("server_port", 8765))
    args.state_dir = str(
        Path(args.state_dir or config.get("state_dir") or ".clipcopy_state")
    )
    if not Path(args.state_dir).is_absolute():
        args.state_dir = str(config_dir / args.state_dir)
    args.auth_token = args.auth_token or config.get("auth_token")
    if not args.tls:
        args.tls = bool(config.get("tls", False))
    args.ca_cert = args.ca_cert or config.get("ca_cert")
    if args.ca_cert and not Path(args.ca_cert).is_absolute():
        args.ca_cert = str(config_dir / args.ca_cert)
    if not args.insecure_tls:
        args.insecure_tls = bool(config.get("insecure_tls", False))
    if hasattr(args, "interval") and args.interval == 0.3 and config.get("interval") is not None:
        args.interval = float(config["interval"])
    return args


# ────────────────────────── dependency helpers ────────────────────────────────


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

    import shutil

    if shutil.which(binary) or not shutil.which("apt-get") or not sys.stdin.isatty():
        return

    LOGGER.info("Installing missing clipboard backend package: %s", package)
    subprocess.run(["sudo", "apt-get", "install", "-y", package], check=False)


# ───────────────────────────── state persistence ───────────────────────────────


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


# ────────────────────────────── network helpers ────────────────────────────────


def server_url(args: argparse.Namespace) -> str:
    scheme = "wss" if args.tls else "ws"
    return f"{scheme}://{args.server_host}:{args.server_port}"


def build_client_ssl_context(args: argparse.Namespace) -> ssl.SSLContext | None:
    if not args.tls:
        return None
    if args.insecure_tls:
        return ssl._create_unverified_context()
    if not args.ca_cert:
        raise RuntimeError(
            "TLS is enabled but no CA certificate path is configured."
        )
    if not Path(args.ca_cert).exists():
        raise FileNotFoundError(
            f"TLS CA certificate not found: {args.ca_cert}\n"
            "Run the install script to fetch it, copy it from the server, "
            "or set insecure_tls: true in the client config."
        )
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


# ──────────────────────────── clipboard read/write ─────────────────────────────


class ClipboardReader:
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
        import shutil

        session_type = os.environ.get("XDG_SESSION_TYPE", "").lower()
        if session_type == "wayland" and shutil.which("wl-paste"):
            return "wl-paste", lambda: self._read_command(["wl-paste", "--no-newline"])
        if shutil.which("xclip"):
            return "xclip", self._read_xclip
        if shutil.which("xsel"):
            return "xsel", lambda: self._read_command(["xsel", "--clipboard", "--output"])
        raise ClipboardUnavailableError("No clipboard backend found. Install wl-clipboard, xclip, or xsel.")

    @staticmethod
    def _read_command(command: list[str]) -> str:
        result = subprocess.run(command, capture_output=True, check=False)
        if result.returncode != 0:
            raise ClipboardUnavailableError(
                result.stderr.decode("utf-8", errors="replace").strip()
                or f"Clipboard command failed: {' '.join(command)}"
            )
        return result.stdout.decode("utf-8", errors="replace")

    def _read_xclip(self) -> str:
        commands = [
            ["xclip", "-selection", "clipboard", "-target", "UTF8_STRING", "-out"],
            ["xclip", "-selection", "clipboard", "-target", "text/plain;charset=utf-8", "-out"],
            ["xclip", "-selection", "clipboard", "-target", "TEXT", "-out"],
            ["xclip", "-selection", "clipboard", "-out"],
        ]
        errors: list[str] = []
        for command in commands:
            try:
                return self._read_command(command)
            except ClipboardUnavailableError as exc:
                errors.append(str(exc))
        raise ClipboardUnavailableError("; ".join(errors))


class ClipboardWriter:
    """Write text payloads to the local clipboard via one-shot subprocess."""

    def __init__(self) -> None:
        self.session_type = os.environ.get("XDG_SESSION_TYPE", "").lower()

    def apply(self, payload: ClipboardPayload) -> None:
        if payload.kind == "empty":
            self._write_text("")
            return
        if payload.kind != "text":
            raise ClipboardUnavailableError(f"Unsupported clipboard kind: {payload.kind}")
        self._write_text(payload.text or "")

    def _write_text(self, text: str) -> None:
        import shutil

        input_bytes = text.encode("utf-8")
        if self.session_type == "wayland" and shutil.which("wl-copy"):
            self._run_command(["wl-copy"], input_bytes)
            return
        if shutil.which("xclip"):
            self._run_command(["xclip", "-selection", "clipboard"], input_bytes)
            return
        if shutil.which("xsel"):
            self._run_command(["xsel", "--clipboard", "--input"], input_bytes)
            return
        raise ClipboardUnavailableError("No backend available for writing text to clipboard.")

    @staticmethod
    def _run_command(command: list[str], input_bytes: bytes) -> None:
        # DEVNULL, not PIPE — xclip forks a child that inherits fds;
        # captured pipes would survive subprocess.run, and a write to a
        # broken pipe (SIGPIPE) kills the orphaned clipboard owner.
        result = subprocess.run(
            command,
            input=input_bytes,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )
        if result.returncode != 0:
            raise ClipboardUnavailableError(
                f"Clipboard command failed (exit {result.returncode}): {' '.join(command)}"
            )


# ────────────────────────────── main client class ──────────────────────────────


class ClipboardClient:
    """Polling-based clipboard client — single asyncio loop, no threads."""

    def __init__(self, args: argparse.Namespace, mode: str) -> None:
        self.args = args
        self.mode = mode
        self.url = server_url(args)
        self.client_id = uuid.uuid4().hex
        self.reader = ClipboardReader()
        self.writer = ClipboardWriter()
        self.state_dir = Path(args.state_dir)
        self.last_fingerprint: str | None = None
        self._stop_event = asyncio.Event()
        self._clipboard_lock = asyncio.Lock()

    async def run(self) -> None:
        LOGGER.info("Starting client in %s mode", self.mode)
        LOGGER.info("Target server: %s", self.url)
        LOGGER.info("Clipboard mode: text-only (polling every %.1fs)", self.args.interval)
        LOGGER.info("Press Ctrl+C to stop")

        state = load_state(self.state_dir)
        if state.get("fingerprint"):
            self.last_fingerprint = state["fingerprint"]

        while not self._stop_event.is_set():
            try:
                await self._connect_and_run()
            except KeyboardInterrupt:
                raise
            except Exception as exc:
                if self._stop_event.is_set():
                    break
                LOGGER.warning("Connection error: %s. Reconnecting in 2 seconds.", exc)
                await asyncio.sleep(2)

    async def _connect_and_run(self) -> None:
        websockets = ensure_websockets(self.args.no_auto_install)
        ssl_context = build_client_ssl_context(self.args)

        async with websockets.connect(
            self.url,
            max_size=None,
            ping_interval=20,
            ping_timeout=20,
            ssl=ssl_context,
        ) as ws:
            await send_hello(ws, self.args, self.mode, self.client_id)
            LOGGER.info("Connected to server")

            ack_queue: asyncio.Queue = asyncio.Queue()
            sender_task = asyncio.create_task(self._sender_loop(ws, ack_queue))
            receiver_task = asyncio.create_task(self._receiver_loop(ws, ack_queue))

            done, pending = await asyncio.wait(
                [sender_task, receiver_task],
                return_when=asyncio.FIRST_EXCEPTION,
            )
            for task in pending:
                task.cancel()
            for task in done:
                task.result()  # propagate first exception

    # ── sender ──────────────────────────────────────────────────────────────

    async def _sender_loop(self, ws, ack_queue: asyncio.Queue) -> None:
        """Poll local clipboard and push changes to the server."""
        while not self._stop_event.is_set():
            try:
                await asyncio.sleep(self.args.interval)

                async with self._clipboard_lock:
                    try:
                        payload = await asyncio.to_thread(self.reader.read)
                    except ClipboardUnavailableError as exc:
                        LOGGER.warning(
                            "Skipping clipboard poll because backend read failed: %s",
                            exc,
                        )
                        continue

                    fingerprint = payload_fingerprint(payload)
                    if fingerprint == self.last_fingerprint:
                        continue

                    if payload.kind in ("text", "empty"):
                        print_payload("Detected local clipboard update", payload)

                    # Keep the clipboard lock until the server acknowledges the
                    # payload. This preserves ordering with remote applies and
                    # prevents a failed send from being marked as delivered.
                    await ws.send(
                        json.dumps(
                            {
                                "action": "put",
                                "client_id": self.client_id,
                                "payload": payload.to_dict(),
                            },
                            ensure_ascii=False,
                        )
                    )
                    response = await ack_queue.get()
                    if response.get("status") != "ok":
                        raise RuntimeError(
                            response.get("error", "Server rejected clipboard payload.")
                        )

                    self.last_fingerprint = fingerprint
                    save_state(self.state_dir, payload)

                LOGGER.info("Payload sent to server successfully")
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                LOGGER.warning("Failed to send clipboard payload: %s", exc)
                raise

    # ── receiver ────────────────────────────────────────────────────────────

    async def _receiver_loop(self, ws, ack_queue: asyncio.Queue) -> None:
        """Receive server messages: forward acks to sender, apply remote updates."""
        try:
            while not self._stop_event.is_set():
                message = json.loads(await ws.recv())

                # Route put responses and errors to the sender task.
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
                if payload.kind not in ("text", "empty"):
                    LOGGER.info("Ignoring unsupported remote clipboard payload kind: %s", payload.kind)
                    continue

                fingerprint = payload_fingerprint(payload)

                async with self._clipboard_lock:
                    if fingerprint == self.last_fingerprint:
                        continue

                    await asyncio.to_thread(self.writer.apply, payload)
                    self.last_fingerprint = fingerprint
                    save_state(self.state_dir, payload)

                print_payload("Applied remote clipboard update", payload)
        except Exception as exc:
            # Make sure the sender unblocks if it is waiting for an ack.
            await ack_queue.put({"status": "error", "error": str(exc)})
            raise


# ────────────────────────────────── paste mode ──────────────────────────────────


async def fetch_server_payload_with_auth(args: argparse.Namespace) -> ClipboardPayload | None:
    websockets = ensure_websockets(args.no_auto_install)
    ssl_context = build_client_ssl_context(args)
    async with websockets.connect(server_url(args), max_size=None, ssl=ssl_context) as ws:
        await send_hello(ws, args, "paste", "paste-client")
        await ws.send(json.dumps({"action": "get_latest"}))
        response = json.loads(await ws.recv())
        if response.get("status") != "ok":
            raise RuntimeError(response.get("error", "Failed to fetch clipboard payload."))
        return ClipboardPayload.from_dict(response.get("payload"))


def run_paste(args: argparse.Namespace) -> int:
    reader = ClipboardReader()
    writer = ClipboardWriter()
    state_dir = Path(args.state_dir)

    local_payload = reader.read()
    state = load_state(state_dir)
    if state.get("fingerprint") == payload_fingerprint(local_payload):
        local_payload.created_at_ms = int(state.get("created_at_ms", local_payload.created_at_ms))

    if local_payload.kind not in ("text", "empty"):
        print_payload("Local clipboard is unsupported", local_payload)
        return 0

    server_payload = asyncio.run(fetch_server_payload_with_auth(args))
    if server_payload is None:
        LOGGER.info("Server does not have a clipboard payload yet")
        return 0
    if server_payload.kind not in ("text", "empty"):
        LOGGER.info("Server clipboard contains an unsupported payload kind: %s", server_payload.kind)
        return 0

    if local_payload.created_at_ms >= server_payload.created_at_ms:
        print_payload("Kept local clipboard because it is newer", local_payload)
        return 0

    writer.apply(server_payload)
    save_state(state_dir, server_payload)
    print_payload("Applied clipboard from server because it is newer", server_payload)
    return 0


# ──────────────────────────────────── entrypoint ────────────────────────────────


def main() -> int:
    configure_logging()
    install_termination_handlers()
    args = resolve_client_settings(parse_args())
    maybe_install_linux_clipboard_tools(args.no_auto_install)

    try:
        if args.command == "paste":
            return run_paste(args)

        client = ClipboardClient(args, args.command)
        try:
            asyncio.run(client.run())
        except KeyboardInterrupt:
            LOGGER.info("Client stopped by signal")
        return 0
    except KeyboardInterrupt:
        LOGGER.info("Client stopped by signal")
        return 0
    except ClipboardUnavailableError as exc:
        LOGGER.error("Clipboard access is unavailable: %s", exc)
        return 1
    except WebSocketUnavailableError as exc:
        LOGGER.error("WebSocket support is unavailable: %s", exc)
        return 1
    except FileNotFoundError as exc:
        LOGGER.error("%s", exc)
        return 1
    except Exception as exc:
        LOGGER.exception("Fatal client error: %s", exc)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
