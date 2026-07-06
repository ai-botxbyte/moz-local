"""
Mock server for moz-local (test mode).

Replaces the production management-service API. Serves domains to workers and
persists posted results — pure Python stdlib, no extra dependencies.

Endpoints:
  GET  /moz/     -> {"success": true, "data": {"domain_name", "execution_id"}}
                    or 204 No Content when no work remains.
  POST /moz/     -> body {"execution_record": {...}, "result": {...}}
                    validates domain_name + metrics, appends valid results to
                    results/moz_results.jsonl, returns {"success": true}.
  GET  /health   -> {"status": "ok"}

Domains are loaded from a configurable source file (--domains / MOCK_DOMAINS_FILE,
default test_domains.txt). Each domain is served at most once per run.

Usage:
    python mock_server.py [--port 8000] [--domains test_domains.txt]
"""

import argparse
import json
import os
import sys
import threading
import uuid
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
RESULTS_DIR = os.path.join(HERE, "results")
RESULTS_FILE = os.path.join(RESULTS_DIR, "moz_results.jsonl")
CREDENTIALS_FILE = os.environ.get(
    "MOCK_CREDENTIALS_FILE", os.path.join(HERE, "credentials.json"))

# Core metric keys — a valid posted result must carry at least one.
CORE_METRIC_KEYS = ("domain_authority", "page_authority", "linking_domains", "spam_score")

_lock = threading.Lock()
_queue: list = []          # remaining domains to serve
_served: set = set()       # domains already handed out this run


def load_domains(path: str) -> list:
    if not os.path.exists(path):
        raise SystemExit(f"[FATAL] domains source file not found: {path}")
    with open(path, "r", encoding="utf-8") as fh:
        domains = [ln.strip() for ln in fh if ln.strip() and not ln.strip().startswith("#")]
    if not domains:
        raise SystemExit(f"[FATAL] domains source file is empty: {path}")
    return domains


def load_credentials() -> dict:
    """Return the Moz login credentials from a file or env vars.

    Precedence: credentials.json (MOCK_CREDENTIALS_FILE) -> MOZ_LOGIN_EMAIL /
    MOZ_LOGIN_PASSWORD env vars. Returns {} when nothing is configured so the
    endpoint can respond with a 503 rather than serving empty creds.
    """
    if os.path.exists(CREDENTIALS_FILE):
        try:
            with open(CREDENTIALS_FILE, encoding="utf-8") as fh:
                data = json.load(fh)
            if data.get("email") and data.get("password"):
                return {"email": data["email"], "password": data["password"]}
        except Exception:
            pass
    email = os.environ.get("MOZ_LOGIN_EMAIL")
    password = os.environ.get("MOZ_LOGIN_PASSWORD")
    if email and password:
        return {"email": email, "password": password}
    return {}


def result_has_metrics(result: dict) -> bool:
    if not isinstance(result, dict):
        return False
    return any(result.get(k) not in (None, "") for k in CORE_METRIC_KEYS)


def persist_result(execution_record: dict, result: dict) -> None:
    os.makedirs(RESULTS_DIR, exist_ok=True)
    with _lock:
        with open(RESULTS_FILE, "a", encoding="utf-8") as fh:
            fh.write(json.dumps({
                "received_at": datetime.now(timezone.utc).isoformat(),
                "execution_record": execution_record,
                "result": result,
            }) + "\n")


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        pass  # quiet

    def _send_json(self, code: int, payload: dict) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path.rstrip("/") == "/health":
            self._send_json(200, {"status": "ok"})
            return
        # Mirrors management-service GET /moz-pro-accounts/pool/ (list of accounts).
        if self.path.rstrip("/") in ("/moz-pro-accounts/pool", "/credentials"):
            creds = load_credentials()
            if not creds:
                self._send_json(503, {"success": False,
                                      "error": "no credentials configured"})
                return
            self._send_json(200, {"success": True, "data": [
                {"name": creds["email"], "email": creds["email"],
                 "password": creds["password"]}
            ]})
            return
        if self.path.rstrip("/") in ("/moz-pro", "/moz"):
            with _lock:
                domain = None
                while _queue:
                    candidate = _queue.pop(0)
                    if candidate not in _served:
                        _served.add(candidate)
                        domain = candidate
                        break
            if domain is None:
                # No work — 204, observably distinguishable from a domain-bearing 200.
                self.send_response(204)
                self.end_headers()
                return
            self._send_json(200, {"success": True, "data": {
                "domain_name": domain,
                "execution_id": uuid.uuid4().hex,
            }})
            return
        self._send_json(404, {"success": False, "error": "not found"})

    def do_POST(self):
        if self.path.rstrip("/") not in ("/moz-pro", "/moz"):
            self._send_json(404, {"success": False, "error": "not found"})
            return
        try:
            length = int(self.headers.get("Content-Length", 0))
            body = json.loads(self.rfile.read(length) or b"{}")
        except Exception as e:
            self._send_json(400, {"success": False, "error": f"invalid JSON: {e}"})
            return

        execution_record = body.get("execution_record") or {}
        result = body.get("result") or {}
        domain_name = result.get("domain_name") or execution_record.get("domain_name")

        if not domain_name:
            self._send_json(400, {"success": False, "error": "missing domain_name"})
            return
        # A completed result must carry metrics; an error result is accepted so
        # the record is retained (Req 4.5) but only completed ones need metrics.
        if result.get("status") == "completed" and not result_has_metrics(result):
            self._send_json(400, {"success": False,
                                  "error": "completed result missing metrics"})
            return

        persist_result(execution_record, result)
        self._send_json(200, {"success": True})


def main():
    p = argparse.ArgumentParser(description="Mock server for moz-local (test mode)")
    p.add_argument("--port", type=int, default=int(os.environ.get("MOCK_PORT", "8000")))
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--domains", default=os.environ.get(
        "MOCK_DOMAINS_FILE", os.path.join(HERE, "test_domains.txt")))
    args = p.parse_args()

    domains = load_domains(args.domains)
    with _lock:
        _queue.extend(domains)
    print(f"[mock] Loaded {len(domains)} domains from {args.domains}", flush=True)
    creds = load_credentials()
    if creds:
        print(f"[mock] Credentials configured for {creds['email']}", flush=True)
    else:
        print("[mock] WARNING: no credentials configured (/credentials returns 503)", flush=True)
    print(f"[mock] Results -> {RESULTS_FILE}", flush=True)
    print(f"[mock] Listening on http://{args.host}:{args.port}  "
          f"(GET/POST /moz-pro/, GET /moz-pro-accounts/pool/, GET /health)", flush=True)

    server = ThreadingHTTPServer((args.host, args.port), Handler)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n[mock] Shutting down", flush=True)
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
