# ClipCopy

<p align="center">
  <img src="assets/banner.png" alt="ClipCopy banner" width="800">
</p>

<p align="center">
  <b>Shared clipboard for Linux.</b> Ctrl+C on one machine → Ctrl+V on any other.<br>
  Encrypted, authenticated, runs invisibly in the background.
</p>

<p align="center">
  <img src="https://img.shields.io/badge/python-3.10+-blue" alt="Python">
  <img src="https://img.shields.io/badge/platform-Linux-lightgrey" alt="Linux">
  <img src="https://img.shields.io/badge/license-MIT-green" alt="MIT">
</p>

---

## Architecture

```
┌──────────────┐      wss://       ┌──────────────────┐      wss://      ┌──────────────┐
│  Client A    │ ──── TLS ───────→ │  Relay Server    │ ←──── TLS ───── │  Client B    │
│  Ctrl+C      │                   │  stores + relays │                  │  Ctrl+V      │
└──────────────┘                   └──────────────────┘                  └──────────────┘
```

- **Server** stores the latest clipboard and broadcasts to every connected client *except* the sender.
- **Clients** run in `sync` mode: prefer X11 clipboard events via XFixes and fall back to polling when needed.
- **Anti-echo** prevents the client from re-sending payloads it just received.

## Quick Start

```bash
git clone https://github.com/amadeusGh/ClipCopy && cd ClipCopy
chmod +x scripts/*.sh
```

### Server (one machine, e.g. VPS)

```bash
./scripts/install_server.sh --public-host your-server.com
```

### Client (every desktop)

```bash
./scripts/install_client.sh \
  --server-host your-server.com \
  --token "the-token-from-server-install"
```

**Done.** Copy text on any client — paste it on any other.

---

## Manual Usage (skip installers)

```bash
pip install -r requirements.txt
cp server_config.example.json server_config.json    # edit auth_token + host
cp client_config.example.json client_config.json    # edit server_host + token
```

**Server:**
```bash
python3 clipboard_server.py --config server_config.json
```

**Client (sync mode — normal use):**
```bash
python3 clipboard_watch.py --config client_config.json sync
```

**Client (watch-only — send, never receive):**
```bash
python3 clipboard_watch.py --config client_config.json watch
```

---

## Security

| Layer | How |
|---|---|
| **Authentication** | Shared token in `hello` handshake. Wrong token → disconnect. |
| **Encryption** | TLS (`wss://`). Self-signed cert auto-generated on server, fetched by client installer. |
| **Anti-echo** | Server tags every relay with `client_id`; each client ignores its own relayed payloads. |

---

## Systemd (background service)

Installers set up `systemd --user` units automatically.

```bash
systemctl --user status clipcopy-server.service
systemctl --user status clipcopy-client.service
journalctl --user -u clipcopy-client.service -f
```

---

## Requirements

- Linux (X11 for event-driven; Wayland needs `wl-clipboard`)
- Python 3.10+ + `websockets`
- `openssl` (for TLS cert generation)

---

## Repository

| File | Role |
|---|---|
| `clipboard_server.py` | WebSocket relay server |
| `clipboard_watch.py` | Client: watch + sync + paste modes |
| `clipcopy_common.py` | Shared payload models |
| `clipcopy_logging.py` | Structured logging |
| `scripts/install_server.sh` | Automated server install |
| `scripts/install_client.sh` | Automated client install |
| `systemd-user/` | Service unit templates |

---

## License

MIT
