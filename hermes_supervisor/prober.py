"""Liveness prober: the state machine's ``probed`` step (false-positive guard before escalate).

The spec's ladder puts a cheap liveness ping here. We probe *deterministically* instead of
with an LLM: hermes-agent exposes no API to inject a prompt into a live ACP worker session,
and a deterministic check honours the operator's "максимально детерминированный код / экономия
токенов" directive (comment 17.07) - the LLM stays out of the loop entirely. The probe claims
the worker is alive ONLY on positive evidence of local CPU progress; a gone, idle, remote, or
unparseable worker returns ``alive=False`` so an unverifiable stall escalates to a human rather
than staying silent (same philosophy the core uses when no prober is configured). Swap in an
LLM-ping ``Prober`` later without touching the core - it only needs the ``__call__`` shape.
"""

import socket
import time

import psutil

from session_supervisor.snapshots import ProbeResult, RunSnapshot


def _pid_from_worker_id(worker_id: str) -> int | None:
    """Worker ids are ``host:pid`` (claim_lock) or ``pid:NNN``; pull the trailing int."""
    if not worker_id or ":" not in worker_id:
        return None
    tail = worker_id.rsplit(":", 1)[1]
    try:
        return int(tail)
    except ValueError:
        return None


def _host_from_worker_id(worker_id: str) -> str | None:
    if not worker_id or ":" not in worker_id:
        return None
    head = worker_id.rsplit(":", 1)[0]
    return head or None


class ProcessLivenessProber:
    """Alive iff the worker process is local, present, and burning CPU over a sample window.

    ``sample_seconds`` is the CPU sampling window (blocking); ``active_cpu_pct`` is the summed
    process+children busy threshold below which the worker is treated as hung.
    """

    def __init__(self, sample_seconds: float = 1.0, active_cpu_pct: float = 5.0):
        self.sample_seconds = sample_seconds
        self.active_cpu_pct = active_cpu_pct
        self._local_host = socket.gethostname()

    def __call__(self, run: RunSnapshot, anomaly_type: str) -> ProbeResult:
        host = _host_from_worker_id(run.worker_id)
        if host is not None and host != self._local_host:
            return ProbeResult(alive=False, detail=f"remote_host_unprobed:{host}")
        pid = _pid_from_worker_id(run.worker_id)
        if pid is None:
            return ProbeResult(alive=False, detail="no_pid")
        try:
            proc = psutil.Process(pid)
            if proc.status() == psutil.STATUS_ZOMBIE:
                return ProbeResult(alive=False, detail="zombie")
            busy = self._sample_cpu(proc)
        except psutil.NoSuchProcess:
            return ProbeResult(alive=False, detail="process_gone")
        except (psutil.AccessDenied, psutil.Error) as exc:
            return ProbeResult(alive=False, detail=f"probe_error:{type(exc).__name__}")
        if busy >= self.active_cpu_pct:
            return ProbeResult(alive=True, detail=f"cpu_active:{busy:.1f}%")
        return ProbeResult(alive=False, detail=f"no_cpu_progress:{busy:.1f}%")

    def _sample_cpu(self, proc: psutil.Process) -> float:
        """Summed CPU% of the worker and its children over the sample window."""
        procs = [proc]
        try:
            procs.extend(proc.children(recursive=True))
        except psutil.Error:
            pass
        for p in procs:
            try:
                p.cpu_percent(None)  # prime; first call always returns 0.0
            except psutil.Error:
                pass
        time.sleep(self.sample_seconds)
        total = 0.0
        for p in procs:
            try:
                total += p.cpu_percent(None)
            except psutil.Error:
                continue
        return total
