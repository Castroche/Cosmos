from __future__ import annotations

import argparse
import json
import sys
from datetime import UTC, datetime
from pathlib import Path

WORKSPACE = Path(__file__).resolve().parents[1]
BACKEND = WORKSPACE / "backend"
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

from cosmos_quant.cleanup_v2 import (  # noqa: E402
    build_cleanup_manifest,
    compact_database,
    create_verified_backup,
    delete_prunable_datasets,
    prune_legacy_backups,
    remove_rebuild_paths,
    replace_database,
    verify_database,
    write_manifest,
)


def _stamp() -> str:
    return datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")


def main() -> int:
    parser = argparse.ArgumentParser(description="Apply audited COSMOS v2 cleanup stages")
    parser.add_argument(
        "stage",
        choices=("backup", "prune-backups", "prune-data", "compact", "clean-generated"),
    )
    parser.add_argument("--workspace", type=Path, default=WORKSPACE)
    parser.add_argument("--database", type=Path, default=WORKSPACE / "data" / "cosmos.db")
    parser.add_argument("--keep-backup", action="append", type=Path, default=[])
    args = parser.parse_args()
    workspace = args.workspace.resolve()
    database = args.database.resolve()
    report_root = workspace / "output" / "cleanup-v2"
    report_root.mkdir(parents=True, exist_ok=True)

    if args.stage == "backup":
        destination = (
            workspace / "data" / "backups" / f"cosmos-pre-v2-cleanup-{_stamp()}.db"
        )
        result: object = create_verified_backup(database, destination)
    elif args.stage == "prune-backups":
        if not args.keep_backup:
            raise SystemExit("prune-backups requires --keep-backup")
        verified = [verify_database(item.resolve()) for item in args.keep_backup]
        if any(item["integrity"] != "ok" or item["foreign_key_errors"] for item in verified):
            raise SystemExit("one or more retained backups failed verification")
        result = {
            "retained": verified,
            "deleted": prune_legacy_backups(
                workspace / "data" / "backups",
                keep=[item.resolve() for item in args.keep_backup],
            ),
        }
    elif args.stage == "prune-data":
        manifest = build_cleanup_manifest(workspace, database)
        before = manifest["dataset_retention"]
        deleted = delete_prunable_datasets(database, before["prunable_ids"])
        after = build_cleanup_manifest(workspace, database)
        result = {
            "before": {
                "known_count": before["known_count"],
                "retained_count": before["retained_count"],
                "prunable_count": before["prunable_count"],
                "prunable_child_rows": before["prunable_child_rows"],
            },
            "deleted": deleted,
            "after": {
                "known_count": after["dataset_retention"]["known_count"],
                "retained_count": after["dataset_retention"]["retained_count"],
                "prunable_count": after["dataset_retention"]["prunable_count"],
            },
        }
        write_manifest(after, report_root / "manifest-after-prune.json")
    elif args.stage == "compact":
        compacted = database.with_name(f"{database.stem}-compacted-{_stamp()}.db")
        compact_result = compact_database(database, compacted)
        replacement = replace_database(database, compacted)
        baseline = (
            workspace / "data" / "backups" / f"cosmos-pre-v2-upgrade-{_stamp()}.db"
        )
        backup = create_verified_backup(database, baseline)
        result = {
            "compacted": compact_result,
            "replacement": replacement,
            "baseline_backup": backup,
        }
    else:
        result = {"removed": remove_rebuild_paths(workspace)}

    report = {
        "stage": args.stage,
        "completed_at": datetime.now(UTC).isoformat(),
        "result": result,
    }
    report_path = report_root / f"{args.stage}-{_stamp()}.json"
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
