import hashlib
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from wps_cloud.cli_core import (
    DownloadError,
    KDocsCliClient,
    WpsCloudDownloader,
    iter_search_files,
    safe_component,
    unwrap_kdocs_response,
)


class FakeClient:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def run(self, service, action, payload):
        self.calls.append((service, action, dict(payload)))
        if not self.responses:
            raise AssertionError(f"unexpected call: {service} {action} {payload}")
        return self.responses.pop(0)


class WpsCloudTests(unittest.TestCase):
    def test_unwraps_nested_kdocs_response(self):
        data = unwrap_kdocs_response(
            {"code": 0, "data": {"code": 0, "msg": "ok", "data": {"url": "x"}}}
        )
        self.assertEqual(data, {"url": "x"})

    def test_rejects_failed_nested_response(self):
        with self.assertRaisesRegex(DownloadError, "denied"):
            unwrap_kdocs_response({"code": 0, "data": {"code": 403, "msg": "denied"}})

    def test_safe_component_blocks_path_traversal(self):
        self.assertEqual(safe_component("../a/b:c*?.docx"), "a_b_c__.docx")
        self.assertEqual(safe_component("   "), "untitled")

    def test_download_file_uses_wps365_and_verifies_hash(self):
        body = b"hello wps"
        sha1 = hashlib.sha1(body).hexdigest()
        client = FakeClient(
            [
                {"code": 0, "data": {"code": 0, "data": {"url": "https://example.invalid/f"}}},
                {
                    "code": 0,
                    "data": {
                        "code": 0,
                        "data": {
                            "name": "测试.docx",
                            "drive_id": "d1",
                            "id": "f1",
                            "size": len(body),
                            "type": "file",
                        },
                    },
                },
            ]
        )

        with TemporaryDirectory() as tmp:
            downloader = WpsCloudDownloader(client, fetcher=lambda url, target: target.write_bytes(body))
            result = downloader.download_file("f1", "d1", Path(tmp), hashes=[{"type": "sha1", "sum": sha1}])

        self.assertEqual(result.status, "downloaded")
        self.assertEqual(client.calls[0][2]["storage_base_domain"], "wps365.com")
        self.assertTrue(result.output_path.name.endswith(".docx"))

    def test_download_file_detects_hash_mismatch(self):
        client = FakeClient(
            [
                {"code": 0, "data": {"code": 0, "data": {"url": "https://example.invalid/f"}}},
                {
                    "code": 0,
                    "data": {
                        "code": 0,
                        "data": {"name": "bad.docx", "drive_id": "d1", "id": "f1", "type": "file"},
                    },
                },
            ]
        )
        with TemporaryDirectory() as tmp:
            downloader = WpsCloudDownloader(client, fetcher=lambda url, target: target.write_bytes(b"bad"))
            with self.assertRaisesRegex(DownloadError, "sha1 mismatch"):
                downloader.download_file(
                    "f1",
                    "d1",
                    Path(tmp),
                    hashes=[{"type": "sha1", "sum": "0" * 40}],
                )

    def test_lists_folder_pages_and_downloads_recursively(self):
        body = b"docx bytes"
        sha1 = hashlib.sha1(body).hexdigest()
        client = FakeClient(
            [
                {
                    "code": 0,
                    "data": {
                        "code": 0,
                        "data": {
                            "items": [
                                {"id": "sub", "name": "子目录", "type": "folder", "drive_id": "d1"},
                                {"id": "f1", "name": "根.docx", "type": "file", "drive_id": "d1"},
                            ],
                            "next_page_token": "next",
                        },
                    },
                },
                {
                    "code": 0,
                    "data": {"code": 0, "data": {"items": [], "next_page_token": ""}},
                },
                {
                    "code": 0,
                    "data": {"code": 0, "data": {"url": "https://example.invalid/f1", "hashes": [{"type": "sha1", "sum": sha1}]}},
                },
                {
                    "code": 0,
                    "data": {
                        "code": 0,
                        "data": {
                            "items": [{"id": "f2", "name": "深.docx", "type": "file", "drive_id": "d1"}],
                            "next_page_token": "",
                        },
                    },
                },
                {"code": 0, "data": {"code": 0, "data": {"url": "https://example.invalid/f2", "hashes": [{"type": "sha1", "sum": sha1}]}}},
            ]
        )
        with TemporaryDirectory() as tmp:
            downloader = WpsCloudDownloader(client, fetcher=lambda url, target: target.write_bytes(body))
            results = downloader.download_folder("d1", "root", Path(tmp))
            paths = [r.output_path.relative_to(tmp).as_posix() for r in results]

        self.assertEqual(paths, ["根.docx", "子目录/深.docx"])
        self.assertEqual(client.calls[1][2]["page_token"], "next")

    def test_iter_search_files_extracts_file_objects(self):
        page = {
            "items": [
                {"file": {"id": "f1", "name": "a.pdf", "type": "file"}},
                {"file": {"id": "folder", "name": "dir", "type": "folder"}},
            ],
            "next_page_token": "",
        }
        self.assertEqual([item["id"] for item in iter_search_files(page)], ["f1", "folder"])

    def test_client_reports_missing_executable_without_traceback(self):
        client = KDocsCliClient("/no/such/kdocs-cli")
        with self.assertRaisesRegex(DownloadError, "not found"):
            client.run("drive", "search-files", {})

    def test_folder_download_can_filter_extension_and_limit(self):
        body = b"docx bytes"
        sha1 = hashlib.sha1(body).hexdigest()
        client = FakeClient(
            [
                {
                    "code": 0,
                    "data": {
                        "code": 0,
                        "data": {
                            "items": [
                                {"id": "f1", "name": "a.pdf", "type": "file", "drive_id": "d1"},
                                {"id": "f2", "name": "b.docx", "type": "file", "drive_id": "d1"},
                                {"id": "f3", "name": "c.docx", "type": "file", "drive_id": "d1"},
                            ],
                            "next_page_token": "",
                        },
                    },
                },
                {"code": 0, "data": {"code": 0, "data": {"url": "https://example.invalid/f2", "hashes": [{"type": "sha1", "sum": sha1}]}}},
            ]
        )
        with TemporaryDirectory() as tmp:
            downloader = WpsCloudDownloader(client, fetcher=lambda url, target: target.write_bytes(body))
            results = downloader.download_folder("d1", "root", Path(tmp), include_exts={"docx"}, limit=1)

        self.assertEqual([r.output_path.name for r in results], ["b.docx"])


if __name__ == "__main__":
    unittest.main()
