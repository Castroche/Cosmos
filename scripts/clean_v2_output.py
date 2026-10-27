from __future__ import annotations

import argparse
import json
import shutil
from datetime import UTC, datetime
from pathlib import Path

from cosmos_quant.cleanup_v2 import OUTPUT_DELETE_PATHS


def usage(path: Path) -> tuple[int, int]:
    if not path.exists():
        return 0, 0
    if path.is_file():
        return path.stat().st_size, 1
    files = [item for item in path.rglob("*") if item.is_file()]
    return sum(item.stat().st_size for item in files), len(files)


def safe_path(workspace: Path, candidate: Path) -> Path:
    root = workspace.resolve()
    resolved = candidate.resolve()
    if resolved == root or root not in resolved.parents:
        raise RuntimeError(f"refusing unsafe cleanup target: {resolved}")
    return resolved


def main() -> None:
    parser = argparse.ArgumentParser(description="Remove audited obsolete v2 output")
    parser.add_argument("--workspace", type=Path, default=Path.cwd())
    parser.add_argument(
        "--report",
        type=Path,
        default=Path("output/cleanup-v2/output-cleanup.json"),
    )
    args = parser.parse_args()
    workspace = args.workspace.resolve()
    targets = [safe_path(workspace, workspace / item) for item in OUTPUT_DELETE_PATHS]
    output_root = safe_path(workspace, workspace / "output")
    for pattern in ("*.stdout.log", "*.stderr.log"):
        targets.extend(
            safe_path(workspace, path) for path in sorted(output_root.glob(pattern))
        )

    deleted = []
    for target in targets:
        size_bytes, file_count = usage(target)
        if not file_count:
            continue
        if target.is_dir():
            shutil.rmtree(target)
        else:
            target.unlink()
        deleted.append(
            {
                "path": str(target),
                "size_bytes": size_bytes,
                "file_count": file_count,
                "reason": "audited obsolete UI/runtime output",
            }
        )

    report = {
        "schema_version": "cosmos-output-cleanup-v2/1",
        "generated_at": datetime.now(UTC).isoformat(),
        "deleted": deleted,
        "summary": {
            "size_bytes": sum(item["size_bytes"] for item in deleted),
            "file_count": sum(item["file_count"] for item in deleted),
        },
    }
    destination = safe_path(workspace, workspace / args.report)
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(json.dumps(report["summary"], ensure_ascii=False, indent=2))
    print(destination)


if __name__ == "__main__":
    main()
