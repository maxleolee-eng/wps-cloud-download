import unittest

from wps_cloud.kdocs_installer import archive_name, normalize_arch, normalize_os


class KDocsInstallerTests(unittest.TestCase):
    def test_normalizes_supported_platforms(self):
        self.assertEqual(normalize_os("Darwin"), "darwin")
        self.assertEqual(normalize_os("Linux"), "linux")
        self.assertEqual(normalize_os("Windows"), "windows")
        self.assertEqual(normalize_arch("x86_64"), "amd64")
        self.assertEqual(normalize_arch("arm64"), "arm64")

    def test_builds_archive_name(self):
        self.assertEqual(
            archive_name("2.5.12", "darwin", "arm64"),
            "kdocs-cli-2.5.12-darwin-arm64.tar.gz",
        )
        self.assertEqual(
            archive_name("2.5.12", "windows", "amd64"),
            "kdocs-cli-2.5.12-windows-amd64.zip",
        )


if __name__ == "__main__":
    unittest.main()
