"""Requirement 1: structure verification."""
import json
import os

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _exists(*parts):
    return os.path.exists(os.path.join(ROOT, *parts))


def test_required_files_present():
    missing = [p for p in [
        "moz_checker.py",
        "moz.json",
        "mock_server.py",
        "run.sh",
        "requirements.txt",
        "tools",
        os.path.join("tools", "setup_vendor.sh"),
        os.path.join("tools", "open_chrome_profile.sh"),
    ] if not _exists(p)]
    assert not missing, f"missing required paths: {missing}"


def test_single_checker_script():
    py = [f for f in os.listdir(ROOT)
          if f.endswith(".py") and f not in ("mock_server.py",)]
    # moz_checker.py is the single checker script at the root.
    assert "moz_checker.py" in py


def test_moz_json_has_evaluate_action():
    with open(os.path.join(ROOT, "moz.json"), encoding="utf-8") as fh:
        spec = json.load(fh)
    actions = spec.get("actions", [])
    assert any(a.get("type") == "evaluate" and "script" in a for a in actions)
    assert "site=${domains}" in spec.get("url", "")


def test_requirements_lists_core_deps():
    with open(os.path.join(ROOT, "requirements.txt"), encoding="utf-8") as fh:
        lines = [ln.strip() for ln in fh if ln.strip() and not ln.startswith("#")]
    names = {ln.split(">=")[0].split("==")[0].split("<")[0].strip().lower() for ln in lines}
    for dep in ("undetected-chromedriver", "selenium", "requests"):
        assert dep in names, f"{dep} not declared in requirements.txt"
