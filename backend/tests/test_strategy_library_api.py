from app_fixture import services as api
from cosmos_quant.storage import SqliteStore
from cosmos_quant.strategy_library import list_strategy_library
from fastapi.testclient import TestClient


def test_personal_strategy_archive_filter_and_restore(tmp_path, monkeypatch):
    monkeypatch.setattr(api, "store", SqliteStore(tmp_path / "cosmos.db"))
    monkeypatch.setattr(api, "add_audit", lambda *args, **kwargs: None)
    client = TestClient(
        api.app,
        headers={},
    )
    response = client.post(
        "/api/strategy-library",
        json={"name": "Archive regression", "strategy_spec": list_strategy_library()[0].spec},
    )
    assert response.status_code == 200
    item_id = response.json()["id"]
    assert client.get("/api/strategy-library?origin=personal").json()["total"] == 1
    assert client.post(f"/api/strategy-library/{item_id}/archive").status_code == 200
    assert client.get("/api/strategy-library?origin=personal").json()["total"] == 0
    archived = client.get("/api/strategy-library?origin=personal&include_hidden=true").json()
    assert archived["items"][0]["id"] == item_id
    assert archived["items"][0]["can_restore"] is True
    assert client.post(f"/api/strategy-library/{item_id}/restore").status_code == 200
    assert client.get("/api/strategy-library?origin=personal").json()["total"] == 1
