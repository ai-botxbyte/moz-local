#!/usr/bin/env bash
# Launcher for moz_checker.py (test mode).
# Starts the local mock server and the headed Moz checker.

set -u
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$HERE"

VENV_PY=""
if [[ -x ".venv/bin/python3" ]]; then
    VENV_PY=".venv/bin/python3"
elif [[ -x "venv/bin/python3" ]]; then
    VENV_PY="venv/bin/python3"
else
    VENV_PY="$(command -v python3)"
fi

API_URL="http://127.0.0.1:8000"
MOCK_PORT=8000
START_MOCK=1

# Allow --no-mock to skip launching the bundled mock server.
ARGS=()
for a in "$@"; do
    case "$a" in
        --no-mock) START_MOCK=0 ;;
        *) ARGS+=("$a") ;;
    esac
done

echo "╔════════════════════════════════════════╗"
echo "║        Moz Local Checker Launcher      ║"
echo "║   (test mode / headed / single browser)║"
echo "╚════════════════════════════════════════╝"
echo ""
read -rp "Number of tabs [1-20] (default: 5): " tabs
tabs="${tabs:-5}"

# Validate: integer in [1, 20].
if ! [[ "$tabs" =~ ^[0-9]+$ ]] || (( tabs < 1 )) || (( tabs > 20 )); then
    echo "❌ Invalid tab count '${tabs}'. Must be an integer between 1 and 20." >&2
    exit 1
fi

MOCK_PID=""
cleanup() {
    if [[ -n "$MOCK_PID" ]]; then
        kill "$MOCK_PID" 2>/dev/null || true
        wait "$MOCK_PID" 2>/dev/null || true
    fi
}
trap cleanup EXIT INT TERM

if (( START_MOCK )); then
    echo "[*] Starting mock server on port ${MOCK_PORT}..."
    "$VENV_PY" mock_server.py --port "${MOCK_PORT}" &
    MOCK_PID=$!
    # Wait for /health.
    for _ in $(seq 1 20); do
        if curl -sf "${API_URL}/health" >/dev/null 2>&1; then
            echo "[*] Mock server is up."
            break
        fi
        sleep 0.5
    done
fi

echo ""
echo "[*] Starting moz_checker.py --api-url ${API_URL} --tabs ${tabs} --no-proxy (headed)"
echo ""

exec "$VENV_PY" moz_checker.py --api-url "${API_URL}" --tabs "${tabs}" --no-proxy "${ARGS[@]}"
