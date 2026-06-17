# wps-cloud-download CL V1.0

Download WPS Cloud / KDocs files, folders, or search results from a personal logged-in account.

This project wraps the official `kdocs-cli` command line tool. It does not store WPS credentials and does not include any private file IDs, download URLs, cookies, or tokens.

## What It Does

- Download one WPS Cloud file by `drive_id` and `file_id`
- Download a folder recursively
- Download search results as a practical "download all" flow
- Preserve cloud paths for bulk downloads
- Verify returned `sha1` / `sha256` / `md5` hashes when WPS returns them
- Skip already downloaded files when the local hash matches
- Install `kdocs-cli` from the official WPS CDN with SHA256 verification

## What It Does Not Do

- It does not upload, move, rename, share, or delete cloud files.
- It does not bundle the `kdocs-cli` binary in this repository.
- It does not export or print WPS/KDocs tokens.
- `download-all` depends on KDocs search pagination and should be treated as a practical export path, not an official complete backup API.

## Install

Install the Python CLI:

```bash
pipx install git+https://github.com/maxleolee-eng/wps-cloud-download.git
```

Or from a local checkout:

```bash
pip install .
```

Install the required `kdocs-cli` binary:

```bash
wps-cloud-download install-kdocs
```

Check login status:

```bash
wps-cloud-download status
```

If not logged in, run:

```bash
kdocs-cli auth login
```

The login token is managed by `kdocs-cli` in the system keychain.

## Usage

Search files:

```bash
wps-cloud-download search "keyword" --file-type file
```

Download one file:

```bash
wps-cloud-download download-file \
  --drive-id <drive_id> \
  --file-id <file_id> \
  --output-dir ./downloaded
```

Preview a folder download:

```bash
wps-cloud-download download-folder \
  --drive-id <drive_id> \
  --folder-id <folder_id> \
  --output-dir ./downloaded-folder \
  --contents-only \
  --dry-run
```

Download a folder:

```bash
wps-cloud-download download-folder \
  --drive-id <drive_id> \
  --folder-id <folder_id> \
  --output-dir ./downloaded-folder \
  --contents-only
```

Preview bulk download:

```bash
wps-cloud-download download-all \
  --output-dir ./downloaded-all \
  --dry-run \
  --limit 20
```

Download only one file type:

```bash
wps-cloud-download download-all \
  --output-dir ./downloaded-docx \
  --ext docx \
  --limit 50
```

## Useful Guards

```bash
--dry-run              # Preview without writing files
--limit 10             # Limit number of files
--ext pdf              # Filter by extension
--overwrite            # Overwrite existing files
--continue-on-error    # Keep going after one file fails
--contents-only        # Download folder contents without wrapping in folder name
```

## Privacy And Safety

- Never commit downloaded documents, exports, cookies, tokens, or `.env` files.
- The repository `.gitignore` excludes common Office/PDF/image downloads and secret-like filenames.
- Download URLs are temporary and are not printed by the CLI.
- `kdocs-cli` auth state is outside this project and should remain in the system keychain.
- Use `--dry-run` before large downloads.

## Notes On `kdocs-cli`

`wps-cloud-download install-kdocs` downloads `kdocs-cli` from:

```text
https://wpsai.wpscdn.cn/skillhub/pro
```

The downloaded archive is verified against WPS-provided `checksums.txt` before installation.

The default object-download domain is `wps365.com`, because this path was verified to avoid `403 userNotLogin` responses seen with direct `kdocs.cn` / `wps.cn` object URLs.
