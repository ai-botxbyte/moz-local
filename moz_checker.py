"""
Moz Pull-based Checker (test mode) — mirrors ahref-local, targets Moz.

Each worker gets its own per-run Chrome profile, runs a *headed* (visible)
browser (preferring a vendored ungoogled-chromium build), and pulls domains
from a local MOCK SERVER — never the production management-service API.

For each domain it navigates to:

  https://moz.com/domain-analysis?site=<domain>

The Moz domain-analysis page is server-rendered, so there is no React modal
or Cloudflare Turnstile dance (unlike Ahrefs). The scrape reduces to:
navigate -> wait for the results card -> read the DOM via the moz.json
evaluate script.

Usage:
    python moz_checker.py [--api-url URL] [--workers N] [--headless]
                          [--chrome /path/to/chrome] [--no-proxy]
                          [--webshare-proxy] [--scrape-timeout 30]

By default the browser runs HEADED and proxies are used only if configured.
The launcher (run.sh) starts this in test mode against a local mock server.
"""

import argparse
import atexit
import html as _htmlmod
import json
import os
import platform
import random
import re
import shutil
import signal
import sys
import tempfile
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Optional

import requests
from requests.adapters import HTTPAdapter

try:
    from urllib3.util.retry import Retry
except ImportError:  # pragma: no cover
    from requests.packages.urllib3.util.retry import Retry  # type: ignore

# --------------------------------------------------------------------------- #
# Constants / config
# --------------------------------------------------------------------------- #
WEBSHARE_API_KEY = os.environ.get("WEBSHARE_API_KEY", "")
WEBSHARE_PROXY_HOST = "p.webshare.io"
WEBSHARE_PROXY_PORT = "9999"
_WEBSHARE_AUTH_ID: Optional[int] = None

MOZ_JSON_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "moz.json")
PROXIES_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "proxies.txt")

# Test mode: default to a local loopback mock server. NEVER articleinnovator.com.
DEFAULT_API_URL = os.environ.get("MOZ_API_URL", "http://127.0.0.1:8000")

# Optional master profile to copy per worker (parity with ahref-local). When
# unset, workers use a plain empty profile — no extension is needed for Moz.
MOZ_MASTER_PROFILE_DIR = os.environ.get("MOZ_MASTER_PROFILE_DIR", "").strip() or None

DEFAULT_SCRAPE_TIMEOUT = int(os.environ.get("MOZ_SCRAPE_TIMEOUT", "30"))
DEFAULT_POLL_INTERVAL = float(os.environ.get("MOZ_POLL_INTERVAL", "5"))

_CREATED_PROFILES: List[str] = []
_PROFILES_LOCK = threading.Lock()


# --------------------------------------------------------------------------- #
# Mode dispatch (single "moz" mode)
# --------------------------------------------------------------------------- #
class CheckerMode:
    """Per-mode config: endpoint, target URL, JS spec, result parser."""

    def __init__(self, name: str, endpoint: str, target_url: str,
                 spec_path: str, extract_row: Callable[[Dict[str, Any], str], Dict[str, Any]]):
        self.name = name
        self.endpoint = endpoint
        self.target_url = target_url
        self.spec_path = spec_path
        self.extract_row = extract_row

    def build_url(self, domain: str) -> str:
        """Build the per-domain Moz URL. The domain is percent-encoded and
        passed via the ?site= query param."""
        from urllib.parse import quote
        return self.target_url.format(domain=quote(domain, safe=""))


def _extract_moz_row(parsed: Dict[str, Any], domain: str) -> Dict[str, Any]:
    """Map the parsed evaluate-script output into a result row.

    completed iff at least one of the four core metrics is non-null;
    every core metric that could not be located is present as None.
    """
    row: Dict[str, Any] = {"domain_name": domain, "status": "error"}
    results = parsed.get("results")
    if results and isinstance(results, list):
        r = results[0]
        row["domain_name"] = r.get("domain_name", domain)
        for k in ("domain_authority", "page_authority", "linking_domains",
                  "spam_score", "ranking_keywords"):
            row[k] = r.get(k)
        row["top_pages"] = r.get("top_pages") or []
        row["top_linking_domains"] = r.get("top_linking_domains") or []
        # Full page capture: overview cards + every table section.
        row["overview"] = r.get("overview") or {}
        row["tables"] = r.get("tables") or {}
        core = [row["domain_authority"], row["page_authority"],
                row["linking_domains"], row["spam_score"]]
        if any(v not in (None, "") for v in core):
            row["status"] = "completed"
        elif r.get("error"):
            row["error"] = r.get("error")
    return row


MODES: Dict[str, CheckerMode] = {
    "moz": CheckerMode(
        name="moz",
        # Pull/result endpoint on the management-service (mirrored by mock_server
        # in test mode). Matches the production moz-pro pull-based pool route.
        endpoint="/moz-pro/",
        target_url="https://moz.com/domain-analysis?site={domain}",
        spec_path=MOZ_JSON_PATH,
        extract_row=_extract_moz_row,
    ),
}


# --------------------------------------------------------------------------- #
# Pure-Python DOM parser (mirror of moz.json JS) — used for offline tests.
# --------------------------------------------------------------------------- #
def extract_moz_metrics_from_html(html_text: str, domain: str) -> Dict[str, Any]:
    """Parse Moz domain-analysis HTML into the same shape the JS produces.

    This is a deterministic, browser-free mirror of the moz.json evaluate
    script so the mapping can be property-tested without Selenium.
    """
    def clean(s: str) -> str:
        return _htmlmod.unescape(re.sub(r"<[^>]+>", " ", s)).replace("\xa0", " ")

    def norm(s: str) -> str:
        return re.sub(r"\s+", " ", clean(s)).strip()

    result: Dict[str, Any] = {
        "domain_name": domain, "domain_authority": None, "page_authority": None,
        "linking_domains": None, "spam_score": None, "ranking_keywords": None,
        "top_pages": [], "top_linking_domains": [],
    }

    for label_raw, value_raw in re.findall(
        r"<h5[^>]*>(.*?)</h5>\s*<h1[^>]*>(.*?)</h1>", html_text, re.S
    ):
        label = norm(label_raw).lower()
        value = norm(value_raw)
        if not label or value == "":
            continue
        if "domain authority" in label:
            result["domain_authority"] = value
        elif "linking" in label:
            result["linking_domains"] = value
        elif "ranking keywords" in label:
            result["ranking_keywords"] = value
        elif "spam" in label:
            result["spam_score"] = value

    def parse_table(heading: str) -> List[Dict[str, str]]:
        rows_out: List[Dict[str, str]] = []
        m = re.search(re.escape(heading) + r"(.*?)</table>", html_text, re.S)
        if not m:
            return rows_out
        block = m.group(1)
        for row in re.findall(r"<tr>(.*?)</tr>", block, re.S):
            tds = re.findall(r"<td[^>]*>(.*?)</td>", row, re.S)
            if len(tds) >= 2:
                first = norm(tds[0])
                last = norm(tds[-1])
                if first:
                    rows_out.append({"first": first, "last": last})
        return rows_out

    for tp in parse_table("Top Pages by Links"):
        result["top_pages"].append({"url": tp["first"], "pa": tp["last"]})
    for tl in parse_table("Top Linking Domains"):
        result["top_linking_domains"].append({"domain": tl["first"], "da": tl["last"]})

    if result["top_pages"]:
        root = None
        maxpa = None
        for tp in result["top_pages"]:
            digits = re.sub(r"[^0-9]", "", tp["pa"] or "")
            if digits:
                num = int(digits)
                maxpa = num if maxpa is None else max(maxpa, num)
            u = re.sub(r"^https?://", "", tp["url"] or "")
            u = re.sub(r"^//", "", u)
            if root is None and (u == domain + "/" or u == domain):
                root = tp["pa"]
        result["page_authority"] = root if root is not None else (
            str(maxpa) if maxpa is not None else None)

    core = [result["domain_authority"], result["page_authority"],
            result["linking_domains"], result["spam_score"]]
    if not any(v not in (None, "") for v in core):
        result["error"] = "no metrics found (results card missing or rate-limited)"
    return result


# --------------------------------------------------------------------------- #
# HTTP resilience
# --------------------------------------------------------------------------- #
def _make_resilient_session() -> requests.Session:
    s = requests.Session()
    retry = Retry(
        total=3, connect=3, read=3, status=3,
        backoff_factor=1.0,
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=frozenset(["GET", "POST"]),
        raise_on_status=False,
    )
    adapter = HTTPAdapter(pool_connections=10, pool_maxsize=20, max_retries=retry)
    s.mount("https://", adapter)
    s.mount("http://", adapter)
    return s


_HTTP = _make_resilient_session()
_print_lock = threading.Lock()


def tprint(msg: str) -> None:
    with _print_lock:
        print(msg, flush=True)


# --------------------------------------------------------------------------- #
# Webshare IP authorization (optional)
# --------------------------------------------------------------------------- #
def webshare_get_my_ip() -> str:
    resp = requests.get("https://api.ipify.org", timeout=10)
    resp.raise_for_status()
    return resp.text.strip()


def webshare_authorize_ip(ip: str, max_retries: int = 3) -> int:
    """Authorize IP with Webshare, retrying up to max_retries within ~30s each."""
    global _WEBSHARE_AUTH_ID
    last_err: Optional[Exception] = None
    for attempt in range(max_retries):
        try:
            resp = requests.post(
                "https://proxy.webshare.io/api/v2/proxy/ipauthorization/",
                json={"ip_address": ip},
                headers={"Authorization": f"Token {WEBSHARE_API_KEY}"},
                timeout=30,
            )
            if resp.status_code == 400 and "already" in resp.text.lower():
                list_resp = requests.get(
                    "https://proxy.webshare.io/api/v2/proxy/ipauthorization/",
                    headers={"Authorization": f"Token {WEBSHARE_API_KEY}"},
                    timeout=30,
                )
                list_resp.raise_for_status()
                for entry in list_resp.json().get("results", []):
                    if entry.get("ip_address") == ip:
                        _WEBSHARE_AUTH_ID = entry["id"]
                        tprint(f"✅ IP {ip} already authorized (id={_WEBSHARE_AUTH_ID})")
                        return _WEBSHARE_AUTH_ID
                return 0
            resp.raise_for_status()
            _WEBSHARE_AUTH_ID = resp.json()["id"]
            tprint(f"✅ Authorized IP {ip} with Webshare (id={_WEBSHARE_AUTH_ID})")
            return _WEBSHARE_AUTH_ID
        except Exception as e:
            last_err = e
            tprint(f"⚠️  Webshare authorize attempt {attempt + 1}/{max_retries} failed: {e}")
            time.sleep(2)
    raise RuntimeError(f"Webshare IP authorization failed after {max_retries} attempts: {last_err}")


def webshare_deauthorize_ip(max_retries: int = 3) -> None:
    global _WEBSHARE_AUTH_ID
    if not _WEBSHARE_AUTH_ID:
        return
    for attempt in range(max_retries):
        try:
            resp = requests.delete(
                f"https://proxy.webshare.io/api/v2/proxy/ipauthorization/{_WEBSHARE_AUTH_ID}/",
                headers={"Authorization": f"Token {WEBSHARE_API_KEY}"},
                timeout=15,
            )
            if resp.status_code in (204, 200):
                tprint(f"✅ Deauthorized IP from Webshare (id={_WEBSHARE_AUTH_ID})")
                _WEBSHARE_AUTH_ID = None
                return
        except Exception as e:
            tprint(f"⚠️  Webshare deauthorize attempt {attempt + 1}/{max_retries} failed: {e}")
            time.sleep(1)
    tprint("⚠️  Failed to deauthorize IP after retries; continuing cleanup")
    _WEBSHARE_AUTH_ID = None


# --------------------------------------------------------------------------- #
# Per-worker profile management
# --------------------------------------------------------------------------- #
def _create_worker_profile(worker_id: int) -> str:
    """Copy the master profile into a fresh per-worker directory."""
    profile_id = f"moz_w{worker_id}_{uuid.uuid4().hex[:8]}"
    dest = os.path.join(tempfile.gettempdir(), profile_id)
    if os.path.isdir(dest):
        shutil.rmtree(dest, ignore_errors=True)
    shutil.copytree(MOZ_MASTER_PROFILE_DIR, dest)
    with _PROFILES_LOCK:
        _CREATED_PROFILES.append(dest)
    return dest


def _create_plain_profile(worker_id: int) -> str:
    """Create a plain empty per-worker profile (default — no extension needed)."""
    profile_id = f"moz_w{worker_id}_{uuid.uuid4().hex[:8]}"
    dest = os.path.join(tempfile.gettempdir(), profile_id)
    os.makedirs(dest, exist_ok=True)
    with _PROFILES_LOCK:
        _CREATED_PROFILES.append(dest)
    return dest


def _make_worker_profile(worker_id: int) -> str:
    """Create a worker profile, copying a master profile if one is configured."""
    if MOZ_MASTER_PROFILE_DIR and os.path.isdir(MOZ_MASTER_PROFILE_DIR):
        return _create_worker_profile(worker_id)
    return _create_plain_profile(worker_id)


def _remove_worker_profile(path: Optional[str], retries: int = 3) -> None:
    """Remove a single per-worker profile dir with retries."""
    if not path:
        return
    for attempt in range(retries):
        try:
            if os.path.isdir(path):
                shutil.rmtree(path, ignore_errors=False)
            break
        except Exception:
            if attempt == retries - 1:
                tprint(f"  [cleanup] could not remove profile after {retries} tries: {path}")
            else:
                time.sleep(0.5)
    with _PROFILES_LOCK:
        try:
            _CREATED_PROFILES.remove(path)
        except ValueError:
            pass


def _global_profile_cleanup() -> None:
    """atexit + signal hook — wipe every per-worker dir."""
    with _PROFILES_LOCK:
        paths = list(_CREATED_PROFILES)
        _CREATED_PROFILES.clear()
    for p in paths:
        try:
            shutil.rmtree(p, ignore_errors=True)
        except Exception:
            pass


atexit.register(_global_profile_cleanup)


def _install_signal_handlers() -> None:
    def _handler(signum, _frame):
        # Each cleanup step is isolated so one failure doesn't skip the rest.
        for step in (webshare_deauthorize_ip, _global_profile_cleanup):
            try:
                step()
            except Exception as e:
                tprint(f"  [signal] cleanup step {step.__name__} raised: {e}")
        sys.exit(128 + signum)

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            signal.signal(sig, _handler)
        except (ValueError, OSError):
            pass


# --------------------------------------------------------------------------- #
# Browser discovery + driver build
# --------------------------------------------------------------------------- #
def find_chrome_binary(explicit: Optional[str] = None) -> Optional[str]:
    """Return a browser binary path using a fixed, documented search order.

    Prefers the vendored ungoogled-chromium build, then system chromium/chrome.
    """
    if explicit and os.path.exists(explicit):
        return explicit

    script_dir = os.path.dirname(os.path.abspath(__file__))
    candidates = [
        # 1. Vendored ungoogled-chromium (Linux portable build)
        os.path.join(script_dir, "vendor", "ungoogled-chromium", "chrome"),
        # 2. Vendored ungoogled-chromium (macOS app bundle, if present)
        os.path.join(script_dir, "vendor", "ungoogled-chromium",
                     "Chromium.app", "Contents", "MacOS", "Chromium"),
    ]
    system = platform.system()
    if system == "Linux":
        candidates += [
            "/usr/bin/ungoogled-chromium",
            "/usr/bin/chromium-browser",
            "/usr/bin/chromium",
            "/usr/bin/google-chrome-stable",
            "/usr/bin/google-chrome",
        ]
    elif system == "Darwin":
        candidates += [
            "/Applications/Chromium.app/Contents/MacOS/Chromium",
            "/Applications/Ungoogled Chromium.app/Contents/MacOS/Chromium",
            "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
        ]
    elif system == "Windows":
        for env in ("PROGRAMFILES", "PROGRAMFILES(X86)", "LOCALAPPDATA"):
            base = os.environ.get(env, "")
            if base:
                candidates.append(os.path.join(base, "Google", "Chrome",
                                                "Application", "chrome.exe"))
    for c in candidates:
        if c and os.path.exists(c):
            return c
    return None


def detect_chrome_major(chrome_binary: Optional[str]) -> Optional[int]:
    import subprocess
    if not chrome_binary:
        return None
    try:
        out = subprocess.check_output([chrome_binary, "--version"], text=True, timeout=5)
        m = re.search(r"(\d+)\.", out)
        return int(m.group(1)) if m else None
    except Exception:
        return None


def _free_port() -> int:
    import socket
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


_driver_build_lock = threading.Lock()


def find_cf_extension() -> Optional[str]:
    """Locate the vendored cf-autoclick Cloudflare-bypass extension."""
    script_dir = os.path.dirname(os.path.abspath(__file__))
    ext = os.path.join(script_dir, "vendor", "cf-autoclick")
    if os.path.isfile(os.path.join(ext, "manifest.json")):
        return ext
    return None


def build_driver(worker_id: int, headless: bool, chrome_binary: Optional[str],
                 version_main: Optional[int], proxy: Optional[str] = None,
                 page_load_strategy: str = "eager", extension_path: Optional[str] = None,
                 persistent_profile: Optional[str] = None):
    """Build a headed (default) uc.Chrome driver. Returns (driver, profile_path).

    When ``persistent_profile`` is set, that directory is used directly as the
    Chrome user-data-dir and is NOT deleted on exit, so the logged-in Moz
    session (cookies) survives across runs — login is only needed once.
    """
    import undetected_chromedriver as uc

    with _driver_build_lock:
        opts = uc.ChromeOptions()
        opts.page_load_strategy = page_load_strategy
        opts.add_argument("--no-first-run")
        opts.add_argument("--no-default-browser-check")
        opts.add_argument("--disable-blink-features=AutomationControlled")
        opts.add_argument("--disable-infobars")
        opts.add_argument("--lang=en-US")
        opts.add_argument("--window-size=1400,1000")
        opts.add_argument(f"--remote-debugging-port={_free_port()}")
        opts.add_argument("--password-store=basic")
        opts.add_argument("--use-mock-keychain")

        # Cloudflare-bypass extension (cf-autoclick) — auto-clicks the Turnstile
        # "verify you are human" challenge. Same extension ahref-local uses.
        if extension_path and os.path.isdir(extension_path):
            ext_abs = os.path.abspath(extension_path)
            opts.add_argument(f"--load-extension={ext_abs}")
            opts.add_argument(f"--disable-extensions-except={ext_abs}")
            tprint(f"  [worker-{worker_id}] Loading cf-autoclick extension: {ext_abs}")

        if persistent_profile:
            profile = os.path.abspath(persistent_profile)
            os.makedirs(profile, exist_ok=True)
            tprint(f"  [worker-{worker_id}] Persistent profile: {profile}")
        else:
            try:
                profile = _make_worker_profile(worker_id)
            except Exception as e:
                raise RuntimeError(f"[W{worker_id}] profile creation failed: {e}") from e
            tprint(f"  [worker-{worker_id}] Profile: {profile}")

        if chrome_binary:
            opts.binary_location = chrome_binary

        if proxy:
            parts = proxy.split(":")
            if len(parts) == 4:
                import zipfile
                ip, port, user, passwd = parts
                ext_zip = os.path.join(tempfile.gettempdir(), f"moz_proxy_ext_w{worker_id}.zip")
                with zipfile.ZipFile(ext_zip, "w") as zp:
                    zp.writestr("manifest.json", json.dumps({
                        "version": "1.0.0", "manifest_version": 2, "name": "Proxy Auth",
                        "permissions": ["proxy", "tabs", "unlimitedStorage", "storage",
                                        "<all_urls>", "webRequest", "webRequestBlocking"],
                        "background": {"scripts": ["background.js"]},
                        "minimum_chrome_version": "22.0.0",
                    }))
                    zp.writestr("background.js", f"""
                        var config = {{mode:"fixed_servers",rules:{{
                            singleProxy:{{scheme:"http",host:"{ip}",port:parseInt({port})}},
                            bypassList:["localhost"]}}}};
                        chrome.proxy.settings.set({{value:config,scope:"regular"}},function(){{}});
                        chrome.webRequest.onAuthRequired.addListener(
                            function(d){{return {{authCredentials:{{username:"{user}",password:"{passwd}"}}}};}},
                            {{urls:["<all_urls>"]}},['blocking']);
                    """)
                opts.add_extension(ext_zip)
            elif len(parts) == 2:
                opts.add_argument(f"--proxy-server=http://{proxy}")

        driver = uc.Chrome(options=opts, headless=headless, use_subprocess=True,
                           version_main=version_main, user_data_dir=profile)
        driver.set_page_load_timeout(30)
        driver.set_script_timeout(45)
        return driver, profile


def load_mode_js(spec_path: str) -> str:
    with open(spec_path, "r", encoding="utf-8") as fh:
        spec = json.load(fh)
    action = next((a for a in spec.get("actions", []) if a.get("type") == "evaluate"), None)
    if not action or "script" not in action:
        raise RuntimeError(f"{spec_path} missing the 'evaluate' action / script")
    return action["script"]


def load_proxies() -> List[str]:
    if not os.path.exists(PROXIES_PATH):
        return []
    with open(PROXIES_PATH) as f:
        return [line.strip() for line in f if line.strip()]


# --------------------------------------------------------------------------- #
# Scrape a single domain (server-rendered Moz page)
# --------------------------------------------------------------------------- #
def scrape_domain(driver, domain: str, mode: CheckerMode,
                  scrape_timeout: int = DEFAULT_SCRAPE_TIMEOUT) -> Dict[str, Any]:
    t0 = time.time()
    row: Dict[str, Any] = {"domain_name": domain, "status": "error"}

    try:
        try:
            driver.get(mode.build_url(domain))
        except Exception as e:
            row["error"] = f"navigation error/timeout: {e}"
            row["elapsed_seconds"] = round(time.time() - t0, 2)
            row["finished_at"] = datetime.now(timezone.utc).isoformat()
            return row

        js = load_mode_js(mode.spec_path).replace("${domains}", domain)
        deadline = time.time() + scrape_timeout
        parsed: Optional[Dict[str, Any]] = None

        while time.time() < deadline:
            try:
                has_card = driver.execute_script(
                    "return !!document.querySelector('.col-md-3 h1') "
                    "|| !!document.querySelector('.text-pink');"
                )
            except Exception:
                has_card = False

            if has_card:
                try:
                    raw = driver.execute_script(js)
                    if raw:
                        candidate = json.loads(raw) if isinstance(raw, str) else raw
                        if isinstance(candidate, dict) and "results" in candidate:
                            r0 = candidate["results"][0] if candidate["results"] else {}
                            core = [r0.get("domain_authority"), r0.get("page_authority"),
                                    r0.get("linking_domains"), r0.get("spam_score")]
                            if any(v not in (None, "") for v in core):
                                parsed = candidate
                                break
                            parsed = candidate  # keep last, maybe still loading
                except Exception:
                    pass
            time.sleep(1.0)

        if parsed is not None:
            row = mode.extract_row(parsed, domain)
        else:
            row["error"] = f"scraping timeout reached ({scrape_timeout}s); no metrics found"

    except Exception as e:
        row["error"] = f"{type(e).__name__}: {e}"
    finally:
        row["elapsed_seconds"] = round(time.time() - t0, 2)
        row["finished_at"] = datetime.now(timezone.utc).isoformat()
    return row


# --------------------------------------------------------------------------- #
# Mock-server pull / post
# --------------------------------------------------------------------------- #
def pull_domain(api_url: str, mode: CheckerMode) -> Optional[Dict[str, Any]]:
    try:
        resp = _HTTP.get(f"{api_url}{mode.endpoint}", timeout=(10, 30))
        if resp.status_code == 204:
            return None
        resp.raise_for_status()
        data = resp.json()
        if data.get("success"):
            return data["data"]
    except requests.exceptions.HTTPError as e:
        if e.response is not None and e.response.status_code == 204:
            return None
    except Exception as e:
        tprint(f"  [pull] error after retries: {e}")
    return None


def get_credentials(api_url: str) -> Optional[Dict[str, str]]:
    """Fetch Moz login credentials from the account pool (never hard-coded).

    Precedence:
      1. MOZ_LOGIN_EMAIL / MOZ_LOGIN_PASSWORD env vars — used by the VNC runner,
         which cannot reach the private /moz-pro-accounts/pool/ endpoint (creds
         are injected as GitHub secrets instead).
      2. ``GET {api_url}/moz-pro-accounts/pool/`` — the in-cluster account pool;
         one active account is chosen at random to spread the daily report cap.
    In test mode the local mock_server mirrors the pool endpoint.
    """
    env_email = os.environ.get("MOZ_LOGIN_EMAIL")
    env_password = os.environ.get("MOZ_LOGIN_PASSWORD")
    if env_email and env_password:
        return {"email": env_email, "password": env_password}
    try:
        resp = _HTTP.get(f"{api_url}/moz-pro-accounts/pool/", timeout=(10, 30))
        if resp.status_code >= 400:
            return None
        data = resp.json()
        accounts = data.get("data") if isinstance(data, dict) else None
        if isinstance(accounts, list) and accounts:
            acct = random.choice(accounts)
            if acct.get("email") and acct.get("password"):
                return {"email": acct["email"], "password": acct["password"]}
    except Exception as e:
        tprint(f"  [creds] error fetching credentials: {e}")
    return None


def _mask_email(email: str) -> str:
    name, _, domain = email.partition("@")
    head = (name[:2] + "***") if len(name) > 2 else "***"
    return f"{head}@{domain}" if domain else head


LOGIN_URL = "https://moz.com/login"


def _ensure_window(driver) -> bool:
    """Ensure the driver is attached to a live window handle.

    Loading the cf-autoclick extension (which uses chrome.debugger) can cause
    Chrome to open/close tabs on startup, invalidating the current handle.
    """
    try:
        _ = driver.current_url  # touches the current window
        return True
    except Exception:
        pass
    try:
        handles = driver.window_handles
        if handles:
            driver.switch_to.window(handles[0])
            return True
    except Exception:
        pass
    return False


def _safe_switch(driver, handle) -> bool:
    """Switch to a window handle, tolerating a closed/stale window."""
    try:
        driver.switch_to.window(handle)
        return True
    except Exception:
        return _ensure_window(driver) and False


def _open_tabs(driver, n: int) -> List[str]:
    """Open up to n tabs robustly (one at a time via window.open, with guards).

    Returns the list of live handles to use as lanes. More stable than rapid
    switch_to.new_window() calls when the cf-autoclick debugger extension is
    active.
    """
    _ensure_window(driver)
    handles = list(driver.window_handles)
    attempts = 0
    while len(handles) < n and attempts < n * 3:
        attempts += 1
        try:
            driver.switch_to.window(handles[-1])
            driver.execute_script("window.open('about:blank','_blank');")
        except Exception:
            _ensure_window(driver)
        time.sleep(0.6)
        try:
            cur = list(driver.window_handles)
        except Exception:
            _ensure_window(driver)
            continue
        new = [h for h in cur if h not in handles]
        if new:
            handles.extend(new)
        elif len(cur) > len(handles):
            handles = cur
    return handles[:n]

# Cookie/consent banner button labels to auto-dismiss (blocks the login form).
_CONSENT_LABELS = ["I Consent", "I consent", "Accept all", "Accept All", "Accept",
                   "I Agree", "I agree", "Agree", "Got it", "Allow all", "OK"]


def dismiss_consent(driver) -> bool:
    """Click a cookie/consent banner button if one is covering the page.

    Moz shows a "Your privacy is important to us." banner with an "I Consent"
    button that intercepts clicks on the login form. Returns True if dismissed.
    """
    from selenium.webdriver.common.by import By
    conds = []
    for lbl in _CONSENT_LABELS:
        # exact match and contains-text (banner button may be a div/span/button)
        conds.append(f"//button[normalize-space()='{lbl}']")
        conds.append(f"//*[@role='button'][normalize-space()='{lbl}']")
        conds.append(f"//a[normalize-space()='{lbl}']")
        conds.append(f"//button[contains(normalize-space(),'{lbl}')]")
    xpath = " | ".join(conds)
    try:
        for el in driver.find_elements(By.XPATH, xpath):
            try:
                if el.is_displayed():
                    try:
                        el.click()
                    except Exception:
                        driver.execute_script("arguments[0].click();", el)
                    tprint(f"  [consent] dismissed banner via '{el.text.strip()}'")
                    time.sleep(1.5)
                    return True
            except Exception:
                continue
    except Exception:
        pass
    return False


# JS that sets a React-controlled input's value the way React expects, so the
# form's internal state updates (avoids a phantom "Required" validation error).
_REACT_SET_JS = """
var el = arguments[0], val = arguments[1];
var proto = el.tagName === 'TEXTAREA'
  ? window.HTMLTextAreaElement.prototype : window.HTMLInputElement.prototype;
var setter = Object.getOwnPropertyDescriptor(proto, 'value').set;
setter.call(el, val);
el.dispatchEvent(new Event('input', {bubbles: true}));
el.dispatchEvent(new Event('change', {bubbles: true}));
"""


def _set_react_input(driver, el, value: str) -> None:
    """Fill a React/MUI controlled input so its component state registers."""
    try:
        el.clear()
    except Exception:
        pass
    driver.execute_script(_REACT_SET_JS, el, value)
    # A trailing real keystroke nudges React and fires the field's blur/validate.
    try:
        el.send_keys(" ")
        el.send_keys("\ue003")  # Backspace — remove the trailing space
    except Exception:
        pass


def _dismiss_consent(driver) -> None:
    """Dismiss whichever cookie-consent banner is covering the page.

    Moz's login page shows a custom "Your privacy is important to us." banner
    with an "I Consent" button. Other pages may use OneTrust. Try the labelled
    button first (matches "I Consent"), then OneTrust accept buttons, then strip
    any known overlay elements so they can't intercept clicks on the form.
    """
    from selenium.webdriver.common.by import By
    # 1. Labelled button ("I Consent", "Accept", ...).
    if dismiss_consent(driver):
        return
    # 2. OneTrust accept buttons.
    for sel in ("#onetrust-accept-btn-handler", ".onetrust-close-btn-handler",
                "#accept-recommended-btn-handler"):
        try:
            btns = driver.find_elements(By.CSS_SELECTOR, sel)
            if btns and btns[0].is_displayed():
                btns[0].click()
                time.sleep(1)
                return
        except Exception:
            pass
    # 3. Fallback: strip known consent overlays entirely.
    try:
        driver.execute_script(
            "['#onetrust-consent-sdk','#onetrust-group-container',"
            "'.onetrust-pc-dark-filter','#onetrust-banner-sdk'].forEach(function(s){"
            "var el=document.querySelector(s); if(el){el.remove();}});")
    except Exception:
        pass


def login(driver, api_url: str, timeout: int = 30) -> bool:
    """Log in to Moz using credentials from the mock server.

    Returns True if the session ends up authenticated (or already was).
    Never logs the password.
    """
    from selenium.webdriver.common.by import By
    from selenium.webdriver.support.ui import WebDriverWait
    from selenium.webdriver.support import expected_conditions as EC

    creds = get_credentials(api_url)
    if not creds:
        tprint("  [login] no credentials available from mock server — skipping scrape")
        return False

    email = creds["email"]
    password = creds["password"]

    # Loading the extension can churn window handles; make sure we're attached
    # to a live window before navigating.
    _ensure_window(driver)
    try:
        driver.get(LOGIN_URL)
    except Exception as e:
        # Window may have been recreated by the extension — re-attach and retry.
        if not _ensure_window(driver):
            tprint(f"  [login] navigation to login page failed: {e}")
            return False
        try:
            driver.get(LOGIN_URL)
        except Exception as e2:
            tprint(f"  [login] navigation to login page failed: {e2}")
            return False

    from selenium.common.exceptions import TimeoutException
    time.sleep(2)

    # Wait for the login form, retrying navigation. This absorbs transient
    # Cloudflare challenges (auto-solved by cf-autoclick) and slow React
    # hydration on the login page. If we get redirected away from /login the
    # session is already authenticated.
    form_present = False
    deadline = time.time() + timeout
    attempt = 0
    while time.time() < deadline and not form_present:
        _dismiss_consent(driver)  # cookie banner blocks the form
        try:
            WebDriverWait(driver, 15).until(
                EC.presence_of_element_located((By.ID, "email")))
            form_present = True
            break
        except TimeoutException:
            if "/login" not in (driver.current_url or ""):
                tprint(f"  [login] session already authenticated ({_mask_email(email)})")
                return True
            attempt += 1
            tprint(f"  [login] form not ready (attempt {attempt}); reloading login page...")
            _ensure_window(driver)
            try:
                driver.get(LOGIN_URL)
            except Exception:
                _ensure_window(driver)
            time.sleep(2)
    if not form_present:
        tprint("  [login] login form never appeared; will retry next session")
        return False

    try:
        wait = WebDriverWait(driver, timeout)
        # Dismiss the "I Consent" cookie banner that overlays the form.
        _dismiss_consent(driver)
        # Email field — set the React way so the form state registers it
        # (plain send_keys can leave a phantom "Required" validation error).
        email_el = wait.until(EC.presence_of_element_located((By.ID, "email")))
        _set_react_input(driver, email_el, email)
        # Pause after entering the username so the MUI/React form settles
        # before the password field is filled.
        time.sleep(3)
        # Banner can render after the form — dismiss again before the password.
        _dismiss_consent(driver)
        # Password field — input#password (name="password").
        pass_el = driver.find_element(By.CSS_SELECTOR, "input#password")
        driver.execute_script("arguments[0].scrollIntoView({block:'center'});", pass_el)
        _set_react_input(driver, pass_el, password)
        time.sleep(1)
        # Tick "Remember me" so the session cookie survives browser restarts
        # (lets the persistent profile skip login on later runs).
        try:
            cb = driver.find_element(By.ID, "remember")
            if not cb.is_selected():
                driver.execute_script("arguments[0].click();", cb)
        except Exception:
            pass
        _dismiss_consent(driver)
        try:
            driver.find_element(By.CSS_SELECTOR, "button[type='submit']").click()
        except Exception:
            driver.execute_script(
                "var b=document.querySelector(\"button[type='submit']\"); if(b){b.click();}")

        # Success = we leave /login (e.g. redirect to /home) or the form disappears.
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                url = driver.current_url or ""
                if "/login" not in url:
                    tprint(f"  [login] OK ({_mask_email(email)}) -> {url}")
                    return True
                still_form = driver.execute_script(
                    "return !!document.getElementById('email');")
                if not still_form:
                    tprint(f"  [login] OK ({_mask_email(email)})")
                    return True
            except Exception:
                pass
            time.sleep(1.0)

        tprint(f"  [login] timeout after {timeout}s; will retry next session")
        return False
    except Exception as e:
        tprint(f"  [login] error: {type(e).__name__}: {e}")
        return False


def server_reachable(api_url: str) -> bool:
    try:
        resp = _HTTP.get(f"{api_url}/health", timeout=(5, 10))
        return resp.status_code < 400
    except Exception:
        return False


def post_result(api_url: str, mode: CheckerMode, execution_record: Dict, result: Dict) -> bool:
    try:
        resp = _HTTP.post(
            f"{api_url}{mode.endpoint}",
            json={"execution_record": execution_record, "result": result},
            timeout=(10, 30),
        )
        resp.raise_for_status()
        ok = resp.json().get("success", False)
        if not ok:
            _buffer_failed_post(mode, execution_record, result)
        return ok
    except Exception as e:
        tprint(f"  [post] error: {e}; buffering result for {result.get('domain_name')}")
        _buffer_failed_post(mode, execution_record, result)
        return False


# --------------------------------------------------------------------------- #
# Persistent post buffer (survives restart)
# --------------------------------------------------------------------------- #
def _post_buffer_path(mode: CheckerMode) -> str:
    return os.environ.get(
        f"MOZ_{mode.name.upper()}_POST_BUFFER",
        os.path.join(tempfile.gettempdir(), f"moz-local_pending_posts_{mode.name}.jsonl"),
    )


_buffer_lock = threading.Lock()
_RETRY_COUNTS: Dict[str, int] = {}


def _buffer_failed_post(mode: CheckerMode, execution_record: Dict, result: Dict) -> None:
    try:
        with _buffer_lock:
            with open(_post_buffer_path(mode), "a", encoding="utf-8") as fh:
                fh.write(json.dumps({
                    "ts": datetime.now(timezone.utc).isoformat(),
                    "execution_record": execution_record,
                    "result": result,
                }) + "\n")
    except Exception as e:
        tprint(f"  [buffer] FATAL: cannot write post buffer: {e}")


def _flush_pending_posts(api_url: str, mode: CheckerMode, max_per_run: int = 50) -> int:
    buf_path = _post_buffer_path(mode)
    if not os.path.exists(buf_path):
        return 0
    if not server_reachable(api_url):
        return 0
    flushed = 0
    remaining: List[str] = []
    with _buffer_lock:
        try:
            with open(buf_path, "r", encoding="utf-8") as fh:
                lines = [ln.strip() for ln in fh if ln.strip()]
        except Exception:
            return 0
        for line in lines:
            if flushed >= max_per_run:
                remaining.append(line)
                continue
            try:
                obj = json.loads(line)
                key = json.dumps(obj.get("result", {}), sort_keys=True)
                resp = _HTTP.post(
                    f"{api_url}{mode.endpoint}",
                    json={"execution_record": obj["execution_record"], "result": obj["result"]},
                    timeout=(10, 30),
                )
                if resp.status_code < 400 and resp.json().get("success"):
                    flushed += 1
                    _RETRY_COUNTS.pop(key, None)
                    continue
                _RETRY_COUNTS[key] = _RETRY_COUNTS.get(key, 0) + 1
                if _RETRY_COUNTS[key] >= 5:
                    tprint(f"  [flusher] result for "
                           f"{obj.get('result', {}).get('domain_name')} undeliverable "
                           f"after 5 attempts — retained in buffer")
            except Exception:
                pass
            remaining.append(line)
        try:
            with open(buf_path, "w", encoding="utf-8") as fh:
                for ln in remaining:
                    fh.write(ln + "\n")
        except Exception:
            pass
    return flushed


def _start_buffer_flusher(api_url: str, mode: CheckerMode) -> threading.Thread:
    def loop():
        while True:
            time.sleep(30)  # >=30s between consecutive re-send attempts
            try:
                n = _flush_pending_posts(api_url, mode)
                if n:
                    tprint(f"  [flusher] re-sent {n} buffered {mode.name} posts")
            except Exception as e:
                tprint(f"  [flusher] error: {e}")
    t = threading.Thread(target=loop, daemon=True, name=f"moz-{mode.name}-post-flusher")
    t.start()
    return t


# --------------------------------------------------------------------------- #
# Worker loop
# --------------------------------------------------------------------------- #
def _is_driver_alive(driver) -> bool:
    if driver is None:
        return False
    try:
        return driver.execute_script("return 1") == 1
    except Exception:
        return False


def worker_loop(worker_id: int, proxy: Optional[str], api_url: str, headless: bool,
                chrome_bin: Optional[str], version_main: Optional[int],
                mode: CheckerMode, scrape_timeout: int, poll_interval: float,
                login_timeout: int = 30):
    proxy_short = proxy.split(":")[0] if proxy else "local"
    tprint(f"  [W{worker_id}] Starting [mode={mode.name}] with proxy {proxy_short}...")

    driver = None
    profile_path: Optional[str] = None
    processed = 0
    authenticated = False

    def start_browser():
        nonlocal driver, profile_path, authenticated
        authenticated = False
        if driver:
            try:
                driver.quit()
            except Exception:
                pass
        if profile_path:
            _remove_worker_profile(profile_path)
            profile_path = None
        last_err = None
        for attempt in range(5):
            try:
                driver, profile_path = build_driver(
                    worker_id, headless=headless, chrome_binary=chrome_bin,
                    version_main=version_main, proxy=proxy)
                if _is_driver_alive(driver):
                    break
                raise RuntimeError("driver built but is_alive() returned False")
            except Exception as e:
                last_err = e
                tprint(f"  [W{worker_id}] driver build attempt {attempt + 1}/5 failed: {e}")
                try:
                    if driver:
                        driver.quit()
                except Exception:
                    pass
                driver = None
                if profile_path:
                    _remove_worker_profile(profile_path)
                    profile_path = None
                time.sleep(min(5 * (2 ** attempt), 30))
        if driver is None:
            raise RuntimeError(f"driver build failed after 5 attempts: {last_err}")
        tprint(f"  [W{worker_id}] Browser ready (proxy: {proxy_short})")
        # Log in to Moz before scraping in this session (Req 12).
        authenticated = login(driver, api_url, login_timeout)

    try:
        start_browser()
        last_healthcheck = time.time()

        while True:
            now = time.time()
            if now - last_healthcheck > 60:
                if not _is_driver_alive(driver):
                    tprint(f"  [W{worker_id}] healthcheck failed — rebuilding browser")
                    start_browser()
                last_healthcheck = now

            if not authenticated:
                # Not logged in — do not scrape unauthenticated. Retry login by
                # rebuilding the session after a short wait (Req 12.4/12.7).
                tprint(f"  [W{worker_id}] not authenticated; retrying login...")
                time.sleep(poll_interval)
                start_browser()
                continue

            record = pull_domain(api_url, mode)
            if record is None:
                time.sleep(poll_interval)
                continue

            domain = record.get("domain_name", "unknown")
            execution_id = str(record.get("execution_id", "?"))[:8]
            tprint(f"  [W{worker_id}] Got: {domain} (exec: {execution_id})")

            try:
                result = scrape_domain(driver, domain, mode, scrape_timeout)
                mark = "OK" if result["status"] == "completed" else "FAIL"
                tprint(f"  [W{worker_id}] [{mark}] {domain} "
                       f"DA={result.get('domain_authority', '-')} "
                       f"PA={result.get('page_authority', '-')} "
                       f"LD={result.get('linking_domains', '-')} "
                       f"Spam={result.get('spam_score', '-')} "
                       f"({result.get('elapsed_seconds', 0):.1f}s)")
                try:
                    driver.get("about:blank")
                except Exception:
                    tprint(f"  [W{worker_id}] post-scrape nav failed; rebuilding browser")
                    start_browser()
            except Exception as e:
                tprint(f"  [W{worker_id}] Error: {type(e).__name__}: {e}. Restarting browser...")
                try:
                    start_browser()
                except Exception as e2:
                    tprint(f"  [W{worker_id}] FATAL: cannot rebuild browser: {e2}")
                    raise
                result = {"domain_name": domain, "status": "error", "error": str(e)}

            post_result(api_url, mode, record, result)
            processed += 1

    except KeyboardInterrupt:
        pass
    except Exception as e:
        tprint(f"  [W{worker_id}] FATAL: {e}")
    finally:
        if driver:
            # Graceful quit; force-kill the underlying process if it lingers.
            proc = getattr(driver, "browser_pid", None)
            try:
                driver.quit()
            except Exception:
                pass
            if proc:
                deadline = time.time() + 10
                while time.time() < deadline:
                    try:
                        os.kill(proc, 0)
                    except OSError:
                        break
                    time.sleep(0.5)
                else:
                    try:
                        os.kill(proc, signal.SIGKILL)
                        tprint(f"  [W{worker_id}] force-killed browser pid {proc}")
                    except OSError:
                        pass
        if profile_path:
            _remove_worker_profile(profile_path)
            tprint(f"  [W{worker_id}] Cleaned up profile {profile_path}")
        tprint(f"  [W{worker_id}] Stopped. Processed: {processed}")


# --------------------------------------------------------------------------- #
# Single-browser, multi-tab pipeline
# --------------------------------------------------------------------------- #
def _accept_parsed(driver, domain: str, js: str, encoded: str):
    """Run the extractor in the current tab; return a completed row or None.

    Guards against stale reads: only accepts metrics when the tab's URL is on
    this domain's page (site=<domain>), so a tab reused for a new domain never
    returns the previous domain's numbers.
    """
    try:
        on_page = driver.execute_script(
            "return (location.href||'').indexOf(arguments[0]) > -1;", "site=" + encoded)
        if not on_page:
            return None
        raw = driver.execute_script(js)
    except Exception:
        return None
    if not raw:
        return None
    try:
        parsed = json.loads(raw) if isinstance(raw, str) else raw
    except Exception:
        return None
    if not (isinstance(parsed, dict) and parsed.get("results")):
        return None
    r0 = parsed["results"][0]
    core = [r0.get("domain_authority"), r0.get("page_authority"),
            r0.get("linking_domains"), r0.get("spam_score")]
    if any(v not in (None, "") for v in core):
        return _extract_moz_row(parsed, domain)
    return None


def run_tabs(api_url: str, headless: bool, chrome_bin: Optional[str],
             version_main: Optional[int], mode: CheckerMode, num_tabs: int,
             scrape_timeout: int, login_timeout: int, poll_interval: float,
             extension_path: Optional[str] = None, stagger: float = 2.0,
             persistent_profile: Optional[str] = None) -> Dict[str, int]:
    """One browser, log in once, pipeline domains across N tabs (shared session).

    Navigations are staggered (default 2s apart) so we don't hit Cloudflare's
    challenge on every tab at once; the cf-autoclick extension solves any
    challenge that does appear.
    """
    from urllib.parse import quote

    stats = {"completed": 0, "error": 0, "total": 0}
    js_template = load_mode_js(mode.spec_path)
    last_launch = 0.0

    # Build the driver with retries — undetected_chromedriver occasionally
    # fails with "unable to discover open pages" on startup, especially with an
    # extension + persistent profile.
    driver = None
    profile_path = None
    last_err = None
    for attempt in range(4):
        try:
            driver, profile_path = build_driver(
                0, headless=headless, chrome_binary=chrome_bin, version_main=version_main,
                proxy=None, page_load_strategy="none", extension_path=extension_path,
                persistent_profile=persistent_profile)
            if _is_driver_alive(driver):
                break
            raise RuntimeError("driver built but is_alive() returned False")
        except Exception as e:
            last_err = e
            tprint(f"[*] driver build attempt {attempt + 1}/4 failed: {e}")
            try:
                if driver:
                    driver.quit()
            except Exception:
                pass
            driver = None
            time.sleep(min(5 * (2 ** attempt), 30))
    if driver is None:
        tprint(f"[FATAL] could not start browser after 4 attempts: {last_err}")
        return stats

    # Give the extension a moment to initialize, then attach to a usable
    # (non-extension) window. Do NOT close tabs — the extension may have opened
    # its own, and we must not close the real content tab by mistake.
    if extension_path:
        time.sleep(4)
    _ensure_window(driver)
    try:
        for h in driver.window_handles:
            driver.switch_to.window(h)
            url = driver.current_url or ""
            if not url.startswith("chrome-extension://") and not url.startswith("devtools://"):
                break
    except Exception:
        _ensure_window(driver)

    def _cleanup():
        proc = getattr(driver, "browser_pid", None)
        try:
            driver.quit()
        except Exception:
            pass
        if proc:
            deadline = time.time() + 10
            while time.time() < deadline:
                try:
                    os.kill(proc, 0)
                except OSError:
                    break
                time.sleep(0.5)
            else:
                try:
                    os.kill(proc, signal.SIGKILL)
                except OSError:
                    pass
        # Only delete throwaway profiles; keep the persistent one for reuse.
        if not persistent_profile:
            _remove_worker_profile(profile_path)

    try:
        # Log in ONCE — all tabs share this session/cookies. With a persistent
        # profile this is a no-op after the first run (session already saved).
        if not login(driver, api_url, login_timeout):
            tprint("[FATAL] login failed; not scraping unauthenticated.")
            return stats

        # Open tabs robustly (guarded window.open, not rapid new_window).
        handles = _open_tabs(driver, num_tabs)
        tprint(f"[*] Logged in. Pipelining across {len(handles)} tab(s)...")
        lanes = [{"h": h, "domain": None, "rec": None, "enc": "",
                  "t0": 0.0, "deadline": 0.0} for h in handles]

        no_work = False
        while True:
            active = 0
            # 1. Assign new work to AT MOST ONE idle lane per stagger interval,
            #    so tab navigations don't all trigger Cloudflare simultaneously.
            if not no_work and (time.time() - last_launch) >= stagger:
                idle = next((ln for ln in lanes if ln["domain"] is None), None)
                if idle is not None:
                    rec = pull_domain(api_url, mode)
                    if rec is None:
                        no_work = True
                    else:
                        domain = rec.get("domain_name", "unknown")
                        idle.update(domain=domain, rec=rec, enc=quote(domain, safe=""),
                                    t0=time.time(), deadline=time.time() + scrape_timeout)
                        last_launch = time.time()
                        if _safe_switch(driver, idle["h"]):
                            try:
                                driver.get(mode.build_url(domain))
                            except Exception as e:
                                _finish(driver, api_url, mode, idle, stats,
                                        {"domain_name": domain, "status": "error",
                                         "error": f"navigation error: {e}"})
                        else:
                            # Tab handle is dead — record error, free the lane.
                            _finish(driver, api_url, mode, idle, stats,
                                    {"domain_name": domain, "status": "error",
                                     "error": "tab window unavailable"})

            # 2. Poll busy lanes.
            for lane in lanes:
                if lane["domain"] is not None:
                    active += 1
                    row = None
                    if _safe_switch(driver, lane["h"]):
                        try:
                            row = _accept_parsed(driver, lane["domain"],
                                                 js_template.replace("${domains}", lane["domain"]),
                                                 lane["enc"])
                        except Exception:
                            row = None
                    if row is None and time.time() > lane["deadline"]:
                        row = {"domain_name": lane["domain"], "status": "error",
                               "error": f"scraping timeout ({scrape_timeout}s); no metrics found"}
                    if row is not None:
                        _finish(driver, api_url, mode, lane, stats, row)

            if no_work and active == 0:
                break
            time.sleep(0.4)

        return stats
    except KeyboardInterrupt:
        tprint("\n[*] Interrupted — shutting down browser...")
        return stats
    except Exception as e:
        tprint(f"[FATAL] run_tabs error: {type(e).__name__}: {e}")
        return stats
    finally:
        _cleanup()


def _finish(driver, api_url, mode, lane, stats, row) -> None:
    """Stamp timing, log, post the result, and free the lane."""
    row["elapsed_seconds"] = round(time.time() - lane["t0"], 2)
    row["finished_at"] = datetime.now(timezone.utc).isoformat()
    status = row.get("status", "error")
    stats["total"] += 1
    stats["completed" if status == "completed" else "error"] += 1
    mark = "OK" if status == "completed" else "FAIL"
    tprint(f"  [{stats['total']}] [{mark}] {row['domain_name']} "
           f"DA={row.get('domain_authority', '-')} "
           f"PA={row.get('page_authority', '-')} "
           f"LD={row.get('linking_domains', '-')} "
           f"Spam={row.get('spam_score', '-')} "
           f"({row['elapsed_seconds']:.1f}s)")
    post_result(api_url, mode, lane["rec"], row)
    lane.update(domain=None, rec=None, enc="", t0=0.0, deadline=0.0)


# --------------------------------------------------------------------------- #
# main()
# --------------------------------------------------------------------------- #
def main():
    p = argparse.ArgumentParser(
        description="Moz Checker (test mode) — headed browser, pulls domains "
                    "from a local mock server, scrapes moz.com/domain-analysis.")
    p.add_argument("--api-url", default=DEFAULT_API_URL,
                   help="Mock server base URL (default: %(default)s). "
                        "Must NOT be a production articleinnovator.com host.")
    p.add_argument("--tabs", type=int, default=int(os.environ.get("MOZ_TABS", "1")),
                   help="Number of tabs in the single browser (default: %(default)s). "
                        "The cf-autoclick Cloudflare extension only works on ONE "
                        "active tab, so keep this at 1 unless Cloudflare is disabled.")
    p.add_argument("--workers", type=int, default=None,
                   help="Deprecated alias for --tabs (single browser is always used).")
    p.add_argument("--headless", action="store_true",
                   help="Run headless (default is HEADED / visible).")
    p.add_argument("--chrome", help="Path to chrome/chromium binary")
    p.add_argument("--no-proxy", action="store_true", help="Disable proxies")
    p.add_argument("--webshare-proxy", action="store_true",
                   help="Use Webshare rotating proxy with IP auth")
    p.add_argument("--scrape-timeout", type=int, default=DEFAULT_SCRAPE_TIMEOUT,
                   help="Seconds to wait for metrics per domain (default: %(default)s)")
    p.add_argument("--poll-interval", type=float, default=DEFAULT_POLL_INTERVAL,
                   help="Seconds to wait between pulls when no work (default: %(default)s)")
    p.add_argument("--login-timeout", type=int, default=int(os.environ.get("MOZ_LOGIN_TIMEOUT", "30")),
                   help="Seconds to wait for Moz login to succeed (default: %(default)s)")
    p.add_argument("--stagger", type=float, default=float(os.environ.get("MOZ_STAGGER", "2.0")),
                   help="Seconds between starting tab navigations, to avoid tripping "
                        "Cloudflare on every tab at once (default: %(default)s)")
    default_profile = os.environ.get(
        "MOZ_PROFILE_DIR",
        os.path.join(os.path.dirname(os.path.abspath(__file__)), "chrome-profile"))
    p.add_argument("--profile-dir", default=default_profile,
                   help="Persistent Chrome profile dir; the logged-in session is "
                        "saved here so login is only needed once (default: %(default)s)")
    p.add_argument("--ephemeral", action="store_true",
                   help="Use a throwaway profile instead of the persistent one "
                        "(forces login every run).")
    args = p.parse_args()

    # (Production runs point --api-url at the management-service gateway.)

    mode = MODES["moz"]
    _install_signal_handlers()

    chrome_bin = find_chrome_binary(args.chrome)
    if not chrome_bin:
        print("[FATAL] No browser binary found. Run tools/setup_vendor.sh to vendor "
              "ungoogled-chromium, or install Chrome/Chromium.", file=sys.stderr)
        sys.exit(2)
    version_main = detect_chrome_major(chrome_bin)

    extension_path = find_cf_extension()
    if extension_path:
        print(f"[*] Cloudflare-bypass extension: {extension_path}")
    else:
        print("[!] cf-autoclick extension not found (vendor/cf-autoclick). "
              "Cloudflare challenges may block scraping. Run tools/setup_vendor.sh.")

    proxies = [] if args.no_proxy else load_proxies()
    if args.webshare_proxy:
        my_ip = webshare_get_my_ip()
        print(f"[*] Public IP: {my_ip}", flush=True)
        try:
            webshare_authorize_ip(my_ip)
        except Exception as e:
            print(f"[FATAL] {e}", file=sys.stderr)
            sys.exit(2)
        atexit.register(webshare_deauthorize_ip)
        proxies = [f"{WEBSHARE_PROXY_HOST}:{WEBSHARE_PROXY_PORT}"]
        time.sleep(3)

    # --workers is a deprecated alias for --tabs (we always use one browser now).
    num_tabs = args.workers if args.workers else args.tabs
    num_tabs = max(1, num_tabs)

    if proxies:
        # A single browser can only route through one proxy; use the first.
        os.environ.setdefault("_MOZ_NOTE", "single-proxy")

    persistent_profile = None if args.ephemeral else args.profile_dir

    print("[*] Moz Checker — test mode (headed, single browser + tabs)")
    print(f"[*] API:     {args.api_url}")
    print(f"[*] Chrome:  {chrome_bin}")
    print(f"[*] Headed:  {not args.headless}")
    print(f"[*] Tabs:    {num_tabs} | Proxy: "
          f"{'disabled' if args.no_proxy or not proxies else f'{len(proxies)} loaded'}")
    print(f"[*] Profile: {persistent_profile or 'ephemeral (login every run)'}")

    initial = _flush_pending_posts(args.api_url, mode, max_per_run=200)
    if initial:
        print(f"[*] Recovered {initial} buffered posts from a previous run")
    _start_buffer_flusher(args.api_url, mode)

    t_start = time.time()
    stats = run_tabs(args.api_url, args.headless, chrome_bin, version_main, mode,
                     num_tabs, args.scrape_timeout, args.login_timeout, args.poll_interval,
                     extension_path=extension_path, stagger=args.stagger,
                     persistent_profile=persistent_profile)
    elapsed = time.time() - t_start
    print(f"\n[*] Done. {stats['total']} domains "
          f"({stats['completed']} completed, {stats['error']} error) in "
          f"{elapsed:.1f}s"
          + (f" — avg {elapsed / stats['total']:.1f}s/domain" if stats['total'] else ""))


if __name__ == "__main__":
    main()
