# Universal Media Downloader

A robust, interactive Python script that downloads videos, images, and GIFs from web pages and known media URLs. It uses `yt-dlp` as the primary extractor and provides retries/fallbacks (requests/curl), optional `ffmpeg`/`ffprobe` support for probing and fixing containers, and summary logging (JSONL + human-readable text).

> **Disclaimer / Legal:** Use this tool only to download media you own, have permission to download, or which is explicitly licensed for download. Do not use it to infringe copyright or circumvent access controls.

---

## Quick facts

* **Script:** `Universal-Media-downloader.py`
* **Main outputs:**

  * Videos saved to the configured `VIDEOS_DIR`.
  * Images saved to the configured `IMAGES_DIR`.
  * Logs: `vd_downloads.jsonl` (JSONL) and `vd_downloads.txt` (human-readable) in the current working directory.
* **Interactive:** prompts for a single URL, multiple URLs, or a path to a text-file containing URLs (one per line).

---

## Prerequisites

* **Python:** 3.9+ (works on Windows/macOS/Linux)
* **PIP packages:** `yt-dlp`, `requests`, `beautifulsoup4`. `Pillow` (optional, used to get image resolution metadata).
* **Recommended tooling (optional but strongly recommended):** `ffmpeg` / `ffprobe` on PATH (used for probing containers and fixing merges), and `curl` (fallback for some direct downloads).

Install Python packages with:

```bash
python -m pip install -U yt-dlp requests beautifulsoup4 pillow
```

Install ffmpeg:

* macOS: `brew install ffmpeg`
* Debian/Ubuntu: `sudo apt install ffmpeg`
* Windows: download from [https://ffmpeg.org](https://ffmpeg.org) and add to your PATH

`curl` is usually present on macOS/Linux; on Windows, install via `choco install curl` or use Git Bash which provides `curl`.

---

## How to run

From your shell / terminal (in the folder containing `Universal-Media-downloader.py`):

```bash
python Universal-Media-downloader.py
```

When prompted, supply either:

* A single URL (e.g. `https://example.com/video-page`),
* Multiple URLs separated by spaces, or
* A path to a text file whose contents are URLs (one URL per line). For example: `C:\Users\you\links.txt` or `./links.txt`.

**Examples:**

* Single URL: `https://youtu.be/dQw4w9WgXcQ`
* Multiple: paste `https://site/a https://site/b` when prompted
* Batch mode (links file): create `links.txt` with URLs (one per line) and pass its path when prompted.

The script will print progress to the console and a summarized `Download Summary` on each successful file. Verbose `yt-dlp` logs are generated only when a download fails to help debugging.

---

## Important configuration variables (edit the top of the script)

Open the script and edit the following constants to suit your environment (they are near the top of `Universal-Media-downloader.py`):

* `VIDEOS_DIR` — the directory where downloaded video files are stored. By default this may point to a Windows path like `D:\Huzaifa\Videos\video downloads` in the supplied file. Change to a directory you control, for example `Path.cwd() / 'downloads' / 'videos'`.
* `IMAGES_DIR` — the directory where images are saved.
* `FORCE_OVERWRITE` — controlled by the env var `VD_FORCE_OVERWRITE`; set to `1` to force overwrites. Example:

  * Linux/macOS: `export VD_FORCE_OVERWRITE=1`
  * Windows (PowerShell): `$env:VD_FORCE_OVERWRITE = '1'`
* `USER_AGENTS` — list of user-agent strings the downloader rotates through.

You can also change the accepted media extensions (`IMAGE_EXTS`, `VIDEO_EXTS`) or other behavior by editing the script.

---

## Cookie handling

* The script will use `cookies.txt` or `yt_cookies.txt` in the current working directory if present.
* You may also set `YDLP_COOKIEFILE` or `YT_COOKIES` environment variables to point to a cookies file path.

This is useful when you need authenticated access for certain sites. Use standard `cookies.txt` exported from browser extensions or `yt-dlp --cookies` format.

---

## Output & logs

* `vd_downloads.jsonl` — append-only JSON Lines file containing metadata per download (path, size, probe info, timestamps).
* `vd_downloads.txt` — human-readable summary table (created/updated as downloads complete).
* On errors, the script attempts to produce a verbose `yt_dlp` run log file, named like `yt_dlp_verbose_<timestamp>.log`.

---

## Environment flags and runtime tweaks

* `VD_FORCE_OVERWRITE=1` — force overwriting of existing outputs.
* To increase HTTP retry resilience, adjust the `requests_session` settings in the code.
* To prefer a different output layout, change `out_template` in the `download_video_with_yt_dlp` function.

---

## Batch usage (automated)

To run non-interactively in batch mode, prepare a `links.txt` file (one URL per line) and run the script, entering the path to `links.txt` when prompted. The script will detect a path to a file and treat it as batch-mode URLs.

For full automation (cron/job), consider creating a wrapper that executes the script and supplies the path via stdin or modifies the script to accept command-line args.

---

## Troubleshooting

* **Missing packages error:** Run the pip install command shown in Prerequisites.
* **`yt-dlp` extraction failures:** Update `yt-dlp` (`python -m pip install -U yt-dlp`) and try different `player_client` extractor arguments (the script already cycles through several variants for YouTube edge-cases).
* **`ffprobe` not found:** If container metadata is unknown and you want full probing/merge fixes, install `ffmpeg` / `ffprobe` and ensure they are on PATH.
* **Permission errors writing to `VIDEOS_DIR`/`IMAGES_DIR`:** pick directories you have write access to or run with appropriate privileges.

---

## Customization notes for other users

* To ship this repo to other team members, replace absolute Windows paths with relative (repo-local) defaults, e.g.: `VIDEOS_DIR = Path.cwd() / 'downloads' / 'videos'`.
* Consider extracting config into a small `config.toml` or environment variables so users can change paths without editing the script.
* If you want programmatic usage, refactor `handle_url`/`download_video_with_yt_dlp` into an importable module and expose a small API.

---

## Contributing

* Create a branch, add tests or run manual checks, and open a PR.
* Suggested improvements:

  * Add CLI flags with `argparse` (current script is interactive-only).
  * Move configuration to a dedicated file or support CLI overrides.
  * Add unit tests for helper functions (filename sanitization, detection heuristics).

---

## Example edits to make it multi-user friendly

1. Replace hard-coded paths:

```py
VIDEOS_DIR = Path.cwd() / 'downloads' / 'videos'
IMAGES_DIR = Path.cwd() / 'downloads' / 'images'
```

2. Add `argparse` support to allow `--links-file` and `--urls` flags.
3. Expose a `--non-interactive` or `--yes` flag to bypass prompts for automated runs.

---

## A minimal example session

```
$ python Universal-Media-downloader.py
Universal media downloader (videos + images + gifs) - robust edition
You may paste multiple URLs separated by spaces/newlines, or enter path to a text file containing URLs.
Cookie handling: uses cookies.txt if present (explicit).
Videos will be saved to: /home/user/downloads/videos
Images will be saved to: /home/user/downloads/images
Enter URLs or path to file: https://example.com/video-page

Processing: https://example.com/video-page
Downloading: ... (progress shown)

Download Summary
----------------
Saved to : /home/user/downloads/videos/...
Size      : 12.34 MB
Duration  : 00:03:21
Total time taken: 00:00:17
Resolution: 720p (width 1280)
Audio     : Yes
----------------
```

---

## License

Add a license file to the repository (for example, `MIT`) to clarify reuse and redistribution terms.

---

If you want, I can:

* produce a `README.md` file for you and add it to the repo,
* or create a `config.example.toml` and a small `cli.py` wrapper that accepts arguments, or
* convert the script to accept command-line flags (non-interactive friendly).

Tell me which of the above you prefer and I will produce it next.
