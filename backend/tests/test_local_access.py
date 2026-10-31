import asyncio
from types import SimpleNamespace

import pytest
from app_fixture import services as api
from cosmos_quant.demo import demo_candles, snapshot_from_candles
from cosmos_quant.local_access import LocalAccess
from cosmos_quant.runtime import NetworkFreeRuntime
from fastapi.testclient import TestClient
from httpx import ASGITransport, AsyncClient
from starlette.websockets import WebSocketDisconnect


@pytest.fixture
def local_api(monkeypatch):
    candles = demo_candles()
    monkeypatch.setattr(api, "local_access", LocalAccess(["http://test"]))
    monkeypatch.setattr(
        api, "runtime", NetworkFreeRuntime(
            api.configuration_service, candles, snapshot_from_candles(candles)
        )
    )


def test_workbench_apis_need_no_key_cookie_or_session(local_api):
    async def run():
        async with AsyncClient(
            transport=ASGITransport(app=api.app), base_url="http://test"
        ) as client:
            for path in (
                "/api/settings", "/api/ai/settings", "/api/dashboard",
                "/api/health", "/openapi.json",
            ):
                response = await client.get(path)
                assert response.status_code == 200, (path, response.text)
                assert "set-cookie" not in response.headers
            assert not client.cookies
            assert not (api.env_settings.cosmos_data_dir / "operator-access.key").exists()
            # Configuration updates reach request validation with no pairing.
            for headers in ({}, {"Origin": "http://test"}):
                response = await client.put("/api/settings", json={}, headers=headers)
                assert response.status_code == 422

    asyncio.run(run())


@pytest.mark.parametrize("headers", [
    {"Origin": "https://untrusted.example"},
    {"Origin": "null"},
    {"Sec-Fetch-Site": "cross-site"},
    {"Sec-Fetch-Site": "same-site"},
])
def test_other_websites_cannot_read_or_write_local_api(local_api, headers):
    async def run():
        async with AsyncClient(
            transport=ASGITransport(app=api.app), base_url="http://test"
        ) as client:
            assert (await client.get("/api/settings", headers=headers)).status_code == 403
            response = await client.put("/api/settings", json={}, headers=headers)
            assert response.status_code == 403

    asyncio.run(run())


def test_invalid_host_is_rejected(local_api):
    async def run():
        async with AsyncClient(
            transport=ASGITransport(app=api.app), base_url="http://untrusted.example"
        ) as client:
            assert (await client.get("/api/settings")).status_code == 400

    asyncio.run(run())


def test_market_stream_needs_no_session_and_checks_origin(local_api, monkeypatch):
    initial = {"type": "market", "test": "local-stream"}
    queue = asyncio.Queue()
    unsubscribed = []

    async def market_context(**kwargs):
        return None

    monkeypatch.setattr(api, "runtime", SimpleNamespace(
        market_context=market_context,
        market_data=SimpleNamespace(
            subscribe=lambda: queue,
            unsubscribe=unsubscribed.append,
            stream_event=lambda symbol, timeframe: initial,
        ),
    ))
    client = TestClient(api.app, base_url="http://test")
    with client.websocket_connect(
        "/api/stream/market", headers={"Origin": "http://test"}
    ) as socket:
        assert socket.receive_json() == initial
    assert unsubscribed == [queue]
    for headers in ({}, {"Origin": "https://untrusted.example"}):
        with (
            pytest.raises(WebSocketDisconnect) as rejected,
            client.websocket_connect("/api/stream/market", headers=headers),
        ):
            pass
        assert rejected.value.code == 1008
