"""Property 8 (buffer fidelity & ordering) and Property 4 (record retention)."""
import json
import os
import tempfile

from hypothesis import given, settings, strategies as st

import moz_checker
from moz_checker import MODES

MODE = MODES["moz"]

result_strategy = st.fixed_dictionaries({
    "domain_name": st.text(min_size=1, max_size=20).filter(lambda s: s.strip()),
    "status": st.sampled_from(["completed", "error"]),
    "domain_authority": st.one_of(st.none(), st.integers(0, 100).map(str)),
    "spam_score": st.one_of(st.none(), st.integers(0, 100).map(lambda n: f"{n}%")),
})


@settings(max_examples=50)
@given(results=st.lists(result_strategy, min_size=1, max_size=8))
def test_buffer_round_trip_and_order(results):
    """Buffered results recover unchanged and in buffer order (Property 8).

    Manages its own isolated buffer file per example (no shared fixtures) so
    each Hypothesis example starts from an empty buffer.
    """
    fd, path = tempfile.mkstemp(suffix=".jsonl")
    os.close(fd)
    os.remove(path)  # start empty
    prev = os.environ.get("MOZ_MOZ_POST_BUFFER")
    os.environ["MOZ_MOZ_POST_BUFFER"] = path
    try:
        for i, res in enumerate(results):
            moz_checker._buffer_failed_post(
                MODE, {"domain_name": res["domain_name"], "execution_id": str(i)}, res)

        with open(path, encoding="utf-8") as fh:
            lines = [json.loads(ln) for ln in fh if ln.strip()]
        assert len(lines) == len(results)  # Property 4: nothing dropped
        for original, stored in zip(results, lines):
            assert stored["result"] == original  # unchanged
        assert [ln["result"]["domain_name"] for ln in lines] == \
               [r["domain_name"] for r in results]  # order preserved
    finally:
        if prev is None:
            os.environ.pop("MOZ_MOZ_POST_BUFFER", None)
        else:
            os.environ["MOZ_MOZ_POST_BUFFER"] = prev
        if os.path.exists(path):
            os.remove(path)


def test_flush_removes_on_success(tmp_path, monkeypatch):
    """A delivered buffered result is removed; failures are retained (Property 8)."""
    buf = tmp_path / "posts.jsonl"
    monkeypatch.setenv("MOZ_MOZ_POST_BUFFER", str(buf))

    # Two buffered posts.
    for i in range(2):
        moz_checker._buffer_failed_post(
            MODE, {"domain_name": f"d{i}.com", "execution_id": str(i)},
            {"domain_name": f"d{i}.com", "status": "completed", "domain_authority": "10"})
    assert len(buf.read_text().splitlines()) == 2

    # Fake a reachable server that accepts everything.
    monkeypatch.setattr(moz_checker, "server_reachable", lambda url: True)

    class OKResp:
        status_code = 200
        def json(self):
            return {"success": True}

    monkeypatch.setattr(moz_checker._HTTP, "post", lambda *a, **k: OKResp())

    flushed = moz_checker._flush_pending_posts("http://127.0.0.1:1", MODE)
    assert flushed == 2
    assert buf.read_text().strip() == ""  # all removed on success


def test_flush_noop_when_unreachable(tmp_path, monkeypatch):
    """Nothing is flushed while the server is unreachable (Property 8 / 11.2)."""
    buf = tmp_path / "posts.jsonl"
    monkeypatch.setenv("MOZ_MOZ_POST_BUFFER", str(buf))
    moz_checker._buffer_failed_post(
        MODE, {"domain_name": "d.com"},
        {"domain_name": "d.com", "status": "completed", "domain_authority": "10"})
    monkeypatch.setattr(moz_checker, "server_reachable", lambda url: False)
    assert moz_checker._flush_pending_posts("http://127.0.0.1:1", MODE) == 0
    assert len(buf.read_text().splitlines()) == 1  # retained
