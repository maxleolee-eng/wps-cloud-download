from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
from pathlib import Path

from .cli_core import DownloadError, KDocsCliClient, WpsCloudDownloader, safe_component
from .kdocs_installer import KDOCS_CLI_VERSION, install_kdocs_cli


def find_kdocs_cli() -> str:
    found = shutil.which("kdocs-cli")
    if found:
        return found
    fallback = Path.home() / ".local/bin/kdocs-cli"
    return str(fallback)


def print_result(result) -> None:
    suffix = ""
    if result.hash_type and result.hash_value:
        suffix = f" {result.hash_type}={result.hash_value}"
    if result.error:
        suffix = f" error={result.error}"
    print(f"{result.status}\t{result.bytes}\t{result.output_path}{suffix}")


def build_downloader(args) -> WpsCloudDownloader:
    return WpsCloudDownloader(
        KDocsCliClient(args.kdocs_cli),
        domain=args.domain,
        page_size=args.page_size,
    )


def cmd_status(args) -> int:
    try:
        proc = subprocess.run(
            [args.kdocs_cli, "auth", "status", "--output", "json"],
            text=True,
            capture_output=True,
        )
    except OSError:
        print(f"error: kdocs-cli not found or not executable: {args.kdocs_cli}", file=sys.stderr)
        print("hint: run `wps-cloud-download install-kdocs` first", file=sys.stderr)
        return 1
    if proc.returncode != 0:
        print((proc.stderr or proc.stdout).strip(), file=sys.stderr)
        return proc.returncode
    data = json.loads(proc.stdout)
    print(json.dumps({"authenticated": data.get("authenticated")}, ensure_ascii=False))
    return 0 if data.get("authenticated") else 1


def cmd_install_kdocs(args) -> int:
    target = install_kdocs_cli(
        version=args.version,
        install_dir=args.install_dir,
        force=args.force,
    )
    print(f"installed\t{target}")
    return 0


def cmd_search(args) -> int:
    client = KDocsCliClient(args.kdocs_cli)
    payload = {
        "keyword": args.keyword or "",
        "type": "all",
        "page_size": args.page_size,
        "with_permission": True,
        "with_drive": True,
        "with_total": True,
    }
    if args.file_type:
        payload["file_type"] = args.file_type
    if args.ext:
        payload["file_exts"] = [ext.lstrip(".").lower() for ext in args.ext]
    data = client.run("drive", "search-files", payload)
    print(json.dumps(data, ensure_ascii=False, indent=2))
    return 0


def cmd_download_file(args) -> int:
    downloader = build_downloader(args)
    result = downloader.download_file(
        args.file_id,
        args.drive_id,
        args.output_dir,
        output_name=args.name,
        overwrite=args.overwrite,
        dry_run=args.dry_run,
    )
    print_result(result)
    return 0


def cmd_download_folder(args) -> int:
    downloader = build_downloader(args)
    base = Path()
    if not args.contents_only and args.folder_id != "0":
        info = downloader.get_file_info(args.folder_id)
        base = Path(safe_component(info.get("name") or args.folder_id))
    results = downloader.download_folder(
        args.drive_id,
        args.folder_id,
        args.output_dir,
        relative_dir=base,
        recursive=not args.no_recursive,
        overwrite=args.overwrite,
        dry_run=args.dry_run,
        continue_on_error=args.continue_on_error,
        include_exts=set(args.ext or []),
        limit=args.limit,
    )
    for result in results:
        print_result(result)
    return 1 if any(result.status == "error" for result in results) else 0


def cloud_relative_path(item: dict) -> Path:
    raw = item.get("_cloud_path") or item.get("path") or ""
    parts = [safe_component(part) for part in raw.split("/") if part and part != "我的云文档"]
    return Path(*parts) if parts else Path()


def cmd_download_all(args) -> int:
    downloader = build_downloader(args)
    count = 0
    errors = 0
    for item in downloader.search_all_files():
        if args.ext:
            ext = Path(item.get("name") or "").suffix.lstrip(".").lower()
            if ext not in {value.lstrip(".").lower() for value in args.ext}:
                continue
        if args.limit is not None and count >= args.limit:
            break
        try:
            result = downloader.download_file(
                item["id"],
                item.get("drive_id"),
                args.output_dir,
                output_name=item.get("name"),
                relative_dir=cloud_relative_path(item) if args.preserve_paths else Path(),
                overwrite=args.overwrite,
                dry_run=args.dry_run,
            )
            print_result(result)
            count += 1
        except DownloadError as exc:
            errors += 1
            print(f"error\t0\t{item.get('name') or item.get('id')}\terror={exc}", file=sys.stderr)
            if not args.continue_on_error:
                return 1
    print(f"summary\tfiles={count}\terrors={errors}", file=sys.stderr)
    return 1 if errors else 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="wps-cloud-download",
        description="wps-cloud-download CL V1.0: download WPS/KDocs cloud files through kdocs-cli.",
    )
    parser.add_argument("--kdocs-cli", default=find_kdocs_cli())
    parser.add_argument("--domain", default="wps365.com", choices=["wps365.com", "kdocs.cn", "wps.cn"])
    parser.add_argument("--page-size", type=int, default=100)
    sub = parser.add_subparsers(dest="command", required=True)

    status = sub.add_parser("status", help="Check kdocs-cli login status.")
    status.set_defaults(func=cmd_status)

    install = sub.add_parser("install-kdocs", help="Install kdocs-cli from the official WPS CDN.")
    install.add_argument("--version", default=KDOCS_CLI_VERSION)
    install.add_argument("--install-dir", type=Path)
    install.add_argument("--force", action="store_true")
    install.set_defaults(func=cmd_install_kdocs)

    search = sub.add_parser("search", help="Search cloud files.")
    search.add_argument("keyword", nargs="?")
    search.add_argument("--file-type", choices=["file", "folder"])
    search.add_argument("--ext", action="append")
    search.set_defaults(func=cmd_search)

    one = sub.add_parser("download-file", help="Download one file by file id.")
    one.add_argument("--file-id", required=True)
    one.add_argument("--drive-id")
    one.add_argument("--output-dir", type=Path, default=Path.cwd())
    one.add_argument("--name")
    one.add_argument("--overwrite", action="store_true")
    one.add_argument("--dry-run", action="store_true")
    one.set_defaults(func=cmd_download_file)

    folder = sub.add_parser("download-folder", help="Download a folder recursively.")
    folder.add_argument("--drive-id", required=True)
    folder.add_argument("--folder-id", required=True)
    folder.add_argument("--output-dir", type=Path, required=True)
    folder.add_argument("--contents-only", action="store_true")
    folder.add_argument("--no-recursive", action="store_true")
    folder.add_argument("--overwrite", action="store_true")
    folder.add_argument("--dry-run", action="store_true")
    folder.add_argument("--continue-on-error", action="store_true")
    folder.add_argument("--ext", action="append")
    folder.add_argument("--limit", type=int)
    folder.set_defaults(func=cmd_download_folder)

    all_files = sub.add_parser("download-all", help="Download files returned by global search.")
    all_files.add_argument("--output-dir", type=Path, required=True)
    all_files.add_argument("--limit", type=int)
    all_files.add_argument("--ext", action="append")
    all_files.add_argument("--preserve-paths", action=argparse.BooleanOptionalAction, default=True)
    all_files.add_argument("--overwrite", action="store_true")
    all_files.add_argument("--dry-run", action="store_true")
    all_files.add_argument("--continue-on-error", action="store_true")
    all_files.set_defaults(func=cmd_download_all)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except DownloadError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
