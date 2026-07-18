from __future__ import annotations

from zeus_watchdog.checks import Probe, evaluate
from zeus_watchdog.config import Config


def _cfg(tmp_path) -> Config:
    from zeus_watchdog.config import load

    return load(home=tmp_path)


def _probe(**kw) -> Probe:
    base = dict(
        now=1000.0,
        gateway_pid=123,
        gateway_alive=True,
        log_age_sec=10.0,
        ready=0,
        run=0,
        stale_heartbeats=[],
        corrupt_dbs=[],
        pool_recovered_after_limit=False,
    )
    base.update(kw)
    return Probe(**base)


def keys(conditions):
    return {c.key for c in conditions}


def test_all_quiet(tmp_path):
    assert evaluate(_probe(), _cfg(tmp_path)) == []


def test_gateway_dead_suppresses_dispatcher_stale(tmp_path):
    p = _probe(gateway_alive=False, gateway_pid=None, log_age_sec=99999)
    ks = keys(evaluate(p, _cfg(tmp_path)))
    assert "gateway_dead" in ks
    assert "dispatcher_stale" not in ks  # dead gateway can't tick; not double-reported


def test_dispatcher_stale_when_log_old(tmp_path):
    p = _probe(log_age_sec=700)  # > 600 default
    assert "dispatcher_stale" in keys(evaluate(p, _cfg(tmp_path)))


def test_dispatcher_ok_when_log_fresh(tmp_path):
    assert evaluate(_probe(log_age_sec=60), _cfg(tmp_path)) == []


def test_ready_no_run_condition_and_sustain(tmp_path):
    conds = evaluate(_probe(ready=3, run=0), _cfg(tmp_path))
    assert len(conds) == 1
    c = conds[0]
    assert c.key == "ready_no_run"
    assert c.sustain_sec == 600
    assert "READY 3" in c.summary


def test_ready_with_run_is_quiet(tmp_path):
    assert evaluate(_probe(ready=3, run=1), _cfg(tmp_path)) == []


def test_resume_stuck_supersedes_ready_no_run(tmp_path):
    p = _probe(ready=2, run=0, pool_recovered_after_limit=True)
    ks = keys(evaluate(p, _cfg(tmp_path)))
    assert ks == {"resume_stuck"}


def test_heartbeat_stale_per_task(tmp_path):
    p = _probe(stale_heartbeats=[("t_1", 2000.0), ("t_2", 3000.0)])
    ks = keys(evaluate(p, _cfg(tmp_path)))
    assert ks == {"heartbeat_stale:t_1", "heartbeat_stale:t_2"}


def test_db_corrupt_per_db(tmp_path):
    ks = keys(evaluate(_probe(corrupt_dbs=["kanban", "zeus"]), _cfg(tmp_path)))
    assert ks == {"db_corrupt:kanban", "db_corrupt:zeus"}
