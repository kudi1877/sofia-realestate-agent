"""Observability: track pipeline runs + push status to the dashboard.

Two artifacts are written into the dashboard repo's data/dashboard/ dir:

  status.json — short, frequently-overwritten file with the agent's current
                state. Polled by the dashboard's StatusBadge every 20s.
                Pushed at pipeline start (state="running") and at the end
                (state="idle" or "error", with last-run summary). NOT pushed
                per source — that would create too many Vercel rebuilds.

  runs.json   — append-only history (capped to N most recent entries) of
                completed pipeline runs. Each entry holds the per-source
                breakdown, timing, anomaly count, digest summary, and any
                errors. Powers the /runs page.

A separate `RunRecorder` class is used by cmd_full / cmd_scrape to collect
data during a run; .finalize() spits out the dict that ends up in runs.json.
"""

from __future__ import annotations

import json
import os
import threading
import time
from dataclasses import dataclass, field, asdict
from typing import Any, Dict, List, Optional

from loguru import logger

from src.config import DASHBOARD_REPO_PATH, DASHBOARD_DATA_DIR, DASHBOARD_AUTO_PUSH
from src.utils.git import commit_and_push
from src.utils.time import utc_now, utc_now_iso_z


# ── Settings ──────────────────────────────────────────────────────────────────

# Keep the most recent N run records in runs.json. ~1KB each → 30 KB at 30 days.
MAX_RUN_HISTORY = 30


# ── RunRecorder ───────────────────────────────────────────────────────────────


@dataclass
class SourceResult:
    """Per-source scrape outcome."""
    name: str
    scraped: int = 0
    duration_sec: float = 0.0
    status: str = "ok"  # ok | error | empty | partial
    error: Optional[str] = None


@dataclass
class RunRecorder:
    """Collects timing + outcomes from one pipeline run.

    Used like:
        rec = RunRecorder()
        rec.start()
        with rec.step("scrape"):
            ...
            rec.add_source(SourceResult(name="imot.bg", scraped=2260, ...))
        with rec.step("analyze"):
            ...
        rec.set_analysis(anomalies=343, neighborhoods=130)
        rec.set_digest(sent=1, qualified=18, top_deals=[...])
        rec.finalize(active_after=6678)
        # → rec.to_dict() ready to write into runs.json
    """
    id: str = field(default_factory=lambda: f"run_{utc_now().strftime('%Y-%m-%dT%H%M%SZ')}")
    started_at: Optional[str] = None
    finished_at: Optional[str] = None
    duration_sec: float = 0.0

    sources: List[SourceResult] = field(default_factory=list)
    totals: Dict[str, int] = field(default_factory=dict)
    analysis: Dict[str, Any] = field(default_factory=dict)
    availability: Dict[str, int] = field(default_factory=dict)
    data_health: Dict[str, Any] = field(default_factory=dict)
    digest: Dict[str, Any] = field(default_factory=dict)
    errors: List[str] = field(default_factory=list)

    status: str = "running"  # running | ok | partial | error

    _start_ts: float = field(default=0.0, repr=False)
    _step_start: Dict[str, float] = field(default_factory=dict, repr=False)
    _off_market: int = field(default=0, repr=False)
    _newly_off_market: int = field(default=0, repr=False)

    # ── lifecycle ─────────────────────────────────────────────────────────────

    def start(self) -> None:
        self._start_ts = time.time()
        self.started_at = _utc_now_iso()

    def step(self, name: str) -> "_StepCtx":
        return _StepCtx(self, name)

    def add_source(self, result: SourceResult) -> None:
        self.sources.append(result)

    def set_analysis(self, *, anomalies: int, neighborhoods: int, groups_used: int = 0) -> None:
        self.analysis = {
            "anomalies": anomalies,
            "neighborhoods_with_stats": neighborhoods,
            "groups_used": groups_used,
        }

    def set_availability(self, *, pinged: int, live: int, gone: int, unknown: int) -> None:
        self.availability = {
            "pinged": pinged,
            "live": live,
            "gone": gone,
            "unknown": unknown,
        }

    def set_data_health(self, data_health: Dict[str, Any]) -> None:
        self.data_health = data_health

    def set_digest(
        self,
        *,
        sent: int,
        qualified: int,
        considered: int = 0,
        top_deals: Optional[List[Dict[str, Any]]] = None,
    ) -> None:
        self.digest = {
            "sent": sent,
            "qualified": qualified,
            "considered": considered,
            "top_deals": top_deals or [],
        }

    def add_error(self, msg: str) -> None:
        self.errors.append(msg)
        if self.status not in ("error",):
            self.status = "partial"

    def set_off_market(self, *, total: int, newly_marked: int = 0) -> None:
        self._off_market = total
        self._newly_off_market = newly_marked

    def finalize(self, *, active_after: int, scraped_total: Optional[int] = None) -> None:
        self.finished_at = _utc_now_iso()
        self.duration_sec = round(time.time() - self._start_ts, 1)
        self.totals = {
            "scraped_total": scraped_total
            if scraped_total is not None
            else sum(s.scraped for s in self.sources),
            "active_after": active_after,
            "off_market": self._off_market,
            "newly_off_market": self._newly_off_market,
        }
        # Decide overall status
        if self.errors and not self.sources:
            self.status = "error"
        elif self.status == "running":
            # No explicit failure → ok unless any source failed hard
            failed = [s for s in self.sources if s.status == "error"]
            self.status = "partial" if failed else "ok"

    # ── serialization ─────────────────────────────────────────────────────────

    def to_dict(self) -> Dict[str, Any]:
        d = {
            "id": self.id,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "duration_sec": self.duration_sec,
            "status": self.status,
            "sources": [asdict(s) for s in self.sources],
            "totals": self.totals,
            "analysis": self.analysis,
            "availability": self.availability,
            "data_health": self.data_health,
            "digest": self.digest,
            "errors": self.errors,
        }
        return d


STEP_HEARTBEAT_SECONDS = int(os.getenv("STEP_HEARTBEAT_SECONDS", "600"))


class _StepCtx:
    """Context manager that records duration of one named pipeline step.

    TIN-517: also runs a daemon heartbeat that WARNS every 10 minutes while
    the step is still executing. The 2026-07-13 and 2026-07-16 incidents
    both hung silently inside a step for hours/days with zero log output —
    a slow or stuck step must announce itself, not disappear.
    """
    def __init__(self, recorder: RunRecorder, name: str):
        self.recorder = recorder
        self.name = name
        self._start: float = 0.0
        self._stop_heartbeat: threading.Event | None = None

    def __enter__(self):
        self._start = time.time()
        self.recorder._step_start[self.name] = self._start
        self._stop_heartbeat = threading.Event()
        stop = self._stop_heartbeat
        name = self.name
        start = self._start

        def _heartbeat():
            while not stop.wait(STEP_HEARTBEAT_SECONDS):
                minutes = (time.time() - start) / 60
                logger.warning(
                    f"Pipeline step '{name}' still running after {minutes:.0f} minutes"
                )

        threading.Thread(target=_heartbeat, daemon=True, name=f"heartbeat-{name}").start()
        return self

    def __exit__(self, exc_type, exc, tb):
        # Don't swallow exceptions — let them bubble up. We just record duration.
        # If exception happened, the recorder caller can call add_error() too.
        if self._stop_heartbeat is not None:
            self._stop_heartbeat.set()
        return False


def _utc_now_iso() -> str:
    return utc_now_iso_z(timespec="seconds")


# ── File writers (status.json, runs.json) + git push ──────────────────────────


def write_status(
    state: str,
    *,
    summary: Optional[Dict[str, Any]] = None,
    push: Optional[bool] = None,
) -> bool:
    """Write data/dashboard/status.json in the dashboard repo. Optionally git-push.

    `state` is "running" | "idle" | "error". `summary` is optional last-run
    data (counts, duration, etc.) — typically only set when state="idle".

    Returns True if the file was written (and pushed if push=True).
    """
    if push is None:
        push = DASHBOARD_AUTO_PUSH

    data_dir = DASHBOARD_DATA_DIR
    if not data_dir.parent.exists():
        logger.warning(f"Dashboard data dir not found at {data_dir.parent}; skipping status update")
        return False

    payload = {
        "state": state,
        "updated_at": _utc_now_iso(),
        "summary": summary or {},
    }

    target = data_dir / "status.json"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    if push:
        return commit_and_push(
            DASHBOARD_REPO_PATH,
            files=["data/dashboard/status.json"],
            message=f"status: {state} ({_utc_now_iso()})",
        )
    return True


def append_run(record: Dict[str, Any], *, push: Optional[bool] = None) -> bool:
    """Append a finalized run record to data/dashboard/runs.json (capped to N entries).

    Pushed alongside data.json + status.json by the export step (caller can
    chain via push=False here and let export_dashboard's commit grab it).
    """
    if push is None:
        push = DASHBOARD_AUTO_PUSH

    data_dir = DASHBOARD_DATA_DIR
    if not data_dir.parent.exists():
        logger.warning(f"Dashboard data dir not found at {data_dir.parent}; skipping runs append")
        return False

    runs_file = data_dir / "runs.json"
    runs_file.parent.mkdir(parents=True, exist_ok=True)

    history: List[Dict[str, Any]] = []
    if runs_file.exists():
        try:
            history = json.loads(runs_file.read_text(encoding="utf-8")).get("runs", [])
        except Exception as e:
            logger.warning(f"Could not parse existing runs.json, starting fresh: {e}")
            history = []

    # Newest first; cap to MAX_RUN_HISTORY
    history.insert(0, record)
    history = history[:MAX_RUN_HISTORY]

    runs_file.write_text(
        json.dumps({"runs": history, "updated_at": _utc_now_iso()}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    if push:
        return commit_and_push(
            DASHBOARD_REPO_PATH,
            files=["data/dashboard/runs.json"],
            message=f"runs: append {record.get('id', 'unknown')}",
        )
    return True
