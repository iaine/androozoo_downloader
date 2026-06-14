"""AndroZoo APK downloader.

Reads a list of SHA-256 hashes and downloads the corresponding APKs from
AndroZoo (https://androzoo.uni.lu/) concurrently using a thread pool.

Configuration comes from command-line flags, with an optional TOML file as a
fallback. CLI flags always take precedence over the config file.

Example:
    python download.py --input hashes.txt --out ./apks --key YOUR_KEY
    python download.py --config azkey.toml --concurrency 10
"""

from __future__ import annotations

import argparse
import hashlib
import logging
import sys
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


def _read_config(path: Path) -> dict:
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
            res = fut.result()
            results.append(res)
            level = logging.INFO if res.status is not Status.FAILED else logging.ERROR
            LOGGER.log(level, "%-8s %s %s", res.status.value, res.sha, res.message)
            if on_result is not None:
                on_result(res, len(results), total)
    return results


def resolve_config(args: argparse.Namespace) -> dict:
    """Merge CLI flags over the TOML config (CLI wins)."""
    cfg = _read_config(Path(args.config))
    merged = {
        "key": args.key or cfg.get("key"),
        "input_file": args.input or cfg.get("input_file"),
        "basedir": args.out or cfg.get("basedir"),
    }
    missing = [k for k, v in merged.items() if not v]
    if missing:
        raise SystemExit(
            f"Missing required setting(s): {', '.join(missing)}. "
            f"Provide them via CLI flags or {args.config}."
        )
    return merged


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Download APKs from AndroZoo given a list of sha256 hashes."
    )
    p.add_argument("--config", default="azkey.toml", help="TOML config file (fallback for flags)")
    p.add_argument("--key", help="AndroZoo API key (overrides config)")
    p.add_argument("--input", help="File with one sha256 per line (overrides config)")
    p.add_argument("--out", help="Output directory (overrides config)")
    p.add_argument("--concurrency", type=int, default=10, help="Number of worker threads")
    p.add_argument("--retries", type=int, default=3, help="Attempts per file before giving up")
    p.add_argument("--timeout", type=float, default=60.0, help="Per-request timeout in seconds")
    p.add_argument("--no-verify", action="store_true", help="Skip sha256 integrity check")
    p.add_argument("--force", action="store_true", help="Re-download files already on disk")
    p.add_argument("-v", "--verbose", action="store_true", help="Enable debug logging")
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )

    if args.concurrency < 1:
        raise SystemExit("--concurrency must be at least 1")
    if args.concurrency > MAX_RECOMMENDED_CONCURRENCY:
        LOGGER.warning(
            "Concurrency %d exceeds AndroZoo's recommended max of %d",
            args.concurrency, MAX_RECOMMENDED_CONCURRENCY,
        )

    cfg = resolve_config(args)

    out_dir = Path(cfg["basedir"])
    out_dir.mkdir(parents=True, exist_ok=True)

    hashes = read_hashes(Path(cfg["input_file"]))
    if not hashes:
        LOGGER.error("No valid hashes found in %s", cfg["input_file"])
        return 1

    jobs = [
        Job(
            sha=sha,
            api_key=cfg["key"],
            out_dir=out_dir,
            timeout=args.timeout,
            retries=args.retries,
            verify=not args.no_verify,
            force=args.force,
        )
        for sha in hashes
    ]

    LOGGER.info("Downloading %d APK(s) with %d threads", len(jobs), args.concurrency)
    results = run(jobs, args.concurrency)

    ok = sum(r.status is Status.OK for r in results)
    skipped = sum(r.status is Status.SKIPPED for r in results)
    failed = [r for r in results if r.status is Status.FAILED]

    LOGGER.info("Done: %d downloaded, %d skipped, %d failed", ok, skipped, len(failed))
    if failed:
        LOGGER.error("Failed hashes:\n%s", "\n".join(f"  {r.sha}: {r.message}" for r in failed))
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
