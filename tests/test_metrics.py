from git_cache_gateway import __version__
from git_cache_gateway.metrics import ServiceStats, prometheus_response


def test_service_stats_snapshot_counts_requests_and_cache_events():
    stats = ServiceStats()
    stats.inc_request()
    stats.inc_cache("hit")
    stats.inc_cache("miss")
    stats.inc_proxy("upstream")
    stats.add_proxy_bytes("upstream", "upstream_to_client", 123)

    snap = stats.snapshot()

    assert snap["requests_total"] == 1
    assert snap["cache_hits"] == 1
    assert snap["cache_misses"] == 1
    assert snap["proxy"]["upstream_total"] == 1
    assert snap["proxy"]["bytes"]["upstream_to_client"] == 123


def test_prometheus_response_contains_gateway_build_info():
    raw, content_type = prometheus_response()
    text = raw.decode("utf-8")

    assert "text/plain" in content_type
    assert "git_cache_gateway_build_info" in text
    assert f'version="{__version__}"' in text
