from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

WORKSPACE = Path(__file__).resolve().parents[1]
BACKEND = WORKSPACE / "backend"
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

from cosmos_quant.cleanup_v2 import build_cleanup_manifest, write_manifest  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description="Audit COSMOS v2 cleanup candidates")
    parser.add_argument("--workspace", type=Path, default=WORKSPACE)
    parser.add_argument("--database", type=Path, default=WORKSPACE / "data" / "cosmos.db")
    parser.add_argument(
        "--output",
        type=Path,
        default=WORKSPACE / "output" / "cleanup-v2" / "manifest.json",
    )
    args = parser.parse_args()
    manifest = build_cleanup_manifest(args.workspace, args.database)
    write_manifest(manifest, args.output)
    print(json.dumps(manifest["summary"], ensure_ascii=False, indent=2))
    print(
        json.dumps(
            {
                key: manifest["dataset_retention"][key]
                for key in (
                    "known_count",
                    "retained_count",
                    "prunable_count",
                    "prunable_child_rows",
                )
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    print(args.output.resolve())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
