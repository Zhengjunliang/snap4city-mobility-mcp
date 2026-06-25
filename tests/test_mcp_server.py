"""Unit tests for the local MCP server (mcp_server.py) — the ServiceMap geocode wrapper.

No network: httpx is monkeypatched. Covers the two things the server owns — picking the
right ServiceMap endpoint from excludePOI, and normalizing features to the FeatureCollection
shape the client expects — plus the error path.
"""
import httpx

from snap4city_mobility_mcp import mcp_server
from snap4city_mobility_mcp.mcp_server import _normalize_feature, _servicemap_search


def _install_fake_httpx(monkeypatch, *, body=None, raise_exc=None):
    """Swap mcp_server.httpx.AsyncClient for a fake; return a dict capturing the GET url/params."""
    captured: dict = {}

    class _FakeResp:
        def raise_for_status(self):
            return None

        def json(self):
            return body

    class _FakeAsyncClient:
        def __init__(self, *args, **kwargs):  # accept follow_redirects= etc.
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def get(self, url, params=None, timeout=None):
            captured["url"] = url
            captured["params"] = params
            if raise_exc is not None:
                raise raise_exc
            return _FakeResp()

    monkeypatch.setattr(mcp_server.httpx, "AsyncClient", _FakeAsyncClient)
    return captured


def test_normalize_maps_name_to_address_for_fulltext():
    # Full-text features carry properties.name (no address/city/score); the client reads
    # `address`, so name must land there while the original name is kept.
    feat = {"geometry": {"coordinates": [11.2556, 43.7731]}, "properties": {"name": "Duomo"}}
    out = _normalize_feature(feat)
    assert out["properties"]["address"] == "Duomo"
    assert out["properties"]["name"] == "Duomo"
    assert out["geometry"]["coordinates"] == [11.2556, 43.7731]


def test_normalize_keeps_address_city_score_for_location():
    feat = {
        "geometry": {"coordinates": [11.25, 43.77]},
        "properties": {"address": "VIA ZARA", "city": "FIRENZE", "score": 12.6, "name": None},
    }
    out = _normalize_feature(feat)["properties"]
    assert out == {"address": "VIA ZARA", "city": "FIRENZE", "score": 12.6, "name": None}


async def test_search_excludepoi_true_hits_location_endpoint(monkeypatch):
    body = {"features": [{"geometry": {"coordinates": [11.25, 43.77]}, "properties": {"city": "FIRENZE"}}]}
    captured = _install_fake_httpx(monkeypatch, body=body)
    out = await _servicemap_search("via zara", excludePOI=True, lang="it", maxresults=100)
    assert captured["url"].endswith("/api/v1/location/")
    assert captured["params"]["search"] == "via zara"
    assert out["type"] == "FeatureCollection" and out["count"] == 1


async def test_search_excludepoi_false_hits_fulltext_endpoint(monkeypatch):
    body = {"features": []}
    captured = _install_fake_httpx(monkeypatch, body=body)
    out = await _servicemap_search("Duomo", excludePOI=False, lang="it", maxresults=100)
    assert captured["url"].endswith("/api/v1")  # full-text base, no /location/
    assert out == {"type": "FeatureCollection", "features": [], "count": 0}


async def test_search_network_error_returns_error(monkeypatch):
    _install_fake_httpx(monkeypatch, raise_exc=httpx.ConnectError("boom"))
    out = await _servicemap_search("Duomo", excludePOI=True, lang="it", maxresults=100)
    assert "error" in out and "servicemap search failed" in out["error"]


async def test_search_missing_feature_list_returns_error(monkeypatch):
    _install_fake_httpx(monkeypatch, body={"unexpected": "shape"})
    out = await _servicemap_search("Duomo", excludePOI=True, lang="it", maxresults=100)
    assert "error" in out
