"""One supervision pass per dispatcher tick: snapshots -> core -> triage card + push.

``SupervisorService`` owns the ``Supervisor`` (deterministic rule engine + incident state) and
the ``EscalationRouter`` (persistent outbox -> real kanban card + ``hermes send`` push). The
gateway's embedded dispatcher calls :meth:`run_tick` once per tick with the health figures it
already computes (ready-queue non-empty? spawned anything? free slots?). Every tick is wrapped
so a supervision failure logs and returns rather than wedging dispatch.
"""

import logging
import os
import sqlite3
from pathlib import Path

from session_supervisor import Supervisor, SupervisorConfig
from session_supervisor.escalation import EscalationRouter
from session_supervisor.kanban_gateway import HermesSendNotifier, KanbanTaskGateway

from .prober import ProcessLivenessProber
from .sources import build_dispatcher_snapshot, build_run_snapshots, count_running


def _default_zeus_db_path() -> Path:
    home = Path(os.environ.get("HERMES_HOME") or Path.home() / ".hermes")
    return home / "zeus" / "zeus.db"


class SupervisorService:
    def __init__(
        self,
        board: str,
        *,
        state_dir: str,
        board_meta: dict | None = None,
        hermes_repo: str | None = None,
        send_argv=("hermes", "send"),
        zeus_db_path: str | None = None,
        db_path: str | None = None,
        prober=None,
        notifier=None,
        tasks=None,
        badge: str | None = None,
        budget_lookup=None,
        logger: logging.Logger | None = None,
    ):
        self._board = board
        self._log = logger or logging.getLogger(__name__)
        self._zeus_db_path = Path(zeus_db_path) if zeus_db_path else _default_zeus_db_path()
        self._db_path = db_path
        self._budget_lookup = budget_lookup
        meta = board_meta if board_meta is not None else self._read_board_meta(hermes_repo)
        self._agent_limit = meta.get("agent_limit")
        config = SupervisorConfig.from_dict(meta.get("supervisor", {}))
        state = Path(state_dir)
        state.mkdir(parents=True, exist_ok=True)
        self.supervisor = Supervisor(
            config=config,
            prober=prober if prober is not None else ProcessLivenessProber(),
            state_path=str(state / "supervisor-state.json"),
            decision_log_path=str(state / "supervisor-decisions.jsonl"),
        )
        self.router = EscalationRouter(
            tasks=tasks
            if tasks is not None
            else KanbanTaskGateway(board=board, db_path=db_path, hermes_repo=hermes_repo),
            notifier=notifier if notifier is not None else HermesSendNotifier(send_argv),
            state_path=str(state / "escalation-state.json"),
            badge=badge if badge is not None else meta.get("icon", ""),
        )

    def _read_board_meta(self, hermes_repo: str | None) -> dict:
        try:
            kdb = _load_kanban_db(hermes_repo)
            return kdb.read_board_metadata(self._board) or {}
        except Exception as exc:  # board.json missing / repo not importable
            self._log.debug("supervisor: board metadata unavailable (%s)", exc)
            return {}

    def run_tick(
        self,
        now: float,
        *,
        ready_nonempty: bool,
        spawned: bool,
        free_slots: int | None = None,
    ) -> dict:
        """Observe one tick and deliver any incidents. Never raises into the caller."""
        kb_conn = None
        zeus_conn = None
        try:
            kb_conn = self._connect_board()
            zeus_conn = self._connect_zeus()
            runs = build_run_snapshots(
                kb_conn, self._board, zeus_conn, now, budget_lookup=self._budget_lookup
            )
            slots = free_slots if free_slots is not None else self._free_slots(kb_conn)
            disp = build_dispatcher_snapshot(
                self._board,
                ready_queue_size=1 if ready_nonempty else 0,
                spawns_last_tick=1 if spawned else 0,
                free_slots=slots,
            )
            events = self.supervisor.tick(now, runs, disp)
            self.router.route(now, events)
            return {
                "runs": len(runs),
                "events": len(events),
                "open_incidents": len(self.supervisor.open_incidents),
                "outbox": self.router.stats(),
            }
        except Exception:
            self._log.exception("supervisor: tick failed on board %s", self._board)
            return {"error": True}
        finally:
            _close(kb_conn)
            _close(zeus_conn)

    def _free_slots(self, kb_conn: sqlite3.Connection) -> int:
        if self._agent_limit is None:
            return 1  # no cap configured -> treat a stalled queue as escalatable
        return int(self._agent_limit) - count_running(kb_conn)

    def _connect_board(self) -> sqlite3.Connection:
        kdb = _load_kanban_db(None)
        if self._db_path is not None:
            return kdb.connect(Path(self._db_path))
        return kdb.connect(board=self._board)

    def _connect_zeus(self) -> sqlite3.Connection | None:
        if not self._zeus_db_path.exists():
            return None
        try:
            conn = sqlite3.connect(str(self._zeus_db_path), timeout=10.0)
            conn.row_factory = sqlite3.Row
            return conn
        except sqlite3.Error as exc:
            self._log.debug("supervisor: zeus ledger unavailable (%s)", exc)
            return None


def _load_kanban_db(hermes_repo: str | None):
    import sys

    if hermes_repo:
        repo = str(Path(hermes_repo).expanduser())
        if repo not in sys.path:
            sys.path.insert(0, repo)
    from hermes_cli import kanban_db

    return kanban_db


def _close(conn) -> None:
    if conn is not None:
        try:
            conn.close()
        except Exception:
            pass
