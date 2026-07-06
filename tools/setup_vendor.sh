#!/usr/bin/env bash
# One-time setup: download + extract ungoogled-chromium into
# moz-local/vendor/ungoogled-chromium/. Idempotent.
#
# moz_checker.py needs a Chromium binary. Rather than depend on whatever
# Chrome the host has, we vendor a known-good ungoogled-chromium build.
# moz_checker.py auto-discovers vendor/ungoogled-chromium/chrome at runtime
# (falling back to system Chrome/Chromium when the vendored build is absent,
# e.g. on macOS where the Linux portable build won't run).
#
# Usage:
#   bash tools/setup_vendor.sh

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
MOZ_DIR="$(dirname "${SCRIPT_DIR}")"
VENDOR_DIR="${MOZ_DIR}/vendor"

# ─── Pinned version ─────────────────────────────────────────────────────────
CHROMIUM_VERSION="149.0.7827.53-1"
CHROMIUM_TARBALL_URL="https://github.com/ungoogled-software/ungoogled-chromium-portablelinux/releases/download/${CHROMIUM_VERSION}/ungoogled-chromium-${CHROMIUM_VERSION}-x86_64_linux.tar.xz"
CHROMIUM_TARBALL="${VENDOR_DIR}/ungoogled-chromium.tar.xz"
CHROMIUM_DIR="${VENDOR_DIR}/ungoogled-chromium"
CHROMIUM_INNER="${VENDOR_DIR}/ungoogled-chromium-${CHROMIUM_VERSION}-x86_64_linux"

EXTENSION_URL="https://github.com/tenacious6/cf-autoclick.git"
EXTENSION_DIR="${VENDOR_DIR}/cf-autoclick"

mkdir -p "${VENDOR_DIR}"

# ─── cf-autoclick Cloudflare-bypass extension (all OSes) ────────────────────
if [[ -f "${EXTENSION_DIR}/manifest.json" ]]; then
  echo "[*] cf-autoclick already present at ${EXTENSION_DIR} — skipping"
else
  echo "[*] Cloning cf-autoclick from ${EXTENSION_URL}..."
  git clone --depth 1 "${EXTENSION_URL}" "${EXTENSION_DIR}"
  if [[ ! -f "${EXTENSION_DIR}/manifest.json" ]]; then
    echo "❌ Cloned extension missing manifest.json: ${EXTENSION_DIR}" >&2
    exit 1
  fi
  echo "✅ Extension ready at ${EXTENSION_DIR}"
fi

if [[ "$(uname -s)" != "Linux" ]]; then
  echo "[!] The vendored Chromium build targets Linux x86_64."
  echo "    On $(uname -s), moz_checker.py falls back to system Chrome/Chromium."
  echo "    Skipping Chromium download (cf-autoclick extension is set up above)."
  exit 0
fi

if [[ -x "${CHROMIUM_DIR}/chrome" ]]; then
  echo "[*] ungoogled-chromium already present at ${CHROMIUM_DIR}/chrome — skipping"
  exit 0
fi

cleanup_partial() {
  # Never leave a partial / non-executable binary behind on failure.
  rm -rf "${CHROMIUM_INNER}" 2>/dev/null || true
  if [[ -e "${CHROMIUM_DIR}/chrome" && ! -x "${CHROMIUM_DIR}/chrome" ]]; then
    rm -rf "${CHROMIUM_DIR}" 2>/dev/null || true
  fi
}
trap 'cleanup_partial' ERR

if [[ ! -f "${CHROMIUM_TARBALL}" ]]; then
  echo "[*] Downloading ungoogled-chromium ${CHROMIUM_VERSION}..."
  curl -fL --progress-bar -o "${CHROMIUM_TARBALL}" "${CHROMIUM_TARBALL_URL}"
else
  echo "[*] Tarball already cached at ${CHROMIUM_TARBALL}"
fi

echo "[*] Extracting tarball to ${VENDOR_DIR}..."
tar -xJf "${CHROMIUM_TARBALL}" -C "${VENDOR_DIR}"

if [[ ! -d "${CHROMIUM_INNER}" ]]; then
  echo "❌ Expected extracted dir ${CHROMIUM_INNER} not found." >&2
  exit 1
fi

mv "${CHROMIUM_INNER}" "${CHROMIUM_DIR}"
chmod +x "${CHROMIUM_DIR}/chrome" "${CHROMIUM_DIR}/chromedriver" 2>/dev/null || true

echo "[*] Verifying binary..."
if ! "${CHROMIUM_DIR}/chrome" --version; then
  echo "❌ vendor/ungoogled-chromium/chrome failed to run --version." >&2
  echo "   You may be missing system libraries. On Ubuntu try:" >&2
  echo "     sudo apt install -y libnss3 libatk-bridge2.0-0 libxkbcommon0 \\" >&2
  echo "                         libxcomposite1 libxdamage1 libxrandr2 libgbm1 \\" >&2
  echo "                         libpango-1.0-0 libcairo2 libasound2t64" >&2
  rm -rf "${CHROMIUM_DIR}"
  exit 1
fi

trap - ERR
echo "✅ Chromium ready at ${CHROMIUM_DIR}/chrome"
cat <<EOF

╔══════════════════════════════════════════════════════════════╗
║                  ✅  VENDOR SETUP COMPLETE                   ║
╚══════════════════════════════════════════════════════════════╝

  Chromium: ${CHROMIUM_DIR}/chrome

Try:
  cd ${MOZ_DIR}
  python -m venv .venv && .venv/bin/pip install -r requirements.txt
  bash run.sh

EOF
