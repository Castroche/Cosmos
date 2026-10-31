from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from time import perf_counter

from fastapi import HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import ValidationError

from .acceptance import ProductAcceptanceReport

logger = __import__("logging").getLogger(__name__)


class OperationsService:
    async def operational_headers_and_metrics(self, request: Request, call_next):
        started = perf_counter()
        status = 500
        try:
            response = await call_next(request)
            status = response.status_code
        finally:
            route = request.scope.get("route")
            # Unauthenticated and unmatched paths must not create unbounded metric labels.
            path = getattr(route, "path", "<unmatched>")
            method = (
                request.method
                if request.method
                in {
                    "GET",
                    "HEAD",
                    "POST",
                    "PUT",
                    "PATCH",
                    "DELETE",
                    "OPTIONS",
                    "TRACE",
                    "CONNECT",
                }
                else "OTHER"
            )
            self.metrics.increment(
                "http_requests_total",
                method=method,
                path=path,
                status=str(status),
            )
            self.metrics.increment(
                "http_request_duration_seconds_total",
                perf_counter() - started,
                method=method,
                path=path,
            )
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=()"
        if request.url.path.startswith("/api/"):
            response.headers["Cache-Control"] = "no-store"
            content_type = response.headers.get("Content-Type", "")
            if content_type.startswith("application/json") and "charset=" not in content_type:
                response.headers["Content-Type"] = "application/json; charset=utf-8"
        return response

    async def prometheus_metrics(self):
        state = self.risk_state_service.get()
        self.metrics.gauge("trading_halted", 1 if state.halted else 0)
        if self.runtime.reconciliation_report:
            report = self.runtime.reconciliation_report
            self.metrics.gauge("orders_uncertain", len(report.uncertain_order_ids))
            self.metrics.gauge("orders_unprotected", len(report.unprotected_order_ids))
            self.metrics.gauge(
                "positions_unknown",
                len(report.unknown_position_symbols),
            )
        return self.metrics.render_prometheus()

    async def readiness(self):
        checks: dict[str, str | bool] = {
            # PRAGMA integrity_check scans the complete database and can take minutes
            # for multi-gigabyte research ledgers. Keep the API event loop responsive
            # while the explicit readiness probe performs that deep check.
            "database": await asyncio.to_thread(self.store.integrity_check),
            "configuration": True,
        }
        try:
            await self.runtime.initialize()
            checks["runtime"] = True
        except Exception as exc:
            checks["runtime"] = str(exc)
        state = self.risk_state_service.get()
        checks["trading_halted"] = state.halted
        checks["critical_workers"] = bool(self.critical_workers) and all(
            not task.done() for task in self.critical_workers.values()
        )
        ready = (
            checks["database"] == "ok"
            and checks.get("runtime") is True
            and checks["critical_workers"] is True
        )
        if not ready:
            raise HTTPException(503, {"ready": False, "checks": checks})
        return {"ready": True, "checks": checks}

    async def create_backup(self):
        try:
            result = await asyncio.to_thread(self.backup_service.create)
        except Exception as exc:
            self.metrics.increment("backup_failures_total")
            raise HTTPException(500, f"备份失败：{exc}") from exc
        self.metrics.increment("backups_total")
        self.add_audit("maintenance", "数据库在线备份已完成", path=result.path)
        return result

    async def latest_backup(self):
        return await asyncio.to_thread(self.backup_service.latest)

    async def health(self):
        return {
            "status": "ok",
            "version": "2.0.0",
            "server_time": datetime.now(UTC),
        }

    async def worker_readiness(self):
        workers = {name: not task.done() for name, task in self.critical_workers.items()}
        ready = bool(workers) and all(workers.values())
        return JSONResponse(
            {
                "status": "ready" if ready else "degraded",
                "workers": workers,
                "risk_halted": self.risk_state_service.get().halted,
            },
            status_code=200 if ready else 503,
        )

    async def product_acceptance(self):
        if not self.PRODUCT_ACCEPTANCE_REPORT_PATH.exists():
            raise HTTPException(
                404,
                "product acceptance report is unavailable; run scripts/acceptance_audit.py",
            )
        try:
            payload = await asyncio.to_thread(
                self.PRODUCT_ACCEPTANCE_REPORT_PATH.read_text,
                encoding="utf-8",
            )
            return ProductAcceptanceReport.model_validate_json(payload)
        except (OSError, ValidationError, ValueError) as exc:
            raise HTTPException(500, f"product acceptance report is invalid: {exc}") from exc
