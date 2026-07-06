"""Property 1 (URL round-trip) and Property 9 (test-mode host safety)."""
from urllib.parse import urlparse, parse_qs

from hypothesis import given, strategies as st

import moz_checker
from moz_checker import MODES, DEFAULT_API_URL

UNRESERVED = set("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-._~")

# Domain-ish strings, but the property must hold for arbitrary non-empty text too.
domain_strategy = st.text(
    alphabet=st.characters(min_codepoint=33, max_codepoint=0x2fff,
                            blacklist_categories=("Cs",)),
    min_size=1, max_size=40,
).filter(lambda s: s.strip() != "")


@given(domain=domain_strategy)
def test_site_param_round_trips(domain):
    """The domain always round-trips out of the site query param (Property 1)."""
    url = MODES["moz"].build_url(domain)
    qs = parse_qs(urlparse(url).query, keep_blank_values=True)
    assert qs.get("site") == [domain]


@given(domain=domain_strategy)
def test_reserved_chars_are_percent_encoded(domain):
    """Every character outside the unreserved set is percent-encoded (Property 1)."""
    url = MODES["moz"].build_url(domain)
    raw_site = urlparse(url).query.split("site=", 1)[1]
    for ch in raw_site:
        if ch == "%":
            continue
        # Any literal (non-%) char in the encoded string must be unreserved.
        assert ch in UNRESERVED, f"unreserved char leaked: {ch!r} in {raw_site!r}"


def test_default_api_is_loopback_and_not_production():
    """Default API base URL is loopback and never a production host (Property 9)."""
    host = urlparse(DEFAULT_API_URL).hostname or ""
    assert "articleinnovator.com" not in DEFAULT_API_URL
    assert host in ("127.0.0.1", "localhost", "::1")
