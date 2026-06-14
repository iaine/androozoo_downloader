"""Desktop UI for the AndroZoo downloader.

Thin pywebview shell around :mod:`androzoo.core`. The Python side exposes a small
JS API (validate hashes, start a download, cancel, pick an output folder) and
pushes progress events into the page via evaluate_js. All download logic is
delegated to the core module so the CLI and UI share one implementation.

Run:
    pip install -e ".[ui]"
    python -m androzoo.ui.app        # or the azdownload-ui console command
"""

from __future__ import annotations

import json
import sys
import threading
from pathlib import Path

from androzoo import core as az

try:
    import webview  # pywebview; the importable module is named "webview"
except ImportError:  # pragma: no cover - only hit when the extra isn't installed
    sys.exit(
        "pywebview is not installed. Install the UI extra:\n"
        '    pip install -e ".[ui]"'
    )

ASSETS = Path(__file__).resolve().parent / "assets"

# Where to look for an optional azkey.toml to pre-fill the form: the current
# working directory first, then the repository root (two levels above this file:
# androzoo/ui/app.py -> repo root).
_CONFIG_LOCATIONS = (Path.cwd(), Path(__file__).resolve().parents[2])


class Api:
    """Methods callable from JavaScript as window.pywebview.api.<name>(...)."""

    def __init__(self) -> None:
        self._window: "webview.Window | None" = None
        self._cancel = threading.Event()
        self._worker: threading.Thread | None = None

    def bind(self, window: "webview.Window") -> None:
        self._window = window

    # -- helpers ---------------------------------------------------------- #

    def _emit(self, event: str, payload: dict) -> None:
        """Push an event into the page. Safe to call from a worker thread."""
        if self._window is None:
            return
        self._window.evaluate_js(f"window.appEvent({json.dumps(event)}, {json.dumps(payload)})")

    @staticmethod
    def _validate(hashes_text: str) -> tuple[list[str], int]:
        """Return (valid lowercased hashes, count of invalid/ignored lines).

        Uses the same validator as the core/CLI so the rule lives in one place.
        """
        valid: list[str] = []
        invalid = 0
        for raw in (hashes_text or "").splitlines():
            sha = raw.strip()
            if not sha or sha.startswith("#"):
                continue
            if az.is_valid_sha(sha):
                valid.append(sha.lower())
            else:
                invalid += 1
        return valid, invalid

    # -- API surface ------------------------------------------------------ #

    def prefill(self) -> dict:
        """Load key/output dir from an azkey.toml, if one is present nearby."""
        for base in _CONFIG_LOCATIONS:
            cfg_path = base / "azkey.toml"
            try:
                cfg = az.read_config(cfg_path)
            except Exception:
                cfg = {}
            if cfg:
                key = cfg.get("key", "")
                basedir = cfg.get("basedir", "")
                # Ignore the shipped placeholder template values.
                return {
                    "key": "" if str(key).startswith("%") else key,
                    "out_dir": "" if str(basedir).startswith("%") else basedir,
                }
        return {"key": "", "out_dir": ""}

    def pick_output_dir(self) -> str | None:
        if self._window is None:
            return None
        result = self._window.create_file_dialog(webview.FOLDER_DIALOG)
        return result[0] if result else None

    def cancel(self) -> None:
        self._cancel.set()

    def start_download(self, opts: dict) -> dict:
        """Validate inputs and kick off a background download.

        Returns immediately with {"ok": bool, "error": str, "total": int}.
        Progress arrives asynchronously via appEvent("progress"/"done"/"error").
        """
        if self._worker and self._worker.is_alive():
            return {"ok": False, "error": "A download is already running."}

        valid, invalid = self._validate(opts.get("hashes_text", ""))
        api_key = (opts.get("api_key") or "").strip()
        out_dir = (opts.get("out_dir") or "").strip()

        if not api_key:
            return {"ok": False, "error": "Enter your AndroZoo API key."}
        if not out_dir:
            return {"ok": False, "error": "Choose an output folder."}
        if not valid:
            return {"ok": False, "error": "Add at least one valid sha256 hash."}

        try:
            out = Path(out_dir)
            out.mkdir(parents=True, exist_ok=True)
        except OSError as e:
            return {"ok": False, "error": f"Cannot use output folder: {e}"}

        try:
            concurrency = max(1, min(int(opts.get("concurrency", 10)), az.MAX_RECOMMENDED_CONCURRENCY))
            retries = max(1, int(opts.get("retries", 3)))
        except (TypeError, ValueError):
            concurrency, retries = 10, 3

        jobs = [
            az.Job(
                sha=sha,
                api_key=api_key,
                out_dir=out,
                timeout=float(opts.get("timeout", 60.0)),
                retries=retries,
                verify=not bool(opts.get("no_verify", False)),
                force=bool(opts.get("force", False)),
            )
            for sha in valid
        ]

        self._cancel.clear()
        self._worker = threading.Thread(
            target=self._run, args=(jobs, concurrency), daemon=True
        )
        self._worker.start()
        return {"ok": True, "total": len(jobs), "invalid": invalid}

    # -- worker ----------------------------------------------------------- #

    def _run(self, jobs: list, concurrency: int) -> None:
        try:
            def on_result(res, done, total):
                self._emit("progress", {
                    "done": done,
                    "total": total,
                    "sha": res.sha,
                    "status": res.status.value,
                    "message": res.message,
                })

            results = az.run(jobs, concurrency, on_result=on_result, cancel_event=self._cancel)

            ok = sum(r.status is az.Status.OK for r in results)
            skipped = sum(r.status is az.Status.SKIPPED for r in results)
            failed = sum(r.status is az.Status.FAILED for r in results)
            self._emit("done", {
                "ok": ok,
                "skipped": skipped,
                "failed": failed,
                "total": len(jobs),
                "cancelled": self._cancel.is_set(),
            })
        except Exception as e:  # surface unexpected errors to the UI
            self._emit("error", {"message": str(e)})


def main() -> None:
    api = Api()
    window = webview.create_window(
        "AndroZoo Downloader",
        url=str(ASSETS / "index.html"),
        js_api=api,
        width=920,
        height=760,
        min_size=(640, 560),
        text_select=True,
    )
    api.bind(window)
    webview.start()


if __name__ == "__main__":
    main()
