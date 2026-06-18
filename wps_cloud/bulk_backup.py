from __future__ import annotations

import html
import json
import os
import plistlib
import re
import subprocess
import time
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from urllib.parse import quote

from .cli_core import DownloadError, WpsCloudDownloader, safe_component

RATE_LIMIT_EXIT_CODE = 75
SUCCESS_STATUSES = {"downloaded", "skipped", "exists"}
NON_RETRYABLE_ERROR_PARTS = (
    "不支持的文件类型",
    "permission denied",
    "没有权限",
    "no download URL returned",
)
RATE_LIMIT_ERROR_PARTS = (
    "今日调用次数已达上限",
    "频繁触发调用限制",
)
RATE_LIMIT_RESUME_RE = re.compile(r"(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})")


@dataclass
class BulkBackupResult:
    exit_code: int
    batch_dir: Path
    files_dir: Path
    manifest_path: Path
    progress_path: Path
    events_path: Path
    failures_path: Path
    html_path: Path
    report_path: Path
    total_files: int
    counts: Counter
    bytes_done: int


@dataclass
class ResumeScheduleResult:
    label: str
    plist_path: Path
    wps_resume_at: str
    scheduled_resume_after: str
    loaded: bool


def now_text() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def format_bytes(value: int | float) -> str:
    value = float(value or 0)
    units = ["B", "KiB", "MiB", "GiB", "TiB"]
    for unit in units:
        if abs(value) < 1024 or unit == units[-1]:
            return f"{int(value)} B" if unit == "B" else f"{value:.2f} {unit}"
        value /= 1024
    return f"{value:.2f} TiB"


def atomic_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)


def append_event(path: Path, event: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(event, ensure_ascii=False) + "\n")


def read_existing_status(events_path: Path) -> dict[str, dict]:
    status: dict[str, dict] = {}
    if not events_path.exists():
        return status
    with events_path.open("r", encoding="utf-8") as handle:
        for line in handle:
            if not line.strip():
                continue
            try:
                item = json.loads(line)
            except json.JSONDecodeError:
                continue
            file_id = item.get("file_id")
            if file_id:
                status[file_id] = item
    return status


def unique_name(name: str, file_id: str, taken: set[str]) -> str:
    base = safe_component(name or file_id)
    if base not in taken:
        taken.add(base)
        return base
    suffix = Path(base).suffix
    stem = Path(base).stem or "untitled"
    marker = safe_component(file_id)[:10] or str(len(taken) + 1)
    candidate = f"{stem} ({marker}){suffix}"
    index = 2
    while candidate in taken:
        candidate = f"{stem} ({marker}-{index}){suffix}"
        index += 1
    taken.add(candidate)
    return candidate


def _folder_root_name(downloader: WpsCloudDownloader, folder_id: str) -> str:
    info = downloader.get_file_info(folder_id)
    return safe_component(info.get("name") or folder_id)


def build_plan_from_folder(
    downloader: WpsCloudDownloader,
    drive_id: str,
    folder_id: str,
    *,
    contents_only: bool = False,
    include_exts: set[str] | None = None,
    max_files: int | None = None,
) -> list[dict]:
    normalized_exts = {ext.lstrip(".").lower() for ext in include_exts or set()}
    root = Path()
    if not contents_only and folder_id != "0":
        root = Path(_folder_root_name(downloader, folder_id))

    plan: list[dict] = []
    taken_by_dir: dict[str, set[str]] = defaultdict(set)

    def visit(parent_id: str, relative_dir: Path) -> None:
        if max_files is not None and len(plan) >= max_files:
            return
        children = list(downloader.list_children(drive_id, parent_id))
        ordered = [item for item in children if item.get("type") == "file"] + [
            item for item in children if item.get("type") != "file"
        ]
        for item in ordered:
            if max_files is not None and len(plan) >= max_files:
                return
            item_type = item.get("type")
            name = item.get("name") or item.get("id") or "untitled"
            if item_type == "folder":
                visit(item["id"], relative_dir / safe_component(name))
                continue
            if item_type != "file":
                continue
            ext = Path(name).suffix.lstrip(".").lower()
            if normalized_exts and ext not in normalized_exts:
                continue
            rel_key = relative_dir.as_posix()
            output_name = unique_name(name, item.get("id") or name, taken_by_dir[rel_key])
            target_rel = relative_dir / output_name
            plan.append(
                {
                    "index": len(plan) + 1,
                    "drive_id": item.get("drive_id") or drive_id,
                    "file_id": item["id"],
                    "name": name,
                    "size": int(item.get("size") or 0),
                    "cloud_path": target_rel.as_posix(),
                    "cloud_parent_path": relative_dir.as_posix(),
                    "relative_dir": relative_dir.as_posix(),
                    "output_name": output_name,
                    "target_relative_path": target_rel.as_posix(),
                }
            )

    visit(folder_id, root)
    return plan


def write_manifest(path: Path, *, drive_id: str, folder_id: str, plan: list[dict]) -> None:
    atomic_json(
        path,
        {
            "generated_at": now_text(),
            "source": "kdocs-cli drive list-files recursive",
            "drive_id": drive_id,
            "folder_id": folder_id,
            "total_files": len(plan),
            "total_bytes": sum(item["size"] for item in plan),
            "total_bytes_human": format_bytes(sum(item["size"] for item in plan)),
            "records": plan,
        },
    )


def rate_limit_error(exc: DownloadError) -> bool:
    text = str(exc)
    return any(part in text for part in RATE_LIMIT_ERROR_PARTS)


def retryable_error(exc: DownloadError) -> bool:
    text = str(exc)
    return not any(part in text for part in NON_RETRYABLE_ERROR_PARTS)


def parse_rate_limit_resume_at(text: str) -> str | None:
    match = RATE_LIMIT_RESUME_RE.search(text)
    if not match:
        return None
    try:
        resume_at = datetime.strptime(match.group(1), "%Y-%m-%d %H:%M:%S")
    except ValueError:
        return None
    return resume_at.strftime("%Y-%m-%d %H:%M:%S")


def scheduled_resume_after(resume_at: str | None) -> str | None:
    if not resume_at:
        return None
    try:
        resume_time = datetime.strptime(resume_at, "%Y-%m-%d %H:%M:%S")
    except ValueError:
        return None
    return (resume_time + timedelta(minutes=5)).strftime("%Y-%m-%d %H:%M:%S")


def _parse_timestamp(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.strptime(str(value), "%Y-%m-%d %H:%M:%S")
    except ValueError:
        return None


def schedule_resume_launch_agent(
    batch_dir: Path,
    command: list[str],
    *,
    progress_path: Path | None = None,
    plist_dir: Path | None = None,
    pythonpath: str | None = None,
    load: bool = True,
) -> ResumeScheduleResult:
    progress_path = progress_path or batch_dir / "download_progress.json"
    plist_dir = plist_dir or Path.home() / "Library/LaunchAgents"
    if not progress_path.exists():
        raise DownloadError(f"progress file not found: {progress_path}")

    progress = json.loads(progress_path.read_text(encoding="utf-8"))
    last = progress.get("last_event") or {}
    wps_resume_at = progress.get("rate_limit_resume_at") or last.get("rate_limit_resume_at")
    schedule_after = progress.get("scheduled_resume_after") or last.get("scheduled_resume_after")

    schedule_at = _parse_timestamp(schedule_after)
    resume_at = _parse_timestamp(wps_resume_at)
    if schedule_at is None:
        error = str(last.get("error") or "")
        if not wps_resume_at:
            wps_resume_at = parse_rate_limit_resume_at(error)
            resume_at = _parse_timestamp(wps_resume_at)
        if resume_at is None:
            raise DownloadError("no WPS rate-limit resume timestamp found in progress")
        schedule_at = resume_at + timedelta(minutes=5)
    if resume_at is None:
        resume_at = schedule_at - timedelta(minutes=5)

    if schedule_at.second:
        schedule_at = schedule_at.replace(second=0, microsecond=0) + timedelta(minutes=1)
    else:
        schedule_at = schedule_at.replace(microsecond=0)
    now = datetime.now()
    if schedule_at <= now:
        schedule_at = (now + timedelta(minutes=5)).replace(second=0, microsecond=0)

    stamp = schedule_at.strftime("%Y%m%d%H%M")
    label = f"com.max.wps-cloud-download.resume.{stamp}"
    plist_path = plist_dir / f"{label}.plist"
    env = {
        "WPS_CLOUD_RESUME_LABEL": label,
        "WPS_CLOUD_RESUME_PLIST": str(plist_path),
    }
    if pythonpath:
        env["PYTHONPATH"] = pythonpath

    plist = {
        "Label": label,
        "ProgramArguments": command,
        "WorkingDirectory": str(Path.cwd()),
        "StartCalendarInterval": {
            "Month": schedule_at.month,
            "Day": schedule_at.day,
            "Hour": schedule_at.hour,
            "Minute": schedule_at.minute,
        },
        "StandardOutPath": str(batch_dir / "launchd.out.log"),
        "StandardErrorPath": str(batch_dir / "launchd.err.log"),
        "EnvironmentVariables": env,
    }

    plist_path.parent.mkdir(parents=True, exist_ok=True)
    with plist_path.open("wb") as handle:
        plistlib.dump(plist, handle, sort_keys=False)

    loaded = False
    if load:
        gui = f"gui/{os.getuid()}"
        subprocess.run(["launchctl", "bootout", gui, str(plist_path)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        subprocess.run(["launchctl", "bootstrap", gui, str(plist_path)], check=True)
        subprocess.run(["launchctl", "enable", f"{gui}/{label}"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        loaded = True

    return ResumeScheduleResult(
        label=label,
        plist_path=plist_path,
        wps_resume_at=resume_at.strftime("%Y-%m-%d %H:%M:%S"),
        scheduled_resume_after=schedule_at.strftime("%Y-%m-%d %H:%M:%S"),
        loaded=loaded,
    )


def previous_success_is_present(files_dir: Path, item: dict, previous: dict) -> bool:
    if previous.get("status") not in SUCCESS_STATUSES:
        return False
    target = files_dir / item["target_relative_path"]
    if not target.exists():
        return False
    expected = int(previous.get("bytes") or 0)
    return expected <= 0 or target.stat().st_size == expected


def previous_nonretryable_error(previous: dict) -> bool:
    error = previous.get("error") or ""
    if any(part in error for part in RATE_LIMIT_ERROR_PARTS):
        return False
    return any(part in error for part in NON_RETRYABLE_ERROR_PARTS)


def write_progress(
    path: Path,
    *,
    started_at: str,
    total: int,
    processed: int,
    counts: Counter,
    bytes_done: int,
    last_event: dict | None,
    errors: list[dict],
    started_mono: float,
) -> None:
    elapsed = max(time.time() - started_mono, 0.001)
    payload = {
        "started_at": started_at,
        "updated_at": now_text(),
        "total_files": total,
        "processed_files": processed,
        "remaining_files": max(total - processed, 0),
        "counts": dict(counts),
        "successful_files": sum(counts.get(status, 0) for status in SUCCESS_STATUSES),
        "failed_files": counts.get("error", 0),
        "bytes_done": bytes_done,
        "bytes_done_human": format_bytes(bytes_done),
        "average_files_per_minute": round(processed / elapsed * 60, 2),
        "last_event": last_event,
        "recent_errors": errors[-20:],
    }
    if last_event:
        for key in ("rate_limit_resume_at", "scheduled_resume_after"):
            if last_event.get(key):
                payload[key] = last_event[key]
    atomic_json(path, payload)


def render_html_tree(plan: list[dict], status_by_file: dict[str, dict], files_root_name: str) -> str:
    root = {"dirs": {}, "files": []}
    for item in plan:
        parts = [part for part in item["target_relative_path"].split("/") if part]
        node = root
        for part in parts[:-1]:
            node = node["dirs"].setdefault(part, {"dirs": {}, "files": []})
        node["files"].append(item)

    def count_files(node: dict) -> int:
        return len(node["files"]) + sum(count_files(child) for child in node["dirs"].values())

    def render_node(node: dict) -> str:
        chunks: list[str] = ["<ul>"]
        for name, child in sorted(node["dirs"].items(), key=lambda pair: pair[0].lower()):
            chunks.append(
                "<li class=\"folder\"><details><summary>"
                f"{html.escape(name)} <span>{count_files(child)} files</span>"
                "</summary>"
            )
            chunks.append(render_node(child))
            chunks.append("</details></li>")
        for item in sorted(node["files"], key=lambda value: value["output_name"].lower()):
            status = (status_by_file.get(item["file_id"]) or {}).get("status", "planned")
            href = quote(f"{files_root_name}/{item['target_relative_path']}", safe="/()[] @._-")
            label = html.escape(item["output_name"])
            size = html.escape(format_bytes(item["size"]))
            cloud = html.escape(item.get("cloud_path") or "")
            link = f"<a href=\"{href}\">{label}</a>" if status in SUCCESS_STATUSES else f"<span>{label}</span>"
            chunks.append(
                f"<li class=\"file {html.escape(status)}\">{link}"
                f" <small>{size} · {html.escape(status)} · {cloud}</small></li>"
            )
        chunks.append("</ul>")
        return "".join(chunks)

    total_bytes = sum(item["size"] for item in plan)
    return (
        "<!doctype html><html lang=\"zh-CN\"><head><meta charset=\"utf-8\">"
        "<title>WPS云盘目录文件列表</title>"
        "<style>"
        "body{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;"
        "margin:24px;line-height:1.45;color:#1f2328;background:#fff}"
        "h1{font-size:24px;margin:0 0 8px}p{margin:4px 0 16px;color:#57606a}"
        "ul{list-style:none;margin:0 0 0 18px;padding:0}.folder{margin:3px 0}"
        "summary{cursor:pointer;font-weight:600}.folder span{color:#6e7781;font-weight:400}"
        ".file{margin:2px 0 2px 18px}.file small{color:#6e7781}"
        "a{color:#0969da;text-decoration:none}a:hover{text-decoration:underline}"
        ".error span{color:#b42318}.planned span{color:#6e7781}"
        "</style></head><body>"
        "<h1>WPS云盘目录文件列表</h1>"
        f"<p>生成时间：{html.escape(now_text())}；文件数：{len(plan)}；云端标称大小：{html.escape(format_bytes(total_bytes))}。</p>"
        "<p>链接指向本批次下载目录中的本地文件；失败或未处理文件不生成可点击链接。</p>"
        f"{render_node(root)}"
        "</body></html>"
    )


def write_report(
    path: Path,
    *,
    batch_dir: Path,
    files_dir: Path,
    manifest_path: Path,
    html_path: Path,
    plan: list[dict],
    counts: Counter,
    bytes_done: int,
    errors: list[dict],
    started_at: str,
) -> None:
    total_bytes = sum(item["size"] for item in plan)
    lines = [
        "# WPS云盘全量下载报告",
        "",
        f"- 开始时间：{started_at}",
        f"- 更新时间：{now_text()}",
        f"- 批次目录：`{batch_dir}`",
        f"- 文件保存目录：`{files_dir}`",
        f"- 清单文件：`{manifest_path}`",
        f"- HTML 目录：`{html_path}`",
        f"- 云端文件数：{len(plan)}",
        f"- 云端标称大小：{format_bytes(total_bytes)}",
        f"- 已完成文件数：{sum(counts.get(status, 0) for status in SUCCESS_STATUSES)}",
        f"- 下载文件数：{counts.get('downloaded', 0)}",
        f"- 已存在且通过校验/保留文件数：{counts.get('skipped', 0) + counts.get('exists', 0)}",
        f"- 失败文件数：{counts.get('error', 0)}",
        f"- 已处理本地字节数：{format_bytes(bytes_done)}",
        "",
        "## 口径说明",
        "",
        "- 本次清单来自 WPS/KDocs `list-files` 递归枚举，不使用全局搜索分页作为全量依据。",
        "- HTML 树按本批次目标路径生成，覆盖所有枚举到的文件。",
        "- 遇到 WPS 限流会暂停并记录恢复时间，不会把剩余文件误记为失败。",
    ]
    if errors:
        lines.extend(["", "## 最近失败样例", ""])
        for item in errors[-20:]:
            lines.append(f"- `{item.get('cloud_path') or item.get('name')}`：{item.get('error')}")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _paths(batch_dir: Path) -> dict[str, Path]:
    return {
        "files_dir": batch_dir / "files",
        "manifest_path": batch_dir / "wps_cloud_full_manifest.json",
        "progress_path": batch_dir / "download_progress.json",
        "events_path": batch_dir / "download_events.jsonl",
        "failures_path": batch_dir / "download_failures.json",
        "html_path": batch_dir / "WPS云盘目录文件列表.html",
        "report_path": batch_dir / "WPS云盘全量下载报告.md",
    }


def run_backup_folder(
    downloader: WpsCloudDownloader,
    drive_id: str,
    folder_id: str,
    batch_dir: Path,
    *,
    contents_only: bool = False,
    include_exts: set[str] | None = None,
    max_files: int | None = None,
    dry_run: bool = False,
    progress_every: int = 25,
    retries: int = 3,
    resume_events: bool = True,
) -> BulkBackupResult:
    batch_dir.mkdir(parents=True, exist_ok=True)
    paths = _paths(batch_dir)
    paths["files_dir"].mkdir(parents=True, exist_ok=True)

    started_at = now_text()
    started_mono = time.time()
    plan = build_plan_from_folder(
        downloader,
        drive_id,
        folder_id,
        contents_only=contents_only,
        include_exts=include_exts,
        max_files=max_files,
    )
    write_manifest(paths["manifest_path"], drive_id=drive_id, folder_id=folder_id, plan=plan)

    counts: Counter[str] = Counter()
    bytes_done = 0
    errors: list[dict] = []
    last_event: dict | None = None
    previous_by_file = read_existing_status(paths["events_path"]) if resume_events else {}
    exit_code = 0
    processed = 0

    try:
        for processed, item in enumerate(plan, start=1):
            previous = previous_by_file.get(item["file_id"]) or {}
            if previous_success_is_present(paths["files_dir"], item, previous):
                event = {
                    "time": now_text(),
                    "index": processed,
                    "file_id": item["file_id"],
                    "name": item["name"],
                    "cloud_path": item.get("cloud_path"),
                    "target_relative_path": item["target_relative_path"],
                    "status": "skipped",
                    "bytes": int(previous.get("bytes") or 0),
                    "resume_from_event": True,
                }
            elif previous_nonretryable_error(previous):
                event = {
                    "time": now_text(),
                    "index": processed,
                    "file_id": item["file_id"],
                    "name": item["name"],
                    "cloud_path": item.get("cloud_path"),
                    "target_relative_path": item["target_relative_path"],
                    "status": "error",
                    "bytes": 0,
                    "error": previous.get("error"),
                    "resume_from_event": True,
                }
            elif dry_run:
                event = {
                    "time": now_text(),
                    "index": processed,
                    "file_id": item["file_id"],
                    "name": item["name"],
                    "cloud_path": item.get("cloud_path"),
                    "target_relative_path": item["target_relative_path"],
                    "status": "planned",
                    "bytes": 0,
                }
            else:
                event = _download_with_retries(
                    downloader,
                    paths["files_dir"],
                    item,
                    processed,
                    retries=retries,
                )
                if event["status"] == "paused":
                    append_event(paths["events_path"], event)
                    last_event = event
                    counts[event["status"]] += 1
                    write_progress(
                        paths["progress_path"],
                        started_at=started_at,
                        total=len(plan),
                        processed=processed,
                        counts=counts,
                        bytes_done=bytes_done,
                        last_event=last_event,
                        errors=errors,
                        started_mono=started_mono,
                    )
                    exit_code = RATE_LIMIT_EXIT_CODE
                    break

            append_event(paths["events_path"], event)
            last_event = event
            counts[event["status"]] += 1
            if event["status"] in SUCCESS_STATUSES:
                bytes_done += int(event.get("bytes") or 0)
            if event["status"] == "error":
                errors.append(event)

            if processed % max(progress_every, 1) == 0 or processed == len(plan):
                write_progress(
                    paths["progress_path"],
                    started_at=started_at,
                    total=len(plan),
                    processed=processed,
                    counts=counts,
                    bytes_done=bytes_done,
                    last_event=last_event,
                    errors=errors,
                    started_mono=started_mono,
                )
    finally:
        atomic_json(paths["failures_path"], {"updated_at": now_text(), "errors": errors})
        status_by_file = read_existing_status(paths["events_path"])
        paths["html_path"].write_text(
            render_html_tree(plan, status_by_file, paths["files_dir"].name),
            encoding="utf-8",
        )
        write_report(
            paths["report_path"],
            batch_dir=batch_dir,
            files_dir=paths["files_dir"],
            manifest_path=paths["manifest_path"],
            html_path=paths["html_path"],
            plan=plan,
            counts=counts,
            bytes_done=bytes_done,
            errors=errors,
            started_at=started_at,
        )
        if not paths["progress_path"].exists():
            write_progress(
                paths["progress_path"],
                started_at=started_at,
                total=len(plan),
                processed=processed,
                counts=counts,
                bytes_done=bytes_done,
                last_event=last_event,
                errors=errors,
                started_mono=started_mono,
            )

    if exit_code == 0 and counts.get("error", 0):
        exit_code = 1
    return BulkBackupResult(
        exit_code=exit_code,
        batch_dir=batch_dir,
        files_dir=paths["files_dir"],
        manifest_path=paths["manifest_path"],
        progress_path=paths["progress_path"],
        events_path=paths["events_path"],
        failures_path=paths["failures_path"],
        html_path=paths["html_path"],
        report_path=paths["report_path"],
        total_files=len(plan),
        counts=counts,
        bytes_done=bytes_done,
    )


def _download_with_retries(
    downloader: WpsCloudDownloader,
    files_dir: Path,
    item: dict,
    index: int,
    *,
    retries: int,
) -> dict:
    for attempt in range(retries + 1):
        try:
            result = downloader.download_file(
                item["file_id"],
                item.get("drive_id"),
                files_dir,
                output_name=item["output_name"],
                relative_dir=Path(item["relative_dir"]),
                overwrite=True,
                dry_run=False,
            )
            return {
                "time": now_text(),
                "index": index,
                "file_id": item["file_id"],
                "name": item["name"],
                "cloud_path": item.get("cloud_path"),
                "target_relative_path": item["target_relative_path"],
                "status": result.status,
                "bytes": int(result.bytes or 0),
                "hash_type": result.hash_type,
                "hash_value": result.hash_value,
                "attempt": attempt + 1,
            }
        except DownloadError as exc:
            if rate_limit_error(exc):
                resume_at = parse_rate_limit_resume_at(str(exc))
                resume_after = scheduled_resume_after(resume_at)
                event = {
                    "time": now_text(),
                    "index": index,
                    "file_id": item["file_id"],
                    "name": item["name"],
                    "cloud_path": item.get("cloud_path"),
                    "target_relative_path": item["target_relative_path"],
                    "status": "paused",
                    "bytes": 0,
                    "error": str(exc),
                    "attempt": attempt + 1,
                }
                if resume_at:
                    event["rate_limit_resume_at"] = resume_at
                if resume_after:
                    event["scheduled_resume_after"] = resume_after
                return event
            if attempt >= retries or not retryable_error(exc):
                return {
                    "time": now_text(),
                    "index": index,
                    "file_id": item["file_id"],
                    "name": item["name"],
                    "cloud_path": item.get("cloud_path"),
                    "target_relative_path": item["target_relative_path"],
                    "status": "error",
                    "bytes": 0,
                    "error": str(exc),
                    "attempt": attempt + 1,
                }
            time.sleep(min(60, 2**attempt * 3))
    raise AssertionError("unreachable")
