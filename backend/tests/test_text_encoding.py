import json

from cosmos_quant.storage import SqliteStore
from cosmos_quant.text_encoding import repair_mojibake


def test_repairs_double_utf8_latin1_mojibake():
    expected = "顺应主要趋势，等待回踩确认后入场。"
    corrupted = expected.encode().decode("latin-1").encode().decode("latin-1")

    assert repair_mojibake(corrupted) == expected


def test_configuration_load_repairs_and_persists_mojibake(tmp_path):
    store = SqliteStore(tmp_path / "encoding.db")
    config, _ = store.load_configuration()
    expected = config.strategy.default_prompt
    corrupted = expected.encode().decode("latin-1").encode().decode("latin-1")
    payload = config.model_dump(mode="json")
    payload["strategy"]["default_prompt"] = corrupted
    with store.connect() as connection:
        connection.execute(
            "INSERT INTO app_configuration (id, payload, updated_at) VALUES (1, ?, ?)",
            (json.dumps(payload), "2026-07-13T00:00:00+00:00"),
        )

    repaired, _ = store.load_configuration()
    assert repaired.strategy.default_prompt == expected
    with store.connect() as connection:
        saved = json.loads(
            connection.execute("SELECT payload FROM app_configuration WHERE id = 1").fetchone()[
                "payload"
            ]
        )
    assert saved["strategy"]["default_prompt"] == expected
