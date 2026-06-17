#!/usr/bin/env python3
from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from wps_cloud.cli import main as wps_cloud_main


def main() -> int:
    parser = argparse.ArgumentParser(description="Compatibility wrapper around wps-cloud download-file.")
    parser.add_argument("--file-id", required=True)
    parser.add_argument("--drive-id")
    parser.add_argument("--output", required=True)
    parser.add_argument("--domain", default="wps365.com")
    parser.add_argument("--kdocs-cli")
    parser.add_argument("--overwrite", action="store_true")
    args = parser.parse_args()

    argv = []
    if args.kdocs_cli:
        argv.extend(["--kdocs-cli", args.kdocs_cli])
    argv.extend(["--domain", args.domain, "download-file", "--file-id", args.file_id])
    if args.drive_id:
        argv.extend(["--drive-id", args.drive_id])
    output = Path(args.output)
    argv.extend(["--output-dir", str(output.parent), "--name", output.name])
    if args.overwrite:
        argv.append("--overwrite")
    return wps_cloud_main(argv)


if __name__ == "__main__":
    raise SystemExit(main())
