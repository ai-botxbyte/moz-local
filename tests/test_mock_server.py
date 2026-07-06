"""Property 5 (serve-once), 6 (no-work distinguishable), 7 (post validation)."""
import json
import threading
from http.server import ThreadingHTTPServer

import pytest
import requests

import mock_server


@pytest.fixture
def server(tmp_path, monkeypatch):
    # Isolate results to a temp file and reset the module-level queues.
    results_file = tmp_path / "moz_results.jsonl"
    monkeypatch.setattr(mock_server, "RESULTS_DIR", str(tmp_path))
    monkeypatch.setattr(mock_server, "RESULTS_FILE", str(results_file))
    mock_server._queue.clear()
    mock_server._served.clear()

    domains = [f"d{i}.com" for i in range(8)]
    mock_server._queue.extend(domains)

    httpd = ThreadingHTTPServer(("127.0.0.1", 0), mock_server.Handler)
    port = httpd.server_address[1]
    t = threading.Thread(target=httpd.serve_forever, daemon=True)
    t.start()
    base = f"http://127.0.0.1:{port}"
    try:
        yield base, domains, results_file
    finally:
        httpd.shutdown()
        httpd.server_close()


def test_serve_once_and_from_source(server):
    """Every served domain is unique and drawn from the source (Property 5)."""
    base, domains, _ = server
    seen = []
    while True:
        r = requests.get(f"{base}/moz/", timeout=5)
        if r.status_code == 204:
            break
        assert r.status_code == 200
        d = r.json()["data"]["domain_name"]
        seen.append(d)
    assert sorted(seen) == sorted(domains)
    assert len(seen) == len(set(seen))  # no duplicates


def test_no_work_is_distinguishable_and_error_free(server):
    """Exhaustion returns 204 (no error), distinct from a 200 (Property 6)."""
    base, domains, _ = server
    for _ in domains:
        assert requests.get(f"{base}/moz/", timeout=5).status_code == 200
    r = requests.get(f"{base}/moz/", timeout=5)
    assert r.status_code == 204
    assert r.content == b""  # no body, not an error payload


def test_post_rejects_missing_domain(server):
    """A post without domain_name is rejected and not persisted (Property 7)."""
    base, _, results_file = server
    r = requests.post(f"{base}/moz/", json={"execution_record": {},
                                            "result": {"status": "completed",
                                                       "domain_authority": "50"}}, timeout=5)
    assert r.status_code == 400
    assert not results_file.exists()


def test_post_rejects_completed_without_metrics(server):
    """A completed post with no metrics is rejected and not persisted (Property 7)."""
    base, _, results_file = server
    r = requests.post(f"{base}/moz/", json={
        "execution_record": {"domain_name": "d.com"},
        "result": {"domain_name": "d.com", "status": "completed"}}, timeout=5)
    assert r.status_code == 400
    assert not results_file.exists()


def test_valid_post_is_persisted_and_readable(server):
    """A valid post persists exactly once and is re-readable (Property 7)."""
    base, _, results_file = server
    payload = {
        "execution_record": {"domain_name": "406mtsports.com", "execution_id": "abc"},
        "result": {"domain_name": "406mtsports.com", "status": "completed",
                   "domain_authority": "50", "spam_score": "30%"},
    }
    r = requests.post(f"{base}/moz/", json=payload, timeout=5)
    assert r.status_code == 200 and r.json()["success"] is True

    lines = [json.loads(ln) for ln in results_file.read_text().splitlines() if ln.strip()]
    assert len(lines) == 1
    assert lines[0]["result"]["domain_name"] == "406mtsports.com"
    assert lines[0]["result"]["domain_authority"] == "50"


def test_error_result_is_retained(server):
    """An error result (no metrics) is still accepted so the record survives (P4)."""
    base, _, results_file = server
    payload = {
        "execution_record": {"domain_name": "blocked.com"},
        "result": {"domain_name": "blocked.com", "status": "error",
                   "error": "scraping timeout"},
    }
    r = requests.post(f"{base}/moz/", json=payload, timeout=5)
    assert r.status_code == 200
    lines = results_file.read_text().splitlines()
    assert len(lines) == 1
