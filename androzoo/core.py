"""Core download logic for the AndroZoo downloader.

Pure library code with no CLI or UI concerns: data types, hash/config readers,
the single-file downloader, and the concurrent runner. Both the CLI
(:mod:`androzoo.cli`) and the desktop UI (:mod:`androzoo.ui.app`) build on this.
"""

from __future__ import annotations

import hashlib
import http.client
import logging
import threading
import time
import tomllib
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import urlopen

LOGGER = logging.getLogger("androzoo")

API_URL = "https://androzoo.uni.lu/api/download"

# AndroZoo asks clients to use no more than ~20 concurrent connections.
MAX_RECOMMENDED_CONCURRENCY = 20

# A valid AndroZoo sha256 is 64 hex characters.
SHA256_LENGTH = 64


class Status(str, Enum):
    OK = "ok"
    SKIPPED = "skipped"
    FAILED = "failed"


@dataclass(frozen=True)
class Result:
    sha: str
    status: Status
    message: str = ""


@dataclass(frozen=True)
class Job:
    sha: str
    api_key: str
    out_dir: Path
    timeout: float
    retries: int
    verify: bool
    force: bool


def read_config(path: Path) -> dict:
    """Load a TOML config file. Returns an empty dict if the file is absent."""
    if not path.exists():
        return {}
    with open(path, "rb") as f:
        return tomllib.load(f)


def read_hashes(path: Path) -> list[str]:
    """Read and validate sha256 hashes from a file, one per line.

    Blank lines and lines starting with '#' are ignored. Invalid hashes are
    skipped with a warning rather than aborting the whole run.
    """
    hashes: list[str] = []
    for lineno, raw in enumerate(path.read_text().splitlines(), start=1):
        sha = raw.strip()
        if not sha or sha.startswith("#"):
            continue
        if len(sha) != SHA256_LENGTH or not all(c in "0123456789abcdefABCDEF" for c in sha):
            LOGGER.warning("Skipping invalid sha256 on line %d: %r", lineno, raw)
            continue
        hashes.append(sha.lower())
    return hashes


def _sha256_of(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def download_one(job: Job) -> Result:
    """Download a single APK, with retries, verification and resume support.

    This function never raises for expected network/IO problems; it returns a
    Result describing the outcome so the caller can aggregate and report.
    """
    dest = job.out_dir / f"{job.sha}.apk"

    # Resume: skip files that already exist (and verify if asked).
    if dest.exists() and not job.force:
        if not job.verify or _sha256_of(dest) == job.sha:
            return Result(job.sha, Status.SKIPPED, "already present")
        LOGGER.warning("%s exists but failed verification; re-downloading", job.sha)

    url = f"{API_URL}?{urlencode({'apikey': job.api_key, 'sha256': job.sha})}"
    tmp = dest.with_suffix(".apk.part")

    last_error = "unknown error"
    for attempt in range(1, job.retries + 1):
        try:
            with urlopen(url, timeout=job.timeout) as resp:
                data = resp.read()

            if job.verify:
                digest = hashlib.sha256(data).hexdigest()
                if digest != job.sha:
                    last_error = f"sha mismatch (got {digest})"
                    LOGGER.warning("%s: %s (attempt %d)", job.sha, last_error, attempt)
                    continue

            tmp.write_bytes(data)
            tmp.replace(dest)  # atomic move into place
            return Result(job.sha, Status.OK)

        except HTTPError as e:
            # 401/403 = bad key, 404 = unknown sha: no point retrying those.
            last_error = f"HTTP {e.code} {e.reason}"
            if e.code in (401, 403, 404):
                return Result(job.sha, Status.FAILED, last_error)
            if e.code == 429:
                LOGGER.warning("%s: rate limited, backing off", job.sha)
        except (URLError, TimeoutError, OSError) as e:
            last_error = str(e)
        except http.client.HTTPException as e:
            # e.g. IncompleteRead from a connection dropped mid-body. These are
            # not OSError subclasses, so catch them explicitly and retry rather
            # than letting them escape and abort the whole run.
            last_error = f"{type(e).__name__}: {e}"
            LOGGER.warning("%s: %s (attempt %d)", job.sha, last_error, attempt)

        if attempt < job.retries:
            time.sleep(min(2 ** attempt, 30))  # exponential backoff, capped

    return Result(job.sha, Status.FAILED, last_error)


def run(
    jobs: list[Job],
    concurrency: int,
    on_result: Callable[[Result, int, int], None] | None = None,
    cancel_event: threading.Event | None = None,
) -> list[Result]:
    """Download all jobs concurrently.

    :param on_result: optional callback invoked as ``on_result(result, completed, total)``
        each time a file finishes. Used by the UI to drive a progress bar.
    :param cancel_event: optional event; when set, pending (not-yet-started) jobs
        are cancelled and the run stops as soon as in-flight downloads return.
    """
    total = len(jobs)
    results: list[Result] = []
    with ThreadPoolExecutor(max_workers=concurrency) as pool:
        futures = {pool.submit(download_one, job): job for job in jobs}
        for fut in as_completed(futures):
            if cancel_event is not None and cancel_event.is_set():
                for pending in futures:
                    pending.cancel()
                break
            try:
                res = fut.result()
            except Exception as e:  # defensive: never let one item abort the batch
                job = futures[fut]
                res = Result(job.sha, Status.FAILED, f"unexpected error: {type(e).__name__}: {e}")
                LOGGER.exception("Unexpected error downloading %s", job.sha)
            results.append(res)
            level = logging.INFO if res.status is not Status.FAILED else logging.ERROR
            LOGGER.log(level, "%-8s %s %s", res.status.value, res.sha, res.message)
            if on_result is not None:
                on_result(res, len(results), total)
    return results
