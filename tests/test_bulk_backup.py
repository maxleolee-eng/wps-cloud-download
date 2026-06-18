import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from wps_cloud.bulk_backup import (
    RATE_LIMIT_EXIT_CODE,
    build_plan_from_folder,
    run_backup_folder,
    schedule_resume_launch_agent,
)
from wps_cloud.cli_core import DownloadError, WpsCloudDownloader


class FakeClient:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def run(self, service, action, payload):
        self.calls.append((service, action, dict(payload)))
        if not self.responses:
            raise AssertionError(f"unexpected call: {service} {action} {payload}")
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


def page(items):
    return {"code": 0, "data": {"code": 0, "data": {"items": items, "next_page_token": ""}}}


def download_response():
    return {"code": 0, "data": {"code": 0, "data": {"url": "https://example.invalid/f"}}}


class BulkBackupTests(unittest.TestCase):
    def test_build_plan_uses_recursive_list_files_and_preserves_paths(self):
        client = FakeClient(
            [
                page(
                    [
                        {"id": "sub", "name": "子目录", "type": "folder", "drive_id": "d1"},
                        {"id": "f1", "name": "根.docx", "type": "file", "drive_id": "d1", "size": 3},
                    ]
                ),
                page(
                    [
                        {"id": "f2", "name": "深.pdf", "type": "file", "drive_id": "d1", "size": 4},
                    ]
                ),
            ]
        )
        downloader = WpsCloudDownloader(client)

        plan = build_plan_from_folder(downloader, "d1", "root", contents_only=True)

        self.assertEqual([item["target_relative_path"] for item in plan], ["根.docx", "子目录/深.pdf"])
        self.assertEqual([call[1] for call in client.calls], ["list-files", "list-files"])

    def test_backup_writes_manifest_progress_events_report_and_html(self):
        client = FakeClient(
            [
                page([{"id": "f1", "name": "文档.docx", "type": "file", "drive_id": "d1", "size": 5}]),
                download_response(),
            ]
        )

        with TemporaryDirectory() as tmp:
            batch_dir = Path(tmp)
            downloader = WpsCloudDownloader(client, fetcher=lambda url, target: target.write_bytes(b"hello"))
            result = run_backup_folder(downloader, "d1", "root", batch_dir, contents_only=True)

            self.assertEqual(result.exit_code, 0)
            self.assertTrue((batch_dir / "files/文档.docx").exists())
            self.assertTrue((batch_dir / "wps_cloud_full_manifest.json").exists())
            self.assertTrue((batch_dir / "download_events.jsonl").exists())
            self.assertTrue((batch_dir / "WPS云盘全量下载报告.md").exists())
            self.assertTrue((batch_dir / "WPS云盘目录文件列表.html").exists())
            progress = json.loads((batch_dir / "download_progress.json").read_text(encoding="utf-8"))

        self.assertEqual(progress["processed_files"], 1)
        self.assertEqual(progress["counts"]["downloaded"], 1)

    def test_rate_limit_pauses_without_marking_remaining_files_failed(self):
        client = FakeClient(
            [
                page(
                    [
                        {"id": "f1", "name": "a.docx", "type": "file", "drive_id": "d1", "size": 1},
                        {"id": "f2", "name": "b.docx", "type": "file", "drive_id": "d1", "size": 1},
                    ]
                ),
                DownloadError("今日调用次数已达上限，将于 2026-06-19 08:00:00 恢复"),
            ]
        )

        with TemporaryDirectory() as tmp:
            downloader = WpsCloudDownloader(client, fetcher=lambda url, target: target.write_bytes(b"x"))
            result = run_backup_folder(downloader, "d1", "root", Path(tmp), contents_only=True)
            progress = json.loads((Path(tmp) / "download_progress.json").read_text(encoding="utf-8"))
            events = (Path(tmp) / "download_events.jsonl").read_text(encoding="utf-8").strip().splitlines()

        self.assertEqual(result.exit_code, RATE_LIMIT_EXIT_CODE)
        self.assertEqual(progress["counts"]["paused"], 1)
        self.assertEqual(progress.get("failed_files"), 0)
        self.assertEqual(progress["remaining_files"], 1)
        self.assertEqual(progress["rate_limit_resume_at"], "2026-06-19 08:00:00")
        self.assertEqual(progress["scheduled_resume_after"], "2026-06-19 08:05:00")
        self.assertEqual(len(events), 1)

    def test_resume_skips_previous_success_when_local_file_still_exists(self):
        client = FakeClient([page([{"id": "f1", "name": "文档.docx", "type": "file", "drive_id": "d1", "size": 5}])])

        with TemporaryDirectory() as tmp:
            batch_dir = Path(tmp)
            (batch_dir / "files").mkdir()
            (batch_dir / "files/文档.docx").write_bytes(b"hello")
            (batch_dir / "download_events.jsonl").write_text(
                json.dumps(
                    {
                        "file_id": "f1",
                        "status": "downloaded",
                        "bytes": 5,
                        "target_relative_path": "文档.docx",
                    },
                    ensure_ascii=False,
                )
                + "\n",
                encoding="utf-8",
            )

            downloader = WpsCloudDownloader(client, fetcher=lambda url, target: target.write_bytes(b"new"))
            result = run_backup_folder(downloader, "d1", "root", batch_dir, contents_only=True)
            events = [
                json.loads(line)
                for line in (batch_dir / "download_events.jsonl").read_text(encoding="utf-8").splitlines()
            ]

        self.assertEqual(result.exit_code, 0)
        self.assertEqual(events[-1]["status"], "skipped")
        self.assertEqual(events[-1]["resume_from_event"], True)
        self.assertEqual([call[1] for call in client.calls], ["list-files"])

    def test_backup_overwrites_stale_local_file_without_success_event(self):
        client = FakeClient(
            [
                page([{"id": "f1", "name": "文档.docx", "type": "file", "drive_id": "d1", "size": 3}]),
                download_response(),
            ]
        )

        with TemporaryDirectory() as tmp:
            batch_dir = Path(tmp)
            (batch_dir / "files").mkdir()
            (batch_dir / "files/文档.docx").write_bytes(b"old")

            downloader = WpsCloudDownloader(client, fetcher=lambda url, target: target.write_bytes(b"new"))
            result = run_backup_folder(downloader, "d1", "root", batch_dir, contents_only=True)
            events = [
                json.loads(line)
                for line in (batch_dir / "download_events.jsonl").read_text(encoding="utf-8").splitlines()
            ]
            saved_body = (batch_dir / "files/文档.docx").read_bytes()

        self.assertEqual(result.exit_code, 0)
        self.assertEqual(events[-1]["status"], "downloaded")
        self.assertEqual(saved_body, b"new")

    def test_non_retryable_error_is_recorded_once(self):
        client = FakeClient(
            [
                page([{"id": "f1", "name": "笔记.wpsnote", "type": "file", "drive_id": "d1", "size": 0}]),
                DownloadError("不支持的文件类型"),
            ]
        )

        with TemporaryDirectory() as tmp:
            downloader = WpsCloudDownloader(client, fetcher=lambda url, target: target.write_bytes(b""))
            result = run_backup_folder(downloader, "d1", "root", Path(tmp), contents_only=True, retries=3)
            failures = json.loads((Path(tmp) / "download_failures.json").read_text(encoding="utf-8"))

        self.assertEqual(result.exit_code, 1)
        self.assertEqual(len(failures["errors"]), 1)
        self.assertEqual(failures["errors"][0]["error"], "不支持的文件类型")
        self.assertEqual([call[1] for call in client.calls], ["list-files", "download-file"])

    def test_schedule_resume_launch_agent_writes_plist_without_loading(self):
        with TemporaryDirectory() as tmp:
            batch_dir = Path(tmp)
            (batch_dir / "download_progress.json").write_text(
                json.dumps(
                    {
                        "rate_limit_resume_at": "2026-06-19 08:00:00",
                        "scheduled_resume_after": "2026-06-19 08:05:00",
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )

            result = schedule_resume_launch_agent(
                batch_dir,
                ["/usr/bin/python3", "-m", "wps_cloud.cli", "status"],
                plist_dir=batch_dir / "LaunchAgents",
                load=False,
            )

        self.assertEqual(result.wps_resume_at, "2026-06-19 08:00:00")
        self.assertEqual(result.scheduled_resume_after, "2026-06-19 08:05:00")
        self.assertTrue(result.plist_path.name.startswith("com.max.wps-cloud-download.resume."))
        self.assertFalse(result.loaded)


if __name__ == "__main__":
    unittest.main()
