from __future__ import annotations

import json
import os
import statistics
import subprocess
import sys
import time
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]
PYTHON = ROOT / ".venv" / "Scripts" / "python.exe"
BASE_URL = "http://127.0.0.1:8001"


def _wait_for_api(client: httpx.Client, timeout_seconds: float = 90) -> None:
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        try:
            response = client.get("/api/health")
            if response.status_code == 200:
                return
        except httpx.HTTPError:
            pass
        time.sleep(0.2)
    raise RuntimeError("temporary API did not become healthy")


def main() -> int:
    environment = os.environ.copy()
    environment["PYTHONUTF8"] = "1"
    process = subprocess.Popen(
        [
            str(PYTHON),
            "-m",
            "uvicorn",
            "cosmos_quant.api:app",
            "--app-dir",
            "backend",
            "--host",
            "127.0.0.1",
            "--port",
            "8001",
        ],
        cwd=ROOT,
        env=environment,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
    )
    failure: BaseException | None = None
    try:
        with httpx.Client(base_url=BASE_URL, timeout=10, trust_env=False) as client:
            _wait_for_api(client)
            acceptance = client.get("/api/acceptance/product")
            acceptance.raise_for_status()
            datasets = client.get("/api/research/datasets?limit=200")
            datasets.raise_for_status()
            candidates = [
                item
                for item in datasets.json()
                if item["id"] == "ds_b37b1cb8c74e65542fb9b601"
            ]
            if not candidates:
                candidates = [
                    item
                    for item in datasets.json()
                    if item["symbol"] == "SOL/USDT"
                    and item["timeframe"] == "1h"
                    and item["candle_count"] >= 8000
                ]
            if not candidates:
                raise RuntimeError("no uncached SOL/USDT 1h verification dataset")
            dataset_id = candidates[0]["id"]
            queued = client.post(
                "/api/research/campaign-jobs",
                json={
                    "dataset_ids": [dataset_id],
                    "library_entry_ids": ["builtin_oi_confirmed_donchian_40_v3"],
                    "max_backtests": 1,
                },
            )
            queued.raise_for_status()
            job = queued.json()
            latencies: list[float] = []
            health_failures = 0
            deadline = time.monotonic() + 180
            while job["status"] in {"queued", "running"} and time.monotonic() < deadline:
                started = time.perf_counter()
                try:
                    health = client.get("/api/health")
                    health.raise_for_status()
                    latencies.append(time.perf_counter() - started)
                except httpx.HTTPError:
                    health_failures += 1
                time.sleep(0.1)
                response = client.get(f"/api/research/campaign-jobs/{job['id']}")
                response.raise_for_status()
                job = response.json()
            if job["status"] != "completed":
                raise RuntimeError(f"verification Campaign did not complete: {job}")
            ordered = sorted(latencies)
            p95 = ordered[max(int(len(ordered) * 0.95) - 1, 0)] if ordered else float("inf")
            result = {
                "job_id": job["id"],
                "report_id": job["report_id"],
                "dataset_id": dataset_id,
                "cache_hits": job["cache_hits"],
                "computed_cells": job["computed_cells"],
                "health_samples": len(latencies),
                "health_failures": health_failures,
                "health_latency_median_ms": round(statistics.median(latencies) * 1000, 2),
                "health_latency_p95_ms": round(p95 * 1000, 2),
                "acceptance_http_status": acceptance.status_code,
            }
            if health_failures or p95 >= 1:
                raise RuntimeError(f"API responsiveness failed: {result}")
            artifact = ROOT / "output" / "acceptance" / "api-isolation.json"
            artifact.parent.mkdir(parents=True, exist_ok=True)
            artifact.write_text(
                json.dumps(result, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            print(json.dumps(result, ensure_ascii=False))
            return 0
    except BaseException as exc:
        failure = exc
        raise
    finally:
        if os.name == "nt":
            subprocess.run(
                ["taskkill", "/PID", str(process.pid), "/T", "/F"],
                capture_output=True,
                check=False,
            )
            process.wait(timeout=10)
        else:
            process.terminate()
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)
        if failure is not None and process.stderr:
            sys.stderr.write(process.stderr.read()[-4000:])


if __name__ == "__main__":
    raise SystemExit(main())
