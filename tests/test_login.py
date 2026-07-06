"""Property 10: credentials come from the server, not the code (Req 12)."""
import os
import threading
from http.server import ThreadingHTTPServer

import pytest
import requests

import mock_server
import moz_checker


def _start(monkeypatch, tmp_path, creds_file_content=None, env_email=None, env_pass=None):
    # Point the mock server at an isolated credentials source.
    if creds_file_content is not None:
        cf = tmp_path / "credentials.json"
        cf.write_text(creds_file_content)
        monkeypatch.setattr(mock_server, "CREDENTIALS_FILE", str(cf))
    else:
        monkeypatch.setattr(mock_server, "CREDENTIALS_FILE", str(tmp_path / "nope.json"))
    for k in ("MOZ_LOGIN_EMAIL", "MOZ_LOGIN_PASSWORD"):
        monkeypatch.delenv(k, raising=False)
    if env_email:
        monkeypatch.setenv("MOZ_LOGIN_EMAIL", env_email)
    if env_pass:
        monkeypatch.setenv("MOZ_LOGIN_PASSWORD", env_pass)

    httpd = ThreadingHTTPServer(("127.0.0.1", 0), mock_server.Handler)
    port = httpd.server_address[1]
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return httpd, f"http://127.0.0.1:{port}"


def test_credentials_served_from_file(monkeypatch, tmp_path):
    httpd, base = _start(monkeypatch, tmp_path,
                         creds_file_content='{"email": "a@b.com", "password": "s3cret"}')
    try:
        creds = moz_checker.get_credentials(base)
        assert creds == {"email": "a@b.com", "password": "s3cret"}
        r = requests.get(f"{base}/moz-pro-accounts/pool/", timeout=5)
        assert r.status_code == 200
        pool = r.json()["data"]
        assert isinstance(pool, list) and pool[0]["email"] == "a@b.com"
    finally:
        httpd.shutdown(); httpd.server_close()


def test_credentials_served_from_env(monkeypatch, tmp_path):
    httpd, base = _start(monkeypatch, tmp_path, env_email="e@x.com", env_pass="pw")
    try:
        creds = moz_checker.get_credentials(base)
        assert creds == {"email": "e@x.com", "password": "pw"}
    finally:
        httpd.shutdown(); httpd.server_close()


def test_no_credentials_returns_503_and_none(monkeypatch, tmp_path):
    httpd, base = _start(monkeypatch, tmp_path)  # no file, no env
    try:
        r = requests.get(f"{base}/moz-pro-accounts/pool/", timeout=5)
        assert r.status_code == 503
        assert moz_checker.get_credentials(base) is None
    finally:
        httpd.shutdown(); httpd.server_close()


def test_checker_source_has_no_hardcoded_credentials():
    """The checker must not embed the real email/password (Property 10 / Req 12.1)."""
    src = open(moz_checker.__file__, encoding="utf-8").read()
    assert "alphanewscall@gmail.com" not in src
    assert "pfL*PLk8U" not in src


def test_email_is_masked_for_logging():
    masked = moz_checker._mask_email("alphanewscall@gmail.com")
    assert masked == "al***@gmail.com"
    assert "alphanewscall" not in masked
