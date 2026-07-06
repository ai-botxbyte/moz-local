"""Property 2 (completed iff core metric) and Property 3 (null-not-drop)."""
import os

from hypothesis import given, strategies as st

from conftest import FIXTURES
from moz_checker import extract_moz_metrics_from_html, _extract_moz_row

CORE = ("domain_authority", "page_authority", "linking_domains", "spam_score")


def _load(name):
    with open(os.path.join(FIXTURES, name), encoding="utf-8") as fh:
        return fh.read()


def test_real_fixture_full_extraction():
    """The real 406mtsports.com DOM yields all four core metrics + completed."""
    parsed = extract_moz_metrics_from_html(_load("406mtsports.html"), "406mtsports.com")
    row = _extract_moz_row({"results": [parsed]}, "406mtsports.com")
    assert row["status"] == "completed"
    assert row["domain_authority"] == "50"
    assert row["linking_domains"] == "3.3k"
    assert row["spam_score"] == "30%"
    assert row["ranking_keywords"] == "4k"
    # Page Authority derived from the site-root row of Top Pages.
    assert row["page_authority"] == "48"
    assert len(row["top_linking_domains"]) == 3
    assert row["top_linking_domains"][0]["domain"] == "www.google.com"


def _card(label, value):
    return f'<div class="col-md-3"><h5>{label}</h5><h1 class="display-1">{value}</h1></div>'


@given(
    da=st.one_of(st.none(), st.integers(0, 100).map(str)),
    ld=st.one_of(st.none(), st.integers(0, 99999).map(str)),
    spam=st.one_of(st.none(), st.integers(0, 100).map(lambda n: f"{n}%")),
)
def test_completed_iff_core_metric_present(da, ld, spam):
    """status is completed iff >=1 core metric present; missing => null (P2/P3)."""
    parts = ['<span class="text-pink">d.com</span>']
    if da is not None:
        parts.append(_card("Domain Authority", da))
    if ld is not None:
        parts.append(_card("Linking Root Domains", ld))
    if spam is not None:
        parts.append(_card("Spam Score", spam))
    html = "<html><body>" + "".join(parts) + "</body></html>"

    parsed = extract_moz_metrics_from_html(html, "d.com")
    row = _extract_moz_row({"results": [parsed]}, "d.com")

    any_core = any(v is not None for v in (da, ld, spam))
    if any_core:
        assert row["status"] == "completed"
    else:
        assert row["status"] == "error"

    # Property 3: every core field is present (possibly None) and domain kept.
    assert row["domain_name"] == "d.com"
    for k in CORE:
        assert k in row


def test_rate_limited_page_is_error_but_record_retained():
    """A page with no results card => error, but the domain record survives."""
    html = "<html><body><h1>Something went wrong</h1></body></html>"
    parsed = extract_moz_metrics_from_html(html, "blocked.com")
    row = _extract_moz_row({"results": [parsed]}, "blocked.com")
    assert row["status"] == "error"
    assert row["domain_name"] == "blocked.com"
    for k in CORE:
        assert row[k] is None
