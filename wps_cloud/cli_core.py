from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterable


class DownloadError(RuntimeError):
    pass


def unwrap_kdocs_response(response: dict) -> dict:
    if response.get("code") != 0:
        raise DownloadError(response.get("message") or response.get("msg") or "kdocs-cli failed")
    data = response.get("data")
    if isinstance(data, dict) and "code" in data:
        if data.get("code") != 0:
            raise DownloadError(data.get("msg") or data.get("message") or "kdocs API failed")
        return data.get("data") or {}
    return data or {}


def safe_component(name: str) -> str:
    cleaned = []
    for char in name.strip():
        if char in {"/", "\\", ":", "*", "?", '"', "<", ">", "|"} or ord(char) < 32:
            cleaned.append("_")
        else:
            cleaned.append(char)
    value = "".join(cleaned).strip(". ")
    while value.startswith(".."):
        value = value[2:].lstrip("._ ")
    value = value.lstrip("_")
    return value or "untitled"


def file_digest(path: Path, algo: str) -> str:
    digest = hashlib.new(algo)
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def first_supported_hash(hashes: Iterable[dict]) -> tuple[str, str] | None:
    for item in hashes:
        algo = item.get("type")
        expected = item.get("sum")
        if algo in {"sha1", "sha256", "md5"} and expected:
            return algo, expected
    return None


def default_fetcher(url: str, target: Path) -> None:
    request = urllib.request.Request(url, headers={"User-Agent": "wps-cloud/0.1"})
    target.parent.mkdir(parents=True, exist_ok=True)
    try:
        with urllib.request.urlopen(request, timeout=120) as response, target.open("wb") as output:
            shutil.copyfileobj(response, output)
    except urllib.error.HTTPError as exc:
        raise DownloadError(f"download HTTP {exc.code}: {exc.reason}") from exc
    except urllib.error.URLError as exc:
        raise DownloadError(f"download failed: {exc.reason}") from exc


def iter_search_files(page: dict) -> Iterable[dict]:
    for item in page.get("items") or []:
        file_item = item.get("file") if isinstance(item, dict) else None
        if isinstance(file_item, dict):
            file_item = dict(file_item)
            src = item.get("file_src") or {}
            if src.get("path"):
                file_item["_cloud_path"] = src["path"]
            yield file_item


@dataclass
class DownloadResult:
    file_id: str
    name: str
    output_path: Path
    status: str
    bytes: int = 0
    hash_type: str | None = None
    hash_value: str | None = None
    error: str | None = None


class KDocsCliClient:
    def __init__(self, executable: str):
        self.executable = executable

    def run(self, service: str, action: str, payload: dict) -> dict:
        command = [
            self.executable,
            service,
            action,
            json.dumps(payload, ensure_ascii=False),
            "--compact",
        ]
        try:
            proc = subprocess.run(command, text=True, capture_output=True)
        except OSError as exc:
            raise DownloadError(f"kdocs-cli not found or not executable: {self.executable}") from exc
        if proc.returncode != 0:
            detail = (proc.stderr or proc.stdout).strip()
            raise DownloadError(detail or f"kdocs-cli exited with {proc.returncode}")
        try:
            return json.loads(proc.stdout)
        except json.JSONDecodeError as exc:
            raise DownloadError(f"kdocs-cli returned non-JSON output: {exc}") from exc


class WpsCloudDownloader:
    def __init__(
        self,
        client: KDocsCliClient,
        fetcher: Callable[[str, Path], None] = default_fetcher,
        domain: str = "wps365.com",
        page_size: int = 100,
    ):
        self.client = client
        self.fetcher = fetcher
        self.domain = domain
        self.page_size = page_size

    def get_file_info(self, file_id: str) -> dict:
        return unwrap_kdocs_response(
            self.client.run(
                "drive",
                "get-file-info",
                {"file_id": file_id, "with_permission": True, "with_drive": True},
            )
        )

    def list_children(self, drive_id: str, parent_id: str) -> Iterable[dict]:
        items: list[dict] = []
        page_token = ""
        while True:
            payload = {
                "drive_id": drive_id,
                "parent_id": parent_id,
                "page_size": self.page_size,
                "order": "asc",
                "order_by": "fname",
                "with_permission": True,
            }
            if page_token:
                payload["page_token"] = page_token
            page = unwrap_kdocs_response(self.client.run("drive", "list-files", payload))
            items.extend(page.get("items") or [])
            page_token = page.get("next_page_token") or ""
            if not page_token:
                break
        yield from items

    def search_all_files(self, limit: int | None = None) -> Iterable[dict]:
        seen = 0
        page_token = ""
        while True:
            payload = {
                "keyword": "",
                "type": "all",
                "file_type": "file",
                "page_size": self.page_size,
                "with_permission": True,
                "with_drive": True,
                "with_total": True,
                "order": "desc",
                "order_by": "mtime",
            }
            if page_token:
                payload["page_token"] = page_token
            page = unwrap_kdocs_response(self.client.run("drive", "search-files", payload))
            for item in iter_search_files(page):
                yield item
                seen += 1
                if limit is not None and seen >= limit:
                    return
            page_token = page.get("next_page_token") or ""
            if not page_token:
                break

    def download_file(
        self,
        file_id: str,
        drive_id: str | None,
        output_dir: Path,
        *,
        output_name: str | None = None,
        relative_dir: Path | None = None,
        overwrite: bool = False,
        dry_run: bool = False,
        hashes: list[dict] | None = None,
    ) -> DownloadResult:
        payload = {
            "file_id": file_id,
            "with_hash": True,
            "storage_base_domain": self.domain,
        }
        if drive_id:
            payload["drive_id"] = drive_id
        download_info = unwrap_kdocs_response(self.client.run("drive", "download-file", payload))

        info = {} if output_name else self.get_file_info(file_id)
        name = output_name or info.get("name") or file_id
        target_dir = output_dir / (relative_dir or Path())
        target = target_dir / safe_component(name)

        expected_hashes = hashes if hashes is not None else download_info.get("hashes") or []
        supported = first_supported_hash(expected_hashes)
        if target.exists() and supported and file_digest(target, supported[0]) == supported[1].lower():
            return DownloadResult(file_id, name, target, "skipped", target.stat().st_size, *supported)
        if target.exists() and not overwrite:
            return DownloadResult(file_id, name, target, "exists", target.stat().st_size)
        if dry_run:
            return DownloadResult(file_id, name, target, "planned")

        url = download_info.get("url")
        if not url:
            raise DownloadError(f"no download URL returned for {file_id}")

        target.parent.mkdir(parents=True, exist_ok=True)
        self.fetcher(url, target)
        if supported:
            actual = file_digest(target, supported[0])
            if actual.lower() != supported[1].lower():
                try:
                    target.unlink()
                except OSError:
                    pass
                raise DownloadError(f"{supported[0]} mismatch for {name}: expected {supported[1]}, got {actual}")
            return DownloadResult(file_id, name, target, "downloaded", target.stat().st_size, supported[0], actual)
        return DownloadResult(file_id, name, target, "downloaded", target.stat().st_size)

    def download_folder(
        self,
        drive_id: str,
        folder_id: str,
        output_dir: Path,
        *,
        relative_dir: Path | None = None,
        recursive: bool = True,
        overwrite: bool = False,
        dry_run: bool = False,
        continue_on_error: bool = False,
        include_exts: set[str] | None = None,
        limit: int | None = None,
    ) -> list[DownloadResult]:
        base = relative_dir or Path()
        results: list[DownloadResult] = []
        normalized_exts = {ext.lstrip(".").lower() for ext in include_exts or set()}
        children = list(self.list_children(drive_id, folder_id))
        ordered_children = [item for item in children if item.get("type") == "file"] + [
            item for item in children if item.get("type") != "file"
        ]
        for item in ordered_children:
            if limit is not None and len(results) >= limit:
                break
            item_type = item.get("type")
            name = item.get("name") or item.get("id") or "untitled"
            if item_type == "folder":
                if recursive:
                    child_limit = None if limit is None else limit - len(results)
                    results.extend(
                        self.download_folder(
                            drive_id,
                            item["id"],
                            output_dir,
                            relative_dir=base / safe_component(name),
                            recursive=True,
                            overwrite=overwrite,
                            dry_run=dry_run,
                            continue_on_error=continue_on_error,
                            include_exts=normalized_exts,
                            limit=child_limit,
                        )
                    )
                continue
            if item_type != "file":
                continue
            if normalized_exts and Path(name).suffix.lstrip(".").lower() not in normalized_exts:
                continue
            try:
                results.append(
                    self.download_file(
                        item["id"],
                        item.get("drive_id") or drive_id,
                        output_dir,
                        output_name=name,
                        relative_dir=base,
                        overwrite=overwrite,
                        dry_run=dry_run,
                    )
                )
            except DownloadError as exc:
                if not continue_on_error:
                    raise
                results.append(DownloadResult(item.get("id", ""), name, output_dir / base / safe_component(name), "error", error=str(exc)))
        return results
