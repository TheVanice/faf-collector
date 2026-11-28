"""Batch-POST spooled events to the ingest endpoint.

Nothing is ever dropped. Events the backend rejects, and batches it refuses
with a 4xx, are moved to .faf/local/rejected.jsonl with the reason;
`python faf_post.py --requeue-rejected` puts them back into the spool.
Connection errors, 3xx, 5xx, 401/403/408/429 stop the flush and keep the batch,
as does a 2xx answer that is not the ingest answer (accepted plus rejected
must account for every event of the batch).
Redirects are never followed: the key must not reach another address.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path

import faf_log
import faf_settings
from faf_event import Spool, _read_jsonl, local_dir, repo_root, utcnow_iso

MAX_BATCH = 500
MAX_BATCH_BYTES = 4_000_000
STOP_STATUSES = {401, 403, 408, 429}
STATUS_TEXT = {401: "key not accepted", 403: "forbidden", 408: "timeout", 429: "rate limited",
               503: "unavailable"}
REJECTED_FILE = ".faf/local/rejected.jsonl"
log = faf_log.LOGGER


def rejected_path(root):
    return local_dir(root, create=False) / "rejected.jsonl"


def settings(root=None):
    """(endpoint, key, problem); problem is None when sending is possible. root is unused:
    the key and endpoint are per user, never per repo."""
    return faf_settings.resolve()


def is_configured(root=None):
    return settings(root)[2] is None


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Return a 3xx as the answer. urllib would repeat the POST as a GET with the key to the new address."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


_OPENER = urllib.request.build_opener(_NoRedirect)


def post_batch(events, endpoint, key, timeout=20):
    """POST one batch. Returns (status, body); body is parsed JSON or text."""
    body = json.dumps(events).encode("utf-8")
    req = urllib.request.Request(  # noqa: S310
        endpoint, data=body,
        headers={
            "Content-Type": "application/json",
            "x-api-key": key or "",
            "User-Agent": f"faf-collector/{faf_settings.COLLECTOR_VERSION}",
            "x-faf-collector": faf_settings.COLLECTOR_VERSION,
        },
        method="POST",
    )
    try:
        with _OPENER.open(req, timeout=timeout) as resp:  # noqa: S310 - faf_settings allows https only
            text = resp.read().decode("utf-8", "replace")
            status = resp.status
    except urllib.error.HTTPError as exc:
        with exc:
            return exc.code, exc.read().decode("utf-8", "replace")[:500]
    try:
        return status, json.loads(text or "{}")
    except ValueError:
        return status, text[:500]


@dataclass
class FlushResult:
    sent: int = 0
    rejected: int = 0
    error: str | None = None  # short reason, safe to log (no response body)
    status: int | None = None  # last HTTP status
    duration: float = 0.0
    uploaded_files: int = 0  # instruction files sent with content
    queued: int = 0          # events still waiting after a failure

    def __bool__(self):
        return self.error is None


def _chunks(events):
    """Yield (chunk, too_large). Chunks hold at most MAX_BATCH events and MAX_BATCH_BYTES."""
    chunk, size = [], 2
    for event in events:
        n = len(json.dumps(event).encode("utf-8")) + 1
        if n + 2 > MAX_BATCH_BYTES:
            if chunk:
                yield chunk, False
                chunk, size = [], 2
            yield [event], True
            continue
        if chunk and (len(chunk) >= MAX_BATCH or size + n > MAX_BATCH_BYTES):
            yield chunk, False
            chunk, size = [], 2
        chunk.append(event)
        size += n
    if chunk:
        yield chunk, False


def _count(value):
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else None


def _rejections(body, count):
    """[(index, reason)] from an ingest answer, or None when body is not one that accounts
    for every event (accepted plus rejected must be count)."""
    if not isinstance(body, dict):
        return None
    accepted, rejected = _count(body.get("accepted")), body.get("rejected", [])
    if accepted is None:
        return None
    if isinstance(rejected, list):
        entries = [(r["index"], str(r.get("error") or "rejected")) for r in rejected
                   if isinstance(r, dict) and _count(r.get("index")) is not None and r["index"] < count]
        if len(entries) != len(rejected) or len({i for i, _ in entries}) != len(entries):
            return None
        rejected = len(entries)
    elif _count(rejected) is not None and rejected:
        # Older backend: no per-event details, so keep the whole chunk.
        reason = f"backend rejected {rejected} of {count} events without details"
        entries = [(i, reason) for i in range(count)]
    elif _count(rejected) is not None:
        entries = []
    else:
        return None
    return entries if accepted + rejected == count else None


def _status_error(status):
    if 300 <= status < 400:
        return f"{status} redirect refused"
    return f"{status} {STATUS_TEXT.get(status) or ('server error' if status >= 500 else 'refused')}"


def _send(chunk, endpoint, key, retries, timeout):
    """Return (status, rejections, error). error is set when flushing must stop."""
    delay, status, error = 1.0, None, None
    for attempt in range(retries + 1):
        try:
            status, body = post_batch(chunk, endpoint, key, timeout=timeout)
        except TimeoutError:
            error = "timeout"
        except OSError:
            error = "no connection"
        else:
            if 200 <= status < 300:
                rejections = _rejections(body, len(chunk))
                if rejections is not None:
                    return status, rejections, None
                # e.g. a parked domain, a proxy page, or counts that leave events unaccounted for
                error = f"{status} not an ingest answer"
            elif 300 <= status < 400 or status in STOP_STATUSES or status >= 500:
                error = _status_error(status)
            else:
                return status, [(i, f"HTTP {status}: {body}") for i in range(len(chunk))], None
        if attempt < retries:
            time.sleep(delay)
            delay *= 2.0
    return status, [], error


def _quarantine(root, entries):
    with open(local_dir(root) / "rejected.jsonl", "a", encoding="utf-8") as fh:
        for event, reason in entries:
            fh.write(json.dumps({"ts": utcnow_iso(), "reason": reason, "event": event}) + "\n")


def _uploaded_files(events):
    return sum(sum(1 for f in e.get("files") or [] if isinstance(f, dict) and f.get("content") is not None)
               for e in events if isinstance(e, dict) and e.get("kind") == "instruction_version")


def queued_events(root):
    """Events waiting in the spool (live file and unsent batches)."""
    local = local_dir(root, create=False)
    paths = [local / "spool.jsonl", *local.glob("spool.*.sending.jsonl")] if local.is_dir() else []
    return sum(len(_read_jsonl(p) or []) for p in paths)


def flush(root=None, retries=3, timeout=20):
    """Send every waiting batch. The result is truthy when nothing is left to send."""
    root = Path(root) if root else repo_root(os.getcwd())
    result = FlushResult()
    endpoint, key, problem = settings(root)
    if problem:
        result.error = problem
        return result

    started = time.monotonic()
    spool = Spool(local_dir(root) / "spool.jsonl")
    try:
        for batch_path, events in spool.take_batches():
            for chunk, too_large in _chunks(events):
                if too_large:
                    _quarantine(root, [(chunk[0], f"event larger than {MAX_BATCH_BYTES} bytes")])
                    result.rejected += 1
                    continue
                status, rejections, error = _send(chunk, endpoint, key, retries, timeout)
                result.status = status
                if error:
                    # The batch file stays; chunks already sent are resent later, which is harmless.
                    result.error = error
                    result.queued = queued_events(root)
                    return result
                if rejections:
                    _quarantine(root, [(chunk[i], reason) for i, reason in rejections])
                bad = {i for i, _ in rejections}
                result.uploaded_files += _uploaded_files([e for i, e in enumerate(chunk) if i not in bad])
                result.sent += len(chunk) - len(rejections)
                result.rejected += len(rejections)
            try:
                os.remove(batch_path)
            except FileNotFoundError:
                pass
    finally:
        result.duration = time.monotonic() - started
    return result


def report(result, endpoint=None):
    """Log the outcome of a flush with the lines of the logging table."""
    host = faf_settings.display_host(endpoint) if endpoint else faf_settings.endpoint_host()
    if result.error == "no API key":
        log.info("not recording: no API key")
        return
    if result.error and result.status is None and result.error not in ("timeout", "no connection"):
        log.info(f"not sending: {result.error}")
        return
    if result.sent:
        log.info(f"sent {result.sent} events to {host} ({result.status}, {result.duration:.1f} s)")
    if result.uploaded_files:
        log.info(f"uploaded instructions ({result.uploaded_files} files)")
    if result.rejected:
        log.info(f"{result.rejected} events rejected, kept in {REJECTED_FILE}")
    if result.error:
        log.info(f"send failed ({result.error}), {result.queued} events queued")
    elif not result.sent and not result.rejected:
        log.info("nothing to send")


def requeue_rejected(root):
    """Move rejected events back into the spool. Returns how many were moved."""
    path = rejected_path(root)
    if not path.exists():
        return 0
    taken = path.with_name(f"rejected.{time.time_ns()}.requeue.jsonl")
    os.replace(path, taken)
    spool = Spool(local_dir(root) / "spool.jsonl")
    moved = 0
    for entry in _read_jsonl(taken) or []:
        if isinstance(entry, dict) and isinstance(entry.get("event"), dict):
            spool.append(entry["event"])
            moved += 1
    os.remove(taken)
    return moved


def main(argv=None):
    parser = argparse.ArgumentParser(prog="faf_post")
    parser.add_argument("--target", default=None, help="repo root (default: current repo)")
    parser.add_argument("--requeue-rejected", action="store_true",
                        help="move rejected events back into the spool before sending")
    args = parser.parse_args(argv)
    root = Path(args.target).resolve() if args.target else repo_root(os.getcwd())
    faf_log.setup(root, console=True, event="manual")
    if args.requeue_rejected:
        log.info(f"requeued {requeue_rejected(root)} rejected events")
    result = flush(root)
    report(result)
    return 0 if result else 1


if __name__ == "__main__":
    sys.exit(main())
