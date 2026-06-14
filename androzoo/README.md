# AndroZoo Downloader

A small Python tool for downloading APKs from [AndroZoo](https://androzoo.uni.lu/)
given a list of SHA-256 hashes. It ships in two forms that share one
implementation: a command-line tool and an accessible desktop UI.

- **Concurrent** downloads via a thread pool (right-sized for network I/O).
- **Verified** — each file is checked against its expected sha256.
- **Resumable** — files already present are skipped, so interrupted runs restart cleanly.
- **Polite** — per-file retries with capped backoff; fails fast on bad key / unknown hash.

---

## Requirements

- **Python 3.11 or newer** (the tool uses the standard-library `tomllib`).
- The core CLI has **no third-party dependencies**.
- The UI needs [`pywebview`](https://pywebview.flowrl.com/); tests need `pytest`.
  Both come from optional extras (below).

## Layout

```
androzoo/
  __init__.py          # public API (Job, Result, run, download_one, read_hashes)
  core.py              # download logic, data types, hash/config readers
  cli.py               # argparse + main (the azdownload command)
  ui/
    app.py             # pywebview bridge (imports androzoo.core)
    assets/
      index.html
      styles.css
      app.js
test_download.py       # pytest suite
pyproject.toml         # packaging, Python floor, extras, entry points
azkey.toml             # optional config (template; fill in or override via flags)
```

## Install

From the repository root:

```bash
# core only
pip install -e .

# with the UI and test extras
pip install -e ".[ui,dev]"
```

This installs two console commands: `azdownload` (the CLI) and `azdownload-ui`
(the desktop UI).

---

## Command-line usage

Provide settings as flags, or put them in `azkey.toml` and let the flags
override as needed. A hash file is one sha256 per line; blank lines and lines
starting with `#` are ignored.

```bash
azdownload --key YOUR_KEY --input hashes.txt --out ./apks
# or, equivalently:
python -m androzoo.cli --key YOUR_KEY --input hashes.txt --out ./apks
```

### Options

| Flag | Default | Meaning |
|------|---------|---------|
| `--config` | `azkey.toml` | TOML file used as a fallback for the settings below |
| `--key` | — | AndroZoo API key (overrides config) |
| `--input` | — | File with one sha256 per line (overrides config) |
| `--out` | — | Output directory (overrides config) |
| `--concurrency` | `10` | Worker threads (warns above AndroZoo's recommended 20) |
| `--retries` | `3` | Attempts per file before giving up |
| `--timeout` | `60` | Per-request timeout, in seconds |
| `--no-verify` | off | Skip the sha256 integrity check |
| `--force` | off | Re-download files already on disk |
| `-v`, `--verbose` | off | Debug logging |

Files are saved as `<sha256>.apk` in the output directory. The process exits
`0` when everything succeeds (or is skipped) and `1` if any download fails; the
failed hashes are listed at the end.

### Config file

`azkey.toml` ships as a template. Replace the placeholders with real values
(note the quotes — TOML strings must be quoted):

```toml
key = "YOUR_ANDROZOO_API_KEY"
input_file = "hashes.txt"
basedir = "./apks"
```

Keep your real key out of version control. Add the populated file to
`.gitignore` and commit only the template.

---

## Desktop UI

```bash
pip install -e ".[ui]"
python -m androzoo.ui.app
```

The window lets you supply hashes three ways — drop a file onto the zone,
choose a file, or paste/type directly — and shows a live count of valid versus
ignored lines. Enter your API key, pick an output folder, then **Start
download**. A progress bar and a per-hash results table update while the window
stays open; **Cancel** stops scheduling further downloads.

If an `azkey.toml` with real (non-placeholder) values is present in the working
directory or the repository root, the key and output folder are pre-filled.

### Accessibility

The UI targets **WCAG 2.1 AA**: associated labels on every control, a
keyboard-operable drop zone with a file-picker alternative to dragging, a
native `<progress>` element with a throttled polite live region, status shown
by icon *and* text (not colour alone), visible keyboard focus, a dark-mode
palette, and `prefers-reduced-motion` support. All text/UI colours meet or
exceed 4.5:1 contrast in both light and dark themes.

---

## Testing

```bash
pip install -e ".[dev]"
pytest
```

The suite stubs the network (it never makes real requests) and patches out
backoff sleeps, so it runs in well under a second. It covers hash validation,
verified downloads, resume/`--force`, sha-mismatch and HTTP-error handling,
retry behaviour, the progress callback, cancellation, config merging, and the
`main()` exit codes.

---

## Smoke testing

Automated tests cover the core logic, but two things need a human: real network
behaviour against AndroZoo, and the GUI (which can't run headless). Use the
checks below before trusting a release.

### 1. CLI against a tiny real batch

Get a valid API key and two or three known sha256 hashes, then:

```bash
printf '%s\n' SHA_ONE SHA_TWO > smoke.txt
azdownload --key "$KEY" --input smoke.txt --out ./smoke-out -v
```

Confirm:

- Files land in `./smoke-out` named `<sha256>.apk`.
- The log ends with `Done: N downloaded, 0 skipped, 0 failed` and exit code `0`
  (`echo $?`).
- **Verification works:** `sha256sum ./smoke-out/*.apk` matches the input hashes.
- **Resume works:** run the same command again — every file reports `skipped`
  and nothing is re-downloaded.
- **Force works:** add `--force` and confirm the files download again.

### 2. CLI error paths

- **Bad key:** use a wrong key — downloads should fail fast with an HTTP
  401/403 message and exit code `1`, with no `.apk` written.
- **Unknown hash:** include a syntactically valid but nonexistent sha256 — it
  should fail with HTTP 404, fail fast (no retries), and be listed at the end.
- **Bad input:** point `--input` at a file of junk lines — invalid lines are
  warned and skipped; if none are valid, the tool exits `1` with a clear message.
- **Concurrency guard:** pass `--concurrency 50` and confirm it warns about
  AndroZoo's recommended maximum but still runs.

### 3. UI smoke test

The GUI cannot run in a headless environment, so test it on a desktop session:

```bash
pip install -e ".[ui,dev]"
python -m androzoo.ui.app
```

Walk through:

- **Three input methods:** paste hashes, choose a file, and drag-drop a file —
  each updates the valid/ignored count.
- **Folder picker:** the **Choose…** button opens a native dialog and fills the
  output field.
- **Happy path:** start a small batch and watch the progress bar advance and
  rows appear (newest first) with `Done`/`Skipped`/`Failed` status.
- **Cancel:** start a larger batch and cancel mid-run; the run stops and reports
  `Cancelled`. *(In-flight requests finish before the run ends — see Known
  limitations.)*
- **Prefill:** with a real-valued `azkey.toml` in the working directory or repo root, confirm the
  key and output folder populate on launch.

#### Accessibility spot-checks

- **Keyboard only:** unplug the mouse. Tab through every control; the drop zone
  should be reachable and open the file picker on Enter/Space; focus must be
  clearly visible at each stop. The skip link should appear on first Tab.
- **Screen reader:** with VoiceOver (macOS), NVDA (Windows), or Orca (Linux),
  confirm controls announce their labels, errors announce on appearance, and
  progress is announced periodically (roughly every 10%) rather than per file.
- **Themes & motion:** toggle OS dark mode and confirm the palette switches and
  stays readable; enable "reduce motion" and confirm transitions are suppressed.
- **Zoom:** at 200% browser/OS zoom the layout should reflow without clipping or
  horizontal scrolling.

If the bridge misbehaves on your `pywebview` version, the most version-sensitive
spots are `window.create_file_dialog(...)` (the folder picker) and
`window.evaluate_js(...)` (progress events) in `androzoo/ui/app.py`.

---

## Known limitations

- **Cancellation** stops *pending* downloads but cannot abort an HTTP request
  already in flight; those finish before the run ends. Making cancellation
  instant would require checking the cancel flag inside the per-file worker.
- **Rate limiting** is bounded only by the thread count. If AndroZoo pushes
  back, lower `--concurrency` or add a token-bucket throttle.
- **Input format** is one sha256 per line. AndroZoo's full CSV index has many
  columns; feeding that directly would need a column selector first.

## Contributing

Please file issues, bugs, and feature requests on the GitHub issue queue.
