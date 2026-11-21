"""In-process background job manager (thread pool) with per-job logs."""

from __future__ import annotations

import re
import threading
import time
import traceback
import uuid
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime

from src.config import JOBS_DIR
from src.fields import Field
from src.pipeline import JobParams, JobParamsError, JobResult, run_job

JOB_ID_RE = re.compile(r"^[0-9a-f]{12}$")
MAX_JOBS_KEPT = 50


@dataclass
class Job:
    job_id: str
    params: JobParams
    polygons: int
    status: str = "queued"  # queued | running | done | failed
    progress: float = 0.0
    created: str = field(default_factory=lambda: datetime.now().isoformat(timespec="seconds"))
    duration_s: float | None = None
    error: str | None = None
    logs: list[str] = field(default_factory=list)
    result: JobResult | None = None
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def log(self, message: str) -> None:
        stamp = datetime.now().strftime("%H:%M:%S")
        with self._lock:
            self.logs.append(f"{stamp}  {message}")

    def snapshot(self, since: int = 0) -> dict:
        with self._lock:
            logs = self.logs[since:]
            total = len(self.logs)
        data = {
            "job_id": self.job_id,
            "status": self.status,
            "progress": round(self.progress, 3),
            "created": self.created,
            "duration_s": self.duration_s,
            "error": self.error,
            "params": self.params.to_dict(),
            "polygons": self.polygons,
            "logs": logs,
            "log_cursor": total,
        }
        if self.result is not None:
            r = self.result
            data["summary"] = {
                "stats": r.stats,
                "files": r.files,
                "scenes": r.scenes,
                "overlays": r.overlays,
                "synthetic": r.synthetic,
            }
        return data


class JobManager:
    def __init__(self, workers: int = 2) -> None:
        self._pool = ThreadPoolExecutor(max_workers=workers, thread_name_prefix="rasterkit-job")
        self._jobs: "OrderedDict[str, Job]" = OrderedDict()
        self._lock = threading.Lock()

    def submit(self, params: JobParams, fields: list[Field]) -> Job:
        job = Job(job_id=uuid.uuid4().hex[:12], params=params, polygons=len(fields))
        with self._lock:
            self._jobs[job.job_id] = job
            while len(self._jobs) > MAX_JOBS_KEPT:
                self._jobs.popitem(last=False)
        job.log("Job queued.")
        self._pool.submit(self._run, job, fields)
        return job

    def get(self, job_id: str) -> Job | None:
        if not JOB_ID_RE.match(job_id):
            return None
        with self._lock:
            return self._jobs.get(job_id)

    def list(self) -> list[Job]:
        with self._lock:
            return list(reversed(self._jobs.values()))

    @staticmethod
    def job_dir(job_id: str):
        return JOBS_DIR / job_id

    def _run(self, job: Job, fields: list[Field]) -> None:
        job.status = "running"
        t0 = time.perf_counter()
        try:
            job.result = run_job(
                job.params,
                fields,
                self.job_dir(job.job_id),
                log=job.log,
                progress=lambda f: setattr(job, "progress", f),
            )
            job.progress = 1.0
            job.status = "done"
            job.log("Job finished successfully.")
        except JobParamsError as exc:
            job.error = str(exc)
            job.status = "failed"
            job.log(f"ERROR: {exc}")
        except Exception as exc:  # keep the worker alive; surface a concise message
            job.error = f"{type(exc).__name__}: {exc}"
            job.status = "failed"
            job.log(f"ERROR: {job.error}")
            job.log(traceback.format_exc(limit=3).strip().splitlines()[-1])
        finally:
            job.duration_s = round(time.perf_counter() - t0, 2)
