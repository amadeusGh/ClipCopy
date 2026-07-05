#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"
VENV_DIR="${PROJECT_DIR}/.venv"
CONFIG_PATH="${PROJECT_DIR}/client_config.json"
STATE_DIR="${PROJECT_DIR}/.clipcopy_state"
SERVER_HOST="127.0.0.1"
SERVER_PORT="8765"
TOKEN=""
TLS_ENABLED=1
INSECURE_TLS=0
FETCH_CERT=1
ENABLE_SERVICE=1
START_SERVICE=1

usage() {
  cat <<'EOF'
Usage: ./scripts/install_client.sh [options]

Options:
  --server-host <host>         Target ClipCopy server host. Default: 127.0.0.1
  --server-port <port>         Target ClipCopy server port. Default: 8765
  --token <token>              Shared authentication token. Required.
  --no-tls                     Connect without TLS
  --insecure-tls               Skip certificate verification (testing only)
  --no-fetch-cert              Do not fetch the server certificate automatically
  --no-service                 Do not install the systemd user service
  --no-start                   Do not start the service after installation
  -h, --help                   Show this help message
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --server-host)
      SERVER_HOST="$2"
      shift 2
      ;;
    --server-port)
      SERVER_PORT="$2"
      shift 2
      ;;
    --token)
      TOKEN="$2"
      shift 2
      ;;
    --no-tls)
      TLS_ENABLED=0
      shift
      ;;
    --insecure-tls)
      INSECURE_TLS=1
      shift
      ;;
    --no-fetch-cert)
      FETCH_CERT=0
      shift
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
  echo "--token is required" >&2
  exit 1
fi

mkdir -p "${STATE_DIR}/tls" "${PROJECT_DIR}/systemd-user"

python3 -m venv "${VENV_DIR}"
"${VENV_DIR}/bin/pip" install --upgrade pip >/dev/null
"${VENV_DIR}/bin/pip" install -r "${PROJECT_DIR}/requirements.txt"

CA_CERT_PATH="${STATE_DIR}/tls/clipcopy.crt"
if [[ "${TLS_ENABLED}" -eq 1 && "${INSECURE_TLS}" -eq 0 && "${FETCH_CERT}" -eq 1 ]]; then
  if ! command -v openssl >/dev/null 2>&1; then
    echo "openssl is required to fetch the server certificate automatically" >&2
    exit 1
  fi

  echo "Fetching TLS certificate from ${SERVER_HOST}:${SERVER_PORT}"
  openssl s_client \
    -showcerts \
    -connect "${SERVER_HOST}:${SERVER_PORT}" \
    -servername "${SERVER_HOST}" </dev/null 2>/dev/null |
    awk 'BEGIN {capture=0} /BEGIN CERTIFICATE/ {capture=1} capture {print} /END CERTIFICATE/ {exit}' \
    > "${CA_CERT_PATH}"

  if [[ ! -s "${CA_CERT_PATH}" ]]; then
    echo "Failed to fetch a valid certificate from ${SERVER_HOST}:${SERVER_PORT}" >&2
    exit 1
  fi
fi

export CONFIG_PATH SERVER_HOST SERVER_PORT STATE_DIR TOKEN TLS_ENABLED INSECURE_TLS CA_CERT_PATH
python3 - <<'PY'
import json
import os
from pathlib import Path

config_path = Path(os.environ["CONFIG_PATH"])
tls_enabled = os.environ["TLS_ENABLED"] == "1"
insecure_tls = os.environ["INSECURE_TLS"] == "1"
ca_cert_path = os.environ["CA_CERT_PATH"]

config = {
    "server_host": os.environ["SERVER_HOST"],
    "server_port": int(os.environ["SERVER_PORT"]),
    "state_dir": os.environ["STATE_DIR"],
    "auth_token": os.environ["TOKEN"],
    "tls": tls_enabled,
    "ca_cert": ca_cert_path if tls_enabled and not insecure_tls else None,
    "insecure_tls": insecure_tls,
    "interval": 0.4,
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
template_path = project_dir / "systemd-user" / "clipcopy-client.service.template"
target_path = Path.home() / ".config" / "systemd" / "user" / "clipcopy-client.service"
content = template_path.read_text(encoding="utf-8")
content = content.replace("@PROJECT_DIR@", str(project_dir))
content = content.replace("@PYTHON_BIN@", str(venv_dir / "bin" / "python"))
target_path.write_text(content, encoding="utf-8")
PY

  systemctl --user daemon-reload
  systemctl --user import-environment DISPLAY XAUTHORITY DBUS_SESSION_BUS_ADDRESS XDG_RUNTIME_DIR XDG_SESSION_TYPE
  systemctl --user enable clipcopy-client.service
  if [[ "${START_SERVICE}" -eq 1 ]]; then
    systemctl --user restart clipcopy-client.service
  fi
fi

cat <<EOF

ClipCopy client installation complete.

Project directory: ${PROJECT_DIR}
Virtual environment: ${VENV_DIR}
Config file: ${CONFIG_PATH}
Target server: ${SERVER_HOST}:${SERVER_PORT}
TLS enabled: ${TLS_ENABLED}
Clipboard mode: text-only

If you installed the service, inspect it with:
  systemctl --user status clipcopy-client.service
  journalctl --user -u clipcopy-client.service -f
EOF
