#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"
VENV_DIR="${PROJECT_DIR}/.venv"
CONFIG_PATH="${PROJECT_DIR}/server_config.json"
STATE_DIR="${PROJECT_DIR}/.clipcopy_state"
STORAGE_DIR="${PROJECT_DIR}/server_storage"
HOST="0.0.0.0"
PORT="8765"
TOKEN=""
TLS_COMMON_NAME="localhost"
PUBLIC_HOST=""
ENABLE_SERVICE=1
START_SERVICE=1

usage() {
  cat <<'EOF'
Usage: ./scripts/install_server.sh [options]

Options:
  --host <host>                Bind host. Default: 0.0.0.0
  --port <port>                Bind port. Default: 8765
  --token <token>              Shared authentication token. Generated if omitted.
  --public-host <host-or-ip>   Hostname or IP to include in the self-signed certificate SAN.
  --tls-common-name <name>     Certificate common name. Default: localhost
  --no-service                 Do not install the systemd user service
  --no-start                   Do not start the service after installation
  -h, --help                   Show this help message
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --host)
      HOST="$2"
      shift 2
      ;;
    --port)
      PORT="$2"
      shift 2
      ;;
    --token)
      TOKEN="$2"
      shift 2
      ;;
    --public-host)
      PUBLIC_HOST="$2"
      shift 2
      ;;
    --tls-common-name)
      TLS_COMMON_NAME="$2"
      shift 2
      ;;
    --no-service)
      ENABLE_SERVICE=0
      shift
      ;;
    --no-start)
      START_SERVICE=0
      shift
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      echo "Unknown argument: $1" >&2
      usage >&2
      exit 1
      ;;
  esac
done

if [[ -z "${TOKEN}" ]]; then
  TOKEN="$(python3 -c 'import secrets; print(secrets.token_urlsafe(32))')"
fi

mkdir -p "${STATE_DIR}/tls" "${STORAGE_DIR}" "${PROJECT_DIR}/systemd-user"

python3 -m venv "${VENV_DIR}"
"${VENV_DIR}/bin/pip" install --upgrade pip >/dev/null
"${VENV_DIR}/bin/pip" install -r "${PROJECT_DIR}/requirements.txt"

export CONFIG_PATH HOST PORT STORAGE_DIR TOKEN TLS_COMMON_NAME PUBLIC_HOST STATE_DIR
python3 - <<'PY'
import json
import os
from pathlib import Path

config_path = Path(os.environ["CONFIG_PATH"])
state_dir = Path(os.environ["STATE_DIR"])
public_host = os.environ["PUBLIC_HOST"].strip()

subject_alt_names = []
if public_host:
    try:
        import ipaddress

        ipaddress.ip_address(public_host)
    except ValueError:
        subject_alt_names.append(f"DNS:{public_host}")
    else:
        subject_alt_names.append(f"IP:{public_host}")

config = {
    "host": os.environ["HOST"],
    "port": int(os.environ["PORT"]),
    "storage_dir": os.environ["STORAGE_DIR"],
    "auth_token": os.environ["TOKEN"],
    "tls_cert": str(state_dir / "tls" / "clipcopy.crt"),
    "tls_key": str(state_dir / "tls" / "clipcopy.key"),
    "tls_common_name": os.environ["TLS_COMMON_NAME"],
    "tls_subject_alt_names": subject_alt_names,
}
config_path.write_text(json.dumps(config, indent=2), encoding="utf-8")
PY

if [[ "${ENABLE_SERVICE}" -eq 1 ]]; then
  mkdir -p "${HOME}/.config/systemd/user"
  export PROJECT_DIR VENV_DIR
  python3 - <<'PY'
import os
from pathlib import Path

project_dir = Path(os.environ["PROJECT_DIR"])
venv_dir = Path(os.environ["VENV_DIR"])
template_path = project_dir / "systemd-user" / "clipcopy-server.service.template"
target_path = Path.home() / ".config" / "systemd" / "user" / "clipcopy-server.service"
content = template_path.read_text(encoding="utf-8")
content = content.replace("@PROJECT_DIR@", str(project_dir))
content = content.replace("@PYTHON_BIN@", str(venv_dir / "bin" / "python"))
target_path.write_text(content, encoding="utf-8")
PY

  systemctl --user daemon-reload
  systemctl --user enable clipcopy-server.service
  if [[ "${START_SERVICE}" -eq 1 ]]; then
    systemctl --user restart clipcopy-server.service
  fi
fi

cat <<EOF

ClipCopy server installation complete.

Project directory: ${PROJECT_DIR}
Virtual environment: ${VENV_DIR}
Config file: ${CONFIG_PATH}
Authentication token: ${TOKEN}
Clipboard mode: text-only

If you installed the service, inspect it with:
  systemctl --user status clipcopy-server.service
  journalctl --user -u clipcopy-server.service -f

Share this token with every client that should connect to the server.
EOF
