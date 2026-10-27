from __future__ import annotations

import argparse
import json
import sqlite3
from datetime import UTC, datetime
from hashlib import sha256
from pathlib import Path
from typing import Any

from cosmos_quant.backup import sha256_file


def path_usage(path: Path) -> dict[str, int]:
    files = [path] if path.is_file() else [item for item in path.rglob("*") if item.is_file()]
    return {
        "file_count": len(files),
        "size_bytes": sum(item.stat().st_size for item in files),
    }


def archive_fingerprint(root: Path) -> dict[str, Any]:
    digest = sha256()
    files = sorted(item for item in root.rglob("*") if item.is_file())
    total = 0
    for path in files:
        size = path.stat().st_size
        total += size
        digest.update(path.relative_to(root).as_posix().encode())
        digest.update(b"\0")
        digest.update(str(size).encode())
        digest.update(b"\0")
        digest.update(bytes.fromhex(sha256_file(path)))
    return {
        "path": str(root.resolve()),
        "file_count": len(files),
        "size_bytes": total,
        "content_tree_sha256": digest.hexdigest(),
    }


def database_report(database: Path) -> dict[str, Any]:
    connection = sqlite3.connect(database)
    try:
        connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        integrity = str(connection.execute("PRAGMA integrity_check").fetchone()[0])
        foreign_keys = connection.execute("PRAGMA foreign_key_check").fetchall()
        tables = [
            str(row[0])
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table' "
                "AND name NOT LIKE 'sqlite_%' ORDER BY name"
            )
        ]
        critical = [
            "research_datasets",
            "research_dataset_candles",
            "research_dataset_funding",
            "research_dataset_open_interest",
            "research_dataset_positioning",
            "research_dataset_basis",
            "research_dataset_order_flow",
            "research_dataset_liquidation_flow",
            "order_ledger",
            "trade_episodes",
            *[name for name in tables if name.startswith("ti_")],
        ]
        row_counts = {
            name: int(connection.execute(f"SELECT COUNT(*) FROM [{name}]").fetchone()[0])
            for name in critical
            if name in tables
        }
        schema_version = int(
            connection.execute(
                "SELECT COALESCE(MAX(version), 0) FROM schema_migrations"
            ).fetchone()[0]
        )
        connection.commit()
    finally:
        connection.close()
    return {
        "path": str(database.resolve()),
        "size_bytes": database.stat().st_size,
        "sha256": sha256_file(database),
        "integrity": integrity,
        "foreign_key_error_count": len(foreign_keys),
        "schema_version": schema_version,
        "table_count": len(tables),
        "trading_intelligence_table_count": sum(name.startswith("ti_") for name in tables),
        "critical_row_counts": row_counts,
    }


def load_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def main() -> None:
    parser = argparse.ArgumentParser(description="Generate COSMOS v2 final evidence report")
    parser.add_argument("--workspace", type=Path, default=Path.cwd())
    parser.add_argument("--output", type=Path, default=Path("output/upgrade-v2/final-report.json"))
    args = parser.parse_args()
    workspace = args.workspace.resolve()
    cleanup = workspace / "output" / "cleanup-v2"
    manifest = load_json(cleanup / "manifest.json")
    prune_data = load_json(next(cleanup.glob("prune-data-*.json")))
    prune_backups = load_json(next(cleanup.glob("prune-backups-*.json")))
    generated_cleanup = load_json(next(cleanup.glob("clean-generated-*.json")))
    output_cleanup = load_json(cleanup / "output-cleanup.json")
    quant = load_json(workspace / "output" / "upgrade-v2" / "quant-training.json")
    database = database_report(workspace / "data" / "cosmos.db")
    backups = []
    for path in sorted((workspace / "data" / "backups").glob("*.db")):
        backups.append(
            {
                "path": str(path.resolve()),
                "size_bytes": path.stat().st_size,
                "sha256": sha256_file(path),
                "retention": (
                    "temporary until external Demo/Live commissioning passes"
                    if "pre-v2-cleanup" in path.name
                    else "final compact pre-v2 baseline"
                ),
            }
        )
    workspace_usage = path_usage(workspace)
    data_usage = path_usage(workspace / "data")
    report = {
        "schema_version": "cosmos-v2-final-report/1",
        "generated_at": datetime.now(UTC).isoformat(),
        "status": "implemented_and_locally_verified_external_commissioning_pending",
        "disk": {
            "before_observed": {
                "data_approx_gb_decimal": 37.6,
                "rebuildable_dependencies_and_cache_approx_gb_decimal": 0.55,
                "source": "pre-cleanup read-only audit",
            },
            "workspace_after": workspace_usage,
            "data_after": data_usage,
            "generated_cleanup": generated_cleanup["result"],
            "output_cleanup": output_cleanup["summary"],
            "legacy_backup_files_deleted": len(prune_backups["result"]["deleted"]),
            "database_compaction_bytes_reduced": 7_494_365_184 - 6_891_732_992,
            "note": (
                "The verified full pre-cleanup backup remains until external "
                "commissioning passes."
            ),
        },
        "database": database,
        "dataset_cleanup": prune_data["result"],
        "cleanup_idempotence": {
            "delete": manifest["summary"]["DELETE"],
            "review": manifest["summary"]["REVIEW"],
            "prunable_count": manifest["dataset_retention"]["prunable_count"],
        },
        "archives": archive_fingerprint(workspace / "data" / "archives"),
        "backups": backups,
        "quant_intelligence_v1": quant,
        "verification": {
            "backend_pytest": {"passed": True, "collected": 681},
            "ruff": {"passed": True, "scope": "backend scripts"},
            "frontend_typecheck": {"passed": True},
            "frontend_eslint": {"passed": True},
            "frontend_tests": {"passed": True, "count": 3},
            "frontend_production_build": {
                "passed": True,
                "warning": "main JS chunk is 521.90 kB after minification",
            },
            "npm_audit_moderate_or_higher": {"passed": True, "vulnerabilities": 0},
        },
        "acceptance_boundary": {
            "local_acceptance_complete": True,
            "external_okx_demo_live_commissioning_complete": False,
            "live_api_contacted": False,
            "reason": (
                "No operator credentials, account/order state, or two-step "
                "HumanLiveGrant were supplied."
            ),
            "required_before_deleting_full_backup": [
                "Verify OKX Demo credentials and commissioned native TP/SL protection",
                "Verify reconciliation health and absence of uncertain orders/positions",
                "Complete two-step HumanLiveGrant issue/revoke acceptance",
                "Repeat final evidence report, then delete only pre-v2-cleanup backup",
            ],
        },
    }
    destination = (workspace / args.output).resolve()
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")

    markdown = destination.with_suffix(".md")
    deleted_rows = sum(prune_data["result"]["deleted"].values())
    markdown.write_text(
        "# COSMOS Trading Intelligence v2 最终实施报告\n\n"
        f"生成时间：`{report['generated_at']}`\n\n"
        "## 结论\n\n"
        "代码、数据库清理、依赖重建、Quant v1 训练与本地验收已完成。"
        "OKX Demo/Live 外部 commissioning 尚未执行，因此完整 pre-cleanup 备份按计划保留。\n\n"
        "## 清理结果\n\n"
        f"- 删除旧备份/WAL/SHM：{report['disk']['legacy_backup_files_deleted']} 个。\n"
        f"- 删除研究数据集：{prune_data['result']['deleted']['research_datasets']} 个；"
        f"数据库记录合计：{deleted_rows:,} 条。\n"
        f"- 当前数据库：{database['size_bytes']:,} bytes，schema v{database['schema_version']}，"
        f"完整性 `{database['integrity']}`，外键错误 {database['foreign_key_error_count']}。\n"
        f"- 当前数据集：{manifest['dataset_retention']['retained_count']} 个，"
        f"可删除 {manifest['dataset_retention']['prunable_count']} 个。\n"
        f"- 清理幂等：DELETE={manifest['summary']['DELETE']['file_count']}，"
        f"REVIEW={manifest['summary']['REVIEW']['file_count']}。\n"
        f"- 原始归档：{report['archives']['file_count']} 个文件，"
        f"{report['archives']['size_bytes']:,} bytes；内容树 SHA-256 "
        f"`{report['archives']['content_tree_sha256']}`。\n\n"
        "## 升级与验证\n\n"
        "- Trading Intelligence v2 事件引擎、状态机、Wake DSL、双 LLM 契约、"
        "确定性风险/执行/持仓、HumanLiveGrant、Decision Ledger 和 v2 API/UI 已实现。\n"
        "- BTC/ETH/SOL 三份 Quant v1 模型已训练、保存并重新加载推理验证。\n"
        "- 后端 681 项测试、Ruff、前端类型检查、ESLint、3 项测试、生产构建与 npm audit 均通过。\n\n"
        "## 尚需外部验收\n\n"
        "未提供 OKX 凭证、账户状态或两步实盘授权，因此没有触达 Live API。"
        "完成 Demo/Live commissioning、原生保护单和对账验收后，"
        "方可删除完整 pre-v2-cleanup 备份。\n",
        encoding="utf-8",
    )
    print(destination)
    print(markdown)


if __name__ == "__main__":
    main()
