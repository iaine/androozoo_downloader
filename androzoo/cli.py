"""Command-line interface for the AndroZoo downloader.

Usage:
    azdownload --key YOUR_KEY --input hashes.txt --out ./apks
    azdownload --config azkey.toml --concurrency 10
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

from . import core
from .core import LOGGER


def resolve_config(args: argparse.Namespace) -> dict:
    """Merge CLI flags over the TOML config (CLI wins)."""
    cfg = core.read_config(Path(args.config))
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
        prog="azdownload",
        description="Download APKs from AndroZoo given a list of sha256 hashes.",
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
    if args.concurrency > core.MAX_RECOMMENDED_CONCURRENCY:
        LOGGER.warning(
            "Concurrency %d exceeds AndroZoo's recommended max of %d",
            args.concurrency, core.MAX_RECOMMENDED_CONCURRENCY,
        )

    cfg = resolve_config(args)

    out_dir = Path(cfg["basedir"])
    out_dir.mkdir(parents=True, exist_ok=True)

    hashes = core.read_hashes(Path(cfg["input_file"]))
    if not hashes:
        LOGGER.error("No valid hashes found in %s", cfg["input_file"])
        return 1

    jobs = [
        core.Job(
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
    results = core.run(jobs, args.concurrency)

    ok = sum(r.status is core.Status.OK for r in results)
    skipped = sum(r.status is core.Status.SKIPPED for r in results)
    failed = [r for r in results if r.status is core.Status.FAILED]

    LOGGER.info("Done: %d downloaded, %d skipped, %d failed", ok, skipped, len(failed))
    if failed:
        LOGGER.error("Failed hashes:\n%s", "\n".join(f"  {r.sha}: {r.message}" for r in failed))
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
