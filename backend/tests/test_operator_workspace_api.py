from uuid import uuid4

from cosmos_quant import api
from fastapi.testclient import TestClient

client = TestClient(api.app)


def _launch_request(profile_id: str) -> dict:
    settings = client.get("/api/settings").json()["configuration"]
    mode = {
        "paper": "paper",
        "sandbox": "sandbox",
        "live": "live_capped",
    }[settings["trading_mode"]]
    return {
        "profile_id": profile_id,
        "symbols": [settings["strategy"]["watchlist"][0]],
        "timeframes": [settings["strategy"]["timeframes"][0]],
        "autonomy_mode": mode,
        "objective": "持续感知授权市场，仅在策略验证与硬风控通过后自动交易",
        "poll_seconds": 60,
    }


def test_quick_launch_requires_one_authorization_and_reuses_goal():
    request = _launch_request(f"test-launch-{uuid4().hex[:10]}")

    preview = client.post("/api/agent/launch/preview", json=request)
    assert preview.status_code == 200, preview.text
    assert preview.json()["confirmation_required"] is True

    rejected = client.post("/api/agent/launch", json=request)
    assert rejected.status_code == 409

    authorized = client.post(
        "/api/agent/launch",
        json={
            **request,
            "authorize": True,
            "preview_fingerprint": preview.json()["grant_fingerprint"],
        },
    )
    assert authorized.status_code == 200, authorized.text
    first = authorized.json()
    assert first["reused"] is False

    second_preview = client.post("/api/agent/launch/preview", json=request)
    assert second_preview.status_code == 200
    assert second_preview.json()["confirmation_required"] is False
    assert second_preview.json()["reuses_goal_id"] == first["goal_id"]

    repeated = client.post("/api/agent/launch", json=request)
    assert repeated.status_code == 200, repeated.text
    assert repeated.json()["reused"] is True
    assert repeated.json()["goal_id"] == first["goal_id"]

    overview = client.get("/api/agent/overview")
    assert overview.status_code == 200
    goal = next(item for item in overview.json()["goals"] if item["goal_id"] == first["goal_id"])
    assert goal["market_sense"]
    assert goal["runtime_status"] in {"running", "waiting", "blocked", "offline"}

    archived = client.post(f"/api/agent/goals/{first['goal_id']}/archive")
    assert archived.status_code == 200
    assert archived.json()["status"] == "archived"
    restored = client.post(f"/api/agent/goals/{first['goal_id']}/restore")
    assert restored.status_code == 200
    # Restoring is deliberately safe: the operator must explicitly resume execution.
    assert restored.json()["status"] == "paused"


def test_personal_strategy_library_lifecycle_and_builtin_visibility():
    contract = client.get("/api/research/strategy-spec")
    assert contract.status_code == 200
    spec = contract.json()["defaults"]["trend"]
    name = f"人工趋势策略 {uuid4().hex[:8]}"

    created = client.post(
        "/api/strategy-library",
        json={"name": name, "description": "接口生命周期验收", "strategy_spec": spec},
    )
    assert created.status_code == 200, created.text
    item = created.json()
    assert item["origin"] == "personal"
    assert item["can_edit"] is True
    assert item["can_delete"] is True

    listed = client.get("/api/strategy-library", params={"search": name})
    assert listed.status_code == 200
    assert [entry["id"] for entry in listed.json()["items"]] == [item["id"]]

    updated = client.patch(
        f"/api/strategy-library/{item['id']}",
        json={"description": "已更新的可视化编辑器草稿"},
    )
    assert updated.status_code == 200
    assert updated.json()["description"] == "已更新的可视化编辑器草稿"

    archived = client.post(f"/api/strategy-library/{item['id']}/archive")
    assert archived.status_code == 200
    assert archived.json()["can_restore"] is True
    restored = client.post(f"/api/strategy-library/{item['id']}/restore")
    assert restored.status_code == 200
    assert restored.json()["status"] == "draft"
    deleted = client.delete(f"/api/strategy-library/{item['id']}")
    assert deleted.status_code == 200
    assert deleted.json()["deleted"] is True

    builtins = client.get("/api/strategy-library", params={"origin": "builtin"}).json()["items"]
    builtin = builtins[0]
    hidden = client.put(
        f"/api/strategy-library/{builtin['id']}/preference",
        json={"hidden": True},
    )
    assert hidden.status_code == 200
    assert hidden.json()["hidden"] is True
    cannot_delete = client.delete(f"/api/strategy-library/{builtin['id']}")
    assert cannot_delete.status_code == 404
    restored_builtin = client.put(
        f"/api/strategy-library/{builtin['id']}/preference",
        json={"hidden": False},
    )
    assert restored_builtin.status_code == 200
    assert restored_builtin.json()["hidden"] is False


def test_audit_event_page_supports_filters_and_cursor():
    page = client.get(
        "/api/audit/events",
        params={"category": "strategy", "severity": "info", "limit": 1},
    )
    assert page.status_code == 200, page.text
    payload = page.json()
    assert len(payload["items"]) <= 1
    assert all(item["category"] == "strategy" for item in payload["items"])
    assert all(item["severity"] == "info" for item in payload["items"])
    if payload["next_cursor"] is not None:
        next_page = client.get(
            "/api/audit/events",
            params={
                "category": "strategy",
                "severity": "info",
                "limit": 1,
                "cursor": payload["next_cursor"],
            },
        )
        assert next_page.status_code == 200
