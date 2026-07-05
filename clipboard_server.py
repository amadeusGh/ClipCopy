#!/usr/bin/env python3
"""WebSocket server for text clipboard synchronization."""

from __future__ import annotations

import argparse
import asyncio
import hmac
import importlib
import ipaddress
import json
import shutil
import signal
import ssl
import subprocess
import sys
from pathlib import Path

from clipcopy_common import ClipboardPayload
from clipcopy_logging import configure_logging, get_logger


class WebSocketUnavailableError(RuntimeError):
    """Raised when the websockets dependency is missing."""


LOGGER = get_logger("clipcopy.server")


def install_termination_handlers() -> None:
    """Translate SIGINT/SIGTERM into KeyboardInterrupt for clean shutdown."""

    def handler(signum, frame):
        raise KeyboardInterrupt()

    signal.signal(signal.SIGINT, handler)
    signal.signal(signal.SIGTERM, handler)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Store and broadcast text clipboard payloads over WebSockets."
    )
    parser.add_argument(
        "--config",
        default="server_config.json",
        help="Path to server JSON config. Default: server_config.json",
    )
    parser.add_argument("--host", default=None, help="Bind host.")
    parser.add_argument("--port", type=int, default=None, help="Bind port.")
    parser.add_argument(
        "--storage-dir",
        default=None,
        help="Directory used to persist the latest clipboard payload.",
    )
    parser.add_argument("--auth-token", default=None, help="Shared token required from clients.")
    parser.add_argument("--tls-cert", default=None, help="TLS certificate path for WSS.")
    parser.add_argument("--tls-key", default=None, help="TLS private key path for WSS.")
    parser.add_argument(
        "--no-auto-install",
        action="store_true",
        help="Disable best-effort installation of Python dependencies.",
    )
    return parser.parse_args()


def load_server_config(path: str) -> dict:
    config_path = Path(path)
    if not config_path.exists():
        return {}
    return json.loads(config_path.read_text(encoding="utf-8"))


def resolve_server_settings(args: argparse.Namespace) -> argparse.Namespace:
    config = load_server_config(args.config)
    args.host = args.host or config.get("host") or "0.0.0.0"
    args.port = args.port or int(config.get("port", 8765))
    args.storage_dir = args.storage_dir or config.get("storage_dir") or "server_storage"
    args.auth_token = args.auth_token or config.get("auth_token")
    args.tls_cert = args.tls_cert or config.get("tls_cert")
    args.tls_key = args.tls_key or config.get("tls_key")
    args.tls_common_name = config.get("tls_common_name") or "localhost"
    args.tls_subject_alt_names = list(config.get("tls_subject_alt_names", []))
    return args


def ensure_self_signed_certificate(args: argparse.Namespace) -> None:
    cert_path = Path(args.tls_cert)
    key_path = Path(args.tls_key)
    if cert_path.exists() and key_path.exists():
        return

    if not shutil.which("openssl"):
        raise RuntimeError("TLS certificate/key is missing and openssl is not installed.")

    cert_path.parent.mkdir(parents=True, exist_ok=True)
    key_path.parent.mkdir(parents=True, exist_ok=True)

    san_entries = ["DNS:localhost", "IP:127.0.0.1"]
    for entry in getattr(args, "tls_subject_alt_names", []) or []:
        if isinstance(entry, str) and entry:
            san_entries.append(entry)
    host_value = args.host or "localhost"
    if host_value not in {"0.0.0.0", "::", "localhost", "127.0.0.1"}:
        try:
            ipaddress.ip_address(host_value)
        except ValueError:
            san_entries.append(f"DNS:{host_value}")
        else:
            san_entries.append(f"IP:{host_value}")

    command = [
        "openssl",
        "req",
        "-x509",
        "-newkey",
        "rsa:2048",
        "-sha256",
        "-nodes",
        "-days",
        "3650",
        "-keyout",
        str(key_path),
        "-out",
        str(cert_path),
        "-subj",
        f"/CN={args.tls_common_name}",
        "-addext",
        f"subjectAltName={','.join(san_entries)}",
    ]
    LOGGER.info("Generating self-signed TLS certificate at %s", cert_path)
    result = subprocess.run(command, check=False, capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(
            f"Failed to generate self-signed certificate: {result.stderr.strip()}"
        )


def build_server_ssl_context(args: argparse.Namespace) -> ssl.SSLContext | None:
    if not args.tls_cert and not args.tls_key:
        return None
    if not args.tls_cert or not args.tls_key:
        raise RuntimeError("Both tls_cert and tls_key must be configured for TLS.")

    ensure_self_signed_certificate(args)
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(certfile=args.tls_cert, keyfile=args.tls_key)
    return context


def ensure_websockets(no_auto_install: bool):
    try:
        return importlib.import_module("websockets")
    except ImportError as exc:
        if no_auto_install:
            raise WebSocketUnavailableError(
                "Python package 'websockets' is not installed."
            ) from exc

    LOGGER.info("Installing missing Python dependency: websockets")
    result = subprocess.run(
        [sys.executable, "-m", "pip", "install", "--user", "websockets"],
        check=False,
    )
    if result.returncode != 0:
        raise WebSocketUnavailableError("Failed to install Python package 'websockets'.")
    return importlib.import_module("websockets")


class ClipboardStore:
    """Persist the latest clipboard payload on disk."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.auth_token: str | None = None
        self.latest_file = root / "latest.json"
        self.root.mkdir(parents=True, exist_ok=True)

    def save(self, payload: ClipboardPayload) -> None:
        self.latest_file.write_text(
            json.dumps(payload.to_dict(), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    def load_latest(self) -> ClipboardPayload | None:
        if not self.latest_file.exists():
            return None
        payload = ClipboardPayload.from_dict(
            json.loads(self.latest_file.read_text(encoding="utf-8"))
        )
        if payload is None:
            return None
        try:
            return validate_payload(payload)
        except ValueError as exc:
            LOGGER.warning("Ignoring unsupported payload stored on disk: %s", exc)
            return None


def validate_payload(payload: ClipboardPayload) -> ClipboardPayload:
    """Accept only the supported text-only payload shapes."""

    if payload.kind not in {"text", "empty"}:
        raise ValueError(f"Unsupported clipboard payload kind: {payload.kind}")

    if payload.kind == "empty":
        payload.text = None
        return payload

    if payload.text is None:
        payload.text = ""
    return payload


async def broadcast_payload(
    clients: dict,
    payload: ClipboardPayload,
    source_client_id: str | None,
) -> None:
    if not clients:
        return

    message = json.dumps(
        {
            "event": "updated",
            "payload": payload.to_dict(),
            "client_id": source_client_id,
        },
        ensure_ascii=False,
    )
    dead = []
    for websocket, meta in list(clients.items()):
        if meta.get("mode") != "sync":
            continue
        if source_client_id and meta.get("client_id") == source_client_id:
            continue
        try:
            await websocket.send(message)
        except Exception:
            dead.append(websocket)

    for websocket in dead:
        clients.pop(websocket, None)
    if dead:
        LOGGER.warning("Dropped %d disconnected subscriber(s) during broadcast", len(dead))


async def handle_connection(websocket, store: ClipboardStore, clients: dict) -> None:
    clients[websocket] = {"client_id": None, "mode": "watch", "authenticated": False}
    auth_required = bool(store.auth_token)
    peer = getattr(websocket, "remote_address", None)
    LOGGER.info("Client connected: %s", peer)
    try:
        async for message in websocket:
            try:
                request = json.loads(message)
                action = request.get("action")
                client_id = request.get("client_id")
                if client_id:
                    clients[websocket]["client_id"] = str(client_id)

                if action == "hello":
                    mode = str(request.get("mode", "watch"))
                    provided_token = request.get("auth_token") or ""
                    if auth_required and not hmac.compare_digest(provided_token, store.auth_token):
                        LOGGER.warning("Authentication failed for client %s (%s)", client_id, peer)
                        await websocket.send(json.dumps({"status": "error", "error": "Invalid auth token."}))
                        await websocket.close()
                        return
                    clients[websocket]["mode"] = mode
                    clients[websocket]["authenticated"] = True
                    LOGGER.info(
                        "Client authenticated: id=%s mode=%s peer=%s",
                        client_id,
                        mode,
                        peer,
                    )
                    await websocket.send(json.dumps({"status": "ok", "action": "hello"}))
                    continue

                if auth_required and not clients[websocket]["authenticated"]:
                    LOGGER.warning("Unauthenticated request rejected from %s", peer)
                    await websocket.send(json.dumps({"status": "error", "error": "Authentication required."}))
                    await websocket.close()
                    return

                if action == "put":
                    payload = ClipboardPayload.from_dict(request.get("payload"))
                    if payload is None:
                        raise ValueError("Missing payload.")
                    payload = validate_payload(payload)
                    store.save(payload)
                    LOGGER.info(
                        "Stored payload from client=%s kind=%s timestamp=%s",
                        client_id,
                        payload.kind,
                        payload.created_at_ms,
                    )
                    await websocket.send(json.dumps({"status": "ok", "action": "put"}))
                    await broadcast_payload(clients, payload, client_id)
                    continue

                if action == "get_latest":
                    payload = store.load_latest()
                    LOGGER.info("Serving latest payload to client=%s", client_id)
                    await websocket.send(
                        json.dumps(
                            {
                                "status": "ok",
                                "payload": payload.to_dict() if payload else None,
                            },
                            ensure_ascii=False,
                        )
                    )
                    continue

                await websocket.send(
                    json.dumps({"status": "error", "error": f"Unknown action: {action}"})
                )
            except Exception as exc:
                LOGGER.exception("Failed to handle client message from %s", peer)
                await websocket.send(json.dumps({"status": "error", "error": str(exc)}))
    finally:
        clients.pop(websocket, None)
        LOGGER.info("Client disconnected: %s", peer)


async def run_server(args: argparse.Namespace) -> None:
    websockets = ensure_websockets(args.no_auto_install)
    ssl_context = build_server_ssl_context(args)
    store = ClipboardStore(Path(args.storage_dir))
    store.auth_token = args.auth_token
    clients: dict = {}

    async def handler(websocket):
        await handle_connection(websocket, store, clients)

    async with websockets.serve(handler, args.host, args.port, max_size=None, ssl=ssl_context):
        scheme = "wss" if ssl_context else "ws"
        LOGGER.info("Clipboard server listening on %s://%s:%s", scheme, args.host, args.port)
        LOGGER.info("Storage directory: %s", Path(args.storage_dir).resolve())
        LOGGER.info("Authentication required: %s", "yes" if args.auth_token else "no")
        LOGGER.info("TLS enabled: %s", "yes" if ssl_context else "no")
        LOGGER.info("Clipboard mode: text-only")
        await asyncio.Future()


def main() -> int:
    configure_logging()
    install_termination_handlers()
    args = resolve_server_settings(parse_args())
    try:
        asyncio.run(run_server(args))
        return 0
    except KeyboardInterrupt:
        LOGGER.info("Server stopped by signal")
        return 0
    except WebSocketUnavailableError as exc:
        LOGGER.error("WebSocket support is unavailable: %s", exc)
        return 1
    except Exception as exc:
        LOGGER.exception("Fatal server error: %s", exc)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
