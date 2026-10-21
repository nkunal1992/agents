"""pulse() is the user-facing line — no internals jargon (#187 polish)."""
from confluence_knowledge.reporting import pulse


def test_pulse_is_plain_language():
    line = pulse({"phase": "fetching", "pages_done": 410,
                  "pages_seen": 1200, "rate_ppm": 42,
                  "errors_n": 3}, rss_mb=1656)
    assert "ppm" not in line and "RSS" not in line
    assert "pages_done" not in line and "fetch_cap" not in line
    assert "410" in line and "1200" in line
    assert "couldn't be read" in line          # errors in human terms


def test_pulse_quiet_when_no_errors():
    line = pulse({"phase": "embedding", "pages_done": 5})
    assert "couldn't be read" not in line
    assert "embedding" in line
