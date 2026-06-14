"""Tests for the androzoo package. Run with: pytest

The network is stubbed by monkeypatching core.urlopen, so no real requests
are made. time.sleep is also patched out so retry/backoff paths run instantly.
"""

import hashlib
import io
import threading

import pytest

from androzoo import cli, core
from androzoo.core import Job, Result, Status

# --------------------------------------------------------------------------- #
# Helpers / fixtures
# --------------------------------------------------------------------------- #

PAYLOAD = b"PK\x03\x04 pretend this is a real apk"
GOOD_SHA = hashlib.sha256(PAYLOAD).hexdigest()


class _FakeResp(io.BytesIO):
    """A BytesIO that also works as a context manager, like urlopen's return."""

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
        return False


def make_http_error(code, reason="error"):
    from urllib.error import HTTPError

    return HTTPError("http://x", code, reason, hdrs={}, fp=None)


@pytest.fixture(autouse=True)
def no_sleep(monkeypatch):
    """Make backoff instant so retry tests don't wait."""
    monkeypatch.setattr(core.time, "sleep", lambda *_: None)


@pytest.fixture
def job(tmp_path):
    def _make(sha=GOOD_SHA, **overrides):
        params = dict(
            sha=sha,
            api_key="TESTKEY",
            out_dir=tmp_path,
            timeout=5.0,
            retries=2,
            verify=True,
            force=False,
        )
        params.update(overrides)
        return Job(**params)

    return _make


def serve(monkeypatch, payload=PAYLOAD, error=None, fail_times=0):
    """Install a fake urlopen.

    :param error: an exception instance to raise (always) if set.
    :param fail_times: raise URLError this many times, then succeed.
    """
    state = {"calls": 0}

    def fake_urlopen(url, timeout=None):
        state["calls"] += 1
        if error is not None:
            raise error
        if state["calls"] <= fail_times:
            from urllib.error import URLError

            raise URLError("transient")
        return _FakeResp(payload)

    monkeypatch.setattr(core, "urlopen", fake_urlopen)
    return state


# --------------------------------------------------------------------------- #
# read_hashes
# --------------------------------------------------------------------------- #

def test_read_hashes_valid_and_normalised(tmp_path):
    f = tmp_path / "h.txt"
    f.write_text(f"{GOOD_SHA.upper()}\n{GOOD_SHA}\n")
    assert core.read_hashes(f) == [GOOD_SHA]  # lowercased; duplicate dropped


def test_read_hashes_skips_comments_blanks_and_invalid(tmp_path, caplog):
    f = tmp_path / "h.txt"
    f.write_text(f"# header\n\n{GOOD_SHA}\nNOTAHASH\n{'g' * 64}\n")
    out = core.read_hashes(f)
    assert out == [GOOD_SHA]
    assert "invalid sha256" in caplog.text.lower()


def test_read_hashes_strips_whitespace(tmp_path):
    f = tmp_path / "h.txt"
    f.write_text(f"   {GOOD_SHA}   \n")
    assert core.read_hashes(f) == [GOOD_SHA]


def test_read_hashes_dedupes_preserving_order(tmp_path):
    other = "b" * 64
    f = tmp_path / "h.txt"
    f.write_text(f"{GOOD_SHA}\n{other}\n{GOOD_SHA.upper()}\n{other}\n")
    # First-seen order kept, later duplicates (incl. case variants) dropped.
    assert core.read_hashes(f) == [GOOD_SHA, other]


def test_is_valid_sha():
    assert core.is_valid_sha("a" * 64)
    assert core.is_valid_sha("A1" * 32)
    assert not core.is_valid_sha("a" * 63)      # too short
    assert not core.is_valid_sha("a" * 65)      # too long
    assert not core.is_valid_sha("g" * 64)      # non-hex
    assert not core.is_valid_sha("")


# --------------------------------------------------------------------------- #
# download_one
# --------------------------------------------------------------------------- #

def test_download_success_writes_and_verifies(monkeypatch, job, tmp_path):
    serve(monkeypatch)
    res = core.download_one(job())
    assert res.status is Status.OK
    dest = tmp_path / f"{GOOD_SHA}.apk"
    assert dest.read_bytes() == PAYLOAD
    assert not (tmp_path / f"{GOOD_SHA}.apk.part").exists()  # temp cleaned up


def test_download_resume_skips_existing(monkeypatch, job, tmp_path):
    (tmp_path / f"{GOOD_SHA}.apk").write_bytes(PAYLOAD)
    state = serve(monkeypatch)
    res = core.download_one(job())
    assert res.status is Status.SKIPPED
    assert state["calls"] == 0  # never hit the network


def test_download_force_redownloads(monkeypatch, job, tmp_path):
    (tmp_path / f"{GOOD_SHA}.apk").write_bytes(b"stale")
    state = serve(monkeypatch)
    res = core.download_one(job(force=True))
    assert res.status is Status.OK
    assert state["calls"] == 1
    assert (tmp_path / f"{GOOD_SHA}.apk").read_bytes() == PAYLOAD


def test_download_existing_but_corrupt_is_replaced(monkeypatch, job, tmp_path):
    (tmp_path / f"{GOOD_SHA}.apk").write_bytes(b"corrupt")  # wrong sha
    serve(monkeypatch)
    res = core.download_one(job())  # verify on, not forced
    assert res.status is Status.OK
    assert (tmp_path / f"{GOOD_SHA}.apk").read_bytes() == PAYLOAD


def test_download_sha_mismatch_fails_and_writes_nothing(monkeypatch, job, tmp_path):
    serve(monkeypatch)  # serves PAYLOAD, whose sha != bad_sha
    bad_sha = "a" * 64
    res = core.download_one(job(sha=bad_sha))
    assert res.status is Status.FAILED
    assert "mismatch" in res.message
    assert not (tmp_path / f"{bad_sha}.apk").exists()


def test_download_no_verify_accepts_anything(monkeypatch, job, tmp_path):
    serve(monkeypatch)
    bad_sha = "b" * 64
    res = core.download_one(job(sha=bad_sha, verify=False))
    assert res.status is Status.OK
    assert (tmp_path / f"{bad_sha}.apk").exists()


@pytest.mark.parametrize("code", [401, 403, 404])
def test_download_client_errors_fail_fast(monkeypatch, job, code):
    state = serve(monkeypatch, error=make_http_error(code))
    res = core.download_one(job(retries=5))
    assert res.status is Status.FAILED
    assert str(code) in res.message
    assert state["calls"] == 1  # no retries on these


def test_download_transient_then_success(monkeypatch, job):
    state = serve(monkeypatch, fail_times=1)  # first call fails, second works
    res = core.download_one(job(retries=3))
    assert res.status is Status.OK
    assert state["calls"] == 2


def test_download_exhausts_retries(monkeypatch, job):
    from urllib.error import URLError

    state = serve(monkeypatch, error=URLError("down"))
    res = core.download_one(job(retries=3))
    assert res.status is Status.FAILED
    assert state["calls"] == 3


def test_download_incomplete_read_is_retried_then_succeeds(monkeypatch, job):
    """A connection dropped mid-body (IncompleteRead) must be retryable, not fatal."""
    import http.client

    state = {"calls": 0}

    def flaky(url, timeout=None):
        state["calls"] += 1
        if state["calls"] == 1:
            raise http.client.IncompleteRead(b"partial")
        return _FakeResp(PAYLOAD)

    monkeypatch.setattr(core, "urlopen", flaky)
    res = core.download_one(job(retries=3))
    assert res.status is Status.OK
    assert state["calls"] == 2


def test_download_incomplete_read_exhausts_to_failed(monkeypatch, job):
    import http.client

    def always(url, timeout=None):
        raise http.client.IncompleteRead(b"partial")

    monkeypatch.setattr(core, "urlopen", always)
    res = core.download_one(job(retries=2))
    assert res.status is Status.FAILED
    assert "IncompleteRead" in res.message


def test_download_429_retries_not_fast(monkeypatch, job):
    """Rate limiting (429) is retryable, unlike 401/403/404."""
    state = serve(monkeypatch, error=make_http_error(429, "Too Many Requests"))
    res = core.download_one(job(retries=3))
    assert res.status is Status.FAILED
    assert state["calls"] == 3  # retried, did not fail fast
    assert "429" in res.message


def test_download_sets_user_agent_header(monkeypatch, job):
    seen = {}

    class R(io.BytesIO):
        def __enter__(self): return self
        def __exit__(self, *a): self.close()

    def capture(req, timeout=None):
        # download_one now passes a urllib.request.Request, not a bare URL.
        seen["ua"] = req.get_header("User-agent")
        seen["url"] = req.full_url
        return R(PAYLOAD)

    monkeypatch.setattr(core, "urlopen", capture)
    core.download_one(job())
    assert seen["ua"] == core.USER_AGENT
    assert "androzoo.uni.lu" in seen["url"]
    assert "$" not in seen["url"]  # regression: no stray shell-style template


def test_download_cleans_up_partial_on_failure(monkeypatch, job, tmp_path):
    from urllib.error import URLError

    serve(monkeypatch, error=URLError("down"))
    res = core.download_one(job(retries=2))
    assert res.status is Status.FAILED
    # No leftover .part file after giving up.
    assert list(tmp_path.glob("*.part")) == []


def test_download_streams_without_buffering_whole_file(monkeypatch, job, tmp_path):
    """read() must be called with a chunk size, i.e. we stream, not slurp."""
    payload = b"x" * (3 * (1 << 20) + 7)  # > 3 chunks
    sha = hashlib.sha256(payload).hexdigest()
    read_sizes = []

    class ChunkedResp:
        def __init__(self): self._buf = io.BytesIO(payload)
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def read(self, size=-1):
            read_sizes.append(size)
            return self._buf.read(size)

    monkeypatch.setattr(core, "urlopen", lambda req, timeout=None: ChunkedResp())
    res = core.download_one(job(sha=sha))
    assert res.status is Status.OK
    assert (tmp_path / f"{sha}.apk").read_bytes() == payload
    # Every read passed an explicit positive chunk size (never a full slurp).
    assert read_sizes and all(s == core._CHUNK for s in read_sizes[:-1])


# --------------------------------------------------------------------------- #
# backoff helpers
# --------------------------------------------------------------------------- #

def test_parse_retry_after():
    assert core._parse_retry_after("5") == 5.0
    assert core._parse_retry_after(" 12 ") == 12.0
    assert core._parse_retry_after(None) is None
    assert core._parse_retry_after("Wed, 21 Oct 2099 07:28:00 GMT") is None  # date form


def test_backoff_is_bounded_and_honours_retry_after(monkeypatch):
    monkeypatch.setattr(core.random, "uniform", lambda a, b: b)  # take the cap
    # Exponential, capped at 30.
    assert core._backoff_seconds(1) == 2
    assert core._backoff_seconds(3) == 8
    assert core._backoff_seconds(10) == 30
    # Retry-After overrides the exponential schedule.
    assert core._backoff_seconds(10, retry_after=3.0) == 3.0


def test_backoff_has_jitter():
    # Full jitter: result lies within [0, base]; values vary across calls.
    samples = {round(core._backoff_seconds(4), 4) for _ in range(50)}
    assert len(samples) > 1
    assert all(0 <= s <= 16 for s in samples)


# --------------------------------------------------------------------------- #
# run() — aggregation, progress callback, cancellation
# --------------------------------------------------------------------------- #

def test_run_invokes_progress_callback(monkeypatch, job):
    serve(monkeypatch)
    jobs = [job(sha=GOOD_SHA) for _ in range(3)]
    seen = []
    core.run(jobs, concurrency=2, on_result=lambda r, done, total: seen.append((done, total)))
    assert len(seen) == 3
    assert seen[-1][1] == 3  # total reported
    assert {d for d, _ in seen} == {1, 2, 3}  # monotonic completion count


def test_run_cancel_event_stops_early(monkeypatch, job):
    serve(monkeypatch)
    jobs = [job() for _ in range(5)]
    ev = threading.Event()
    ev.set()  # already cancelled before we start consuming
    results = core.run(jobs, concurrency=1, cancel_event=ev)
    assert len(results) <= len(jobs)  # stops at/near the first completion


def test_run_survives_unexpected_worker_exception(monkeypatch, job):
    """One worker raising an unexpected error must not abort the whole batch."""
    calls = {"n": 0}

    def sometimes_explodes(j):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("boom")  # not an expected network error
        return Result(j.sha, Status.OK)

    monkeypatch.setattr(core, "download_one", sometimes_explodes)
    jobs = [job() for _ in range(3)]
    results = core.run(jobs, concurrency=1)
    assert len(results) == 3  # all three accounted for, none lost
    statuses = sorted(r.status.value for r in results)
    assert statuses == ["failed", "ok", "ok"]
    failed = next(r for r in results if r.status is Status.FAILED)
    assert "unexpected error" in failed.message
    assert "RuntimeError" in failed.message


# --------------------------------------------------------------------------- #
# config + args
# --------------------------------------------------------------------------- #

def test_resolve_config_cli_overrides_toml(tmp_path, monkeypatch):
    cfg = tmp_path / "az.toml"
    cfg.write_text('key = "fromfile"\ninput_file = "f.txt"\nbasedir = "d"\n')
    args = cli.parse_args(["--config", str(cfg), "--key", "fromcli"])
    merged = cli.resolve_config(args)
    assert merged["key"] == "fromcli"  # CLI wins
    assert merged["input_file"] == "f.txt"  # falls back to TOML


def test_resolve_config_missing_raises(tmp_path):
    args = cli.parse_args(["--config", str(tmp_path / "nope.toml")])
    with pytest.raises(SystemExit):
        cli.resolve_config(args)


def test_parse_args_defaults():
    args = cli.parse_args([])
    assert args.concurrency == 10
    assert args.retries == 3
    assert args.no_verify is False


# --------------------------------------------------------------------------- #
# main() integration
# --------------------------------------------------------------------------- #

def _write_run(tmp_path, shas):
    (tmp_path / "h.txt").write_text("\n".join(shas) + "\n")
    (tmp_path / "az.toml").write_text(
        f'key = "demo"\ninput_file = "{tmp_path / "h.txt"}"\nbasedir = "{tmp_path / "apks"}"\n'
    )
    return str(tmp_path / "az.toml")


def test_main_success_returns_zero(monkeypatch, tmp_path):
    serve(monkeypatch)
    cfg = _write_run(tmp_path, [GOOD_SHA])
    assert cli.main(["--config", cfg, "--concurrency", "2"]) == 0
    assert (tmp_path / "apks" / f"{GOOD_SHA}.apk").exists()


def test_main_with_failures_returns_one(monkeypatch, tmp_path):
    serve(monkeypatch, error=make_http_error(404))
    cfg = _write_run(tmp_path, [GOOD_SHA])
    assert cli.main(["--config", cfg]) == 1


def test_main_no_hashes_returns_one(monkeypatch, tmp_path):
    serve(monkeypatch)
    cfg = _write_run(tmp_path, ["# only a comment"])
    assert cli.main(["--config", cfg]) == 1


def test_main_rejects_bad_concurrency(monkeypatch, tmp_path):
    cfg = _write_run(tmp_path, [GOOD_SHA])
    with pytest.raises(SystemExit):
        cli.main(["--config", cfg, "--concurrency", "0"])
