#!/usr/bin/env bash
# Open a Chrome/Chromium profile for inspection or customization, then reuse it
# as a master profile via MOZ_MASTER_PROFILE_DIR.
#
# The Moz domain-analysis tool is server-rendered and needs no extension, so a
# master profile is optional. This helper is provided for parity with
# ahref-local and for cases where you want a pre-warmed profile (cookies,
# consent dismissed, etc.).
#
# Usage:
#   bash tools/open_chrome_profile.sh
#   PROFILE_DIR=/tmp/foo bash tools/open_chrome_profile.sh
#
# After closing Chrome, point the checker at it:
#   MOZ_MASTER_PROFILE_DIR="<PROFILE_DIR>" bash run.sh

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
MOZ_DIR="$(dirname "${SCRIPT_DIR}")"

PROFILE_DIR="${PROFILE_DIR:-${MOZ_DIR}/master-profile}"

# Resolve to an absolute path.
if command -v realpath >/dev/null 2>&1; then
  PROFILE_DIR="$(realpath -m "${PROFILE_DIR}")"
fi
echo "[*] Profile dir: ${PROFILE_DIR}"

# --- Find a Chrome/Chromium binary (fixed order) ---------------------------
CHROME_BIN=""
CANDIDATES=(
  "${MOZ_DIR}/vendor/ungoogled-chromium/chrome"
  "${MOZ_DIR}/vendor/ungoogled-chromium/Chromium.app/Contents/MacOS/Chromium"
  "/Applications/Chromium.app/Contents/MacOS/Chromium"
  "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
  "/usr/bin/ungoogled-chromium"
  "/usr/bin/chromium-browser"
  "/usr/bin/chromium"
  "/usr/bin/google-chrome-stable"
  "/usr/bin/google-chrome"
)
for cand in "${CANDIDATES[@]}"; do
  if [[ -x "${cand}" ]]; then CHROME_BIN="${cand}"; break; fi
done

if [[ -z "${CHROME_BIN}" ]]; then
  echo "❌ No Chrome/Chromium binary found. Run tools/setup_vendor.sh or install Chrome." >&2
  exit 1
fi
echo "✅ Chrome: ${CHROME_BIN}"

mkdir -p "${PROFILE_DIR}"

echo ""
echo "Chrome will open with the profile above. Customize it (dismiss consent,"
echo "sign in, etc.), then close the window. Reuse it with:"
echo "  MOZ_MASTER_PROFILE_DIR=\"${PROFILE_DIR}\" bash run.sh"
echo ""

"${CHROME_BIN}" \
  --user-data-dir="${PROFILE_DIR}" \
  --no-first-run \
  --no-default-browser-check \
  --disable-blink-features=AutomationControlled \
  "https://moz.com/domain-analysis?site=example.com"

echo ""
echo "✅ Chrome closed. Profile saved at ${PROFILE_DIR}"
