from __future__ import annotations

import hashlib
import os
import platform
import shutil
import subprocess
import tarfile
import tempfile
import urllib.request
import zipfile
from pathlib import Path

from .cli_core import DownloadError

KDOCS_CLI_VERSION = "2.5.12"
KDOCS_CLI_CDN = "https://wpsai.wpscdn.cn/skillhub/pro"


def normalize_os(value: str) -> str:
    lower = value.lower()
    if lower.startswith("darwin"):
        return "darwin"
    if lower.startswith("linux"):
        return "linux"
    if lower.startswith(("windows", "mingw", "msys", "cygwin")):
        return "windows"
    raise DownloadError(f"unsupported OS: {value}")


def normalize_arch(value: str) -> str:
    lower = value.lower()
    if lower in {"x86_64", "amd64"}:
        return "amd64"
    if lower in {"arm64", "aarch64"}:
        return "arm64"
    raise DownloadError(f"unsupported architecture: {value}")


def archive_name(version: str, os_name: str, arch: str) -> str:
    ext = ".zip" if os_name == "windows" else ".tar.gz"
    return f"kdocs-cli-{version}-{os_name}-{arch}{ext}"


def default_install_dir(os_name: str) -> Path:
    if os_name == "windows":
        base = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
        return Path(base) / "kdocs-cli"
    return Path.home() / ".local" / "bin"


def existing_version(executable: str = "kdocs-cli") -> str | None:
    try:
        proc = subprocess.run([executable, "version"], text=True, capture_output=True)
    except OSError:
        return None
    if proc.returncode != 0:
        return None
    return proc.stdout.strip() or None


def download(url: str, dest: Path) -> None:
    request = urllib.request.Request(url, headers={"User-Agent": "wps-cloud-download/1.0"})
    with urllib.request.urlopen(request, timeout=120) as response, dest.open("wb") as output:
        shutil.copyfileobj(response, output)


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def expected_checksum(checksums_path: Path, wanted_archive: str) -> str | None:
    for line in checksums_path.read_text(encoding="utf-8").splitlines():
        parts = line.split()
        if len(parts) >= 2 and parts[1] == wanted_archive:
            return parts[0].lower()
    return None


def extract_archive(archive: Path, dest: Path, os_name: str) -> None:
    dest.mkdir(parents=True, exist_ok=True)
    if os_name == "windows":
        with zipfile.ZipFile(archive) as zf:
            zf.extractall(dest)
        return
    with tarfile.open(archive, "r:gz") as tf:
        tf.extractall(dest)


def find_extracted_binary(root: Path, os_name: str) -> Path:
    target = "kdocs-cli.exe" if os_name == "windows" else "kdocs-cli"
    matches = list(root.rglob(target))
    if not matches:
        raise DownloadError("kdocs-cli binary not found in downloaded archive")
    return matches[0]


def install_kdocs_cli(
    *,
    version: str = KDOCS_CLI_VERSION,
    cdn: str = KDOCS_CLI_CDN,
    install_dir: Path | None = None,
    force: bool = False,
) -> Path:
    os_name = normalize_os(platform.system())
    arch = normalize_arch(platform.machine())
    install_dir = install_dir or default_install_dir(os_name)
    binary_name = "kdocs-cli.exe" if os_name == "windows" else "kdocs-cli"
    target = install_dir / binary_name

    current = existing_version(str(target)) or existing_version("kdocs-cli")
    if current == version and target.exists() and not force:
        return target

    name = archive_name(version, os_name, arch)
    base = f"{cdn}/v{version}/releases"
    with tempfile.TemporaryDirectory(prefix="wps-cloud-kdocs-") as tmp:
        tmp_path = Path(tmp)
        archive = tmp_path / name
        checksums = tmp_path / "checksums.txt"
        download(f"{base}/{name}", archive)
        download(f"{base}/checksums.txt", checksums)
        expected = expected_checksum(checksums, name)
        if not expected:
            raise DownloadError(f"checksum not found for {name}")
        actual = sha256(archive)
        if actual.lower() != expected:
            raise DownloadError(f"kdocs-cli archive checksum mismatch: expected {expected}, got {actual}")

        extract_dir = tmp_path / "extract"
        extract_archive(archive, extract_dir, os_name)
        source = find_extracted_binary(extract_dir, os_name)
        install_dir.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)
        if os_name != "windows":
            target.chmod(0o755)
        try:
            (install_dir / ".source").write_text("wps-cloud-download", encoding="utf-8")
        except OSError:
            pass
    return target
