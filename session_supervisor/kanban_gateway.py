"""Adapters binding the escalation ports to hermes-agent.

KanbanTaskGateway drives the real kanban board through ``hermes_cli.kanban_db``
(an incident card is a kanban task in ``triage``). HermesSendNotifier shells out
to ``hermes send`` - the existing operator push channel (Telegram et al.), which
reuses the gateway's already-configured credentials.
"""

import subprocess
import sys
from pathlib import Path


class PushDeliveryError(RuntimeError):
    pass


def _load_kanban_db(hermes_repo: str | None):
    if hermes_repo:
        repo = str(Path(hermes_repo).expanduser())
        if repo not in sys.path:
            sys.path.insert(0, repo)
    from hermes_cli import kanban_db

    return kanban_db


class KanbanTaskGateway:
    """Task port over the hermes kanban DB; each op uses a short-lived connection."""

    def __init__(
        self,
        board: str,
        db_path: str | None = None,
        author: str = "supervisor",
        hermes_repo: str | None = None,
    ):
        self._kdb = _load_kanban_db(hermes_repo)
        self._board = board
        self._db_path = Path(db_path) if db_path else None
        self._author = author

    def _connect(self):
        if self._db_path is not None:
            return self._kdb.connect(self._db_path)
        return self._kdb.connect(board=self._board)

    def create_incident(self, title: str, body: str, idempotency_key: str) -> str:
        conn = self._connect()
        try:
            return self._kdb.create_task(
                conn,
                title=title,
                body=body,
                created_by=self._author,
                triage=True,
                idempotency_key=idempotency_key,
                board=self._board,
            )
        finally:
            conn.close()

    def add_comment(self, task_id: str, body: str) -> None:
        conn = self._connect()
        try:
            self._kdb.add_comment(conn, task_id, self._author, body)
        finally:
            conn.close()

    def reopen_incident(self, task_id: str, comment: str) -> None:
        # No public unarchive API in kanban_db; flipping the archived card back to
        # triage directly is the reopen path the contract (§4) requires.
        conn = self._connect()
        try:
            self._kdb.add_comment(conn, task_id, self._author, comment)
            with self._kdb.write_txn(conn):
                conn.execute(
                    "UPDATE tasks SET status = 'triage' "
                    "WHERE id = ? AND status = 'archived'",
                    (task_id,),
                )
        finally:
            conn.close()

    def resolve_incident(self, task_id: str, comment: str) -> None:
        conn = self._connect()
        try:
            self._kdb.add_comment(conn, task_id, self._author, comment)
            # False means the card is already archived - resolve stays idempotent.
            self._kdb.archive_task(conn, task_id)
        finally:
            conn.close()


class HermesSendNotifier:
    """Push port over ``hermes send``; the message text is appended to argv_prefix."""

    def __init__(self, argv_prefix=("hermes", "send"), timeout_s: float = 30.0):
        self._argv_prefix = tuple(argv_prefix)
        self._timeout_s = timeout_s

    def send(self, text: str) -> None:
        proc = subprocess.run(
            [*self._argv_prefix, text],
            capture_output=True,
            text=True,
            timeout=self._timeout_s,
        )
        if proc.returncode != 0:
            detail = (proc.stderr or proc.stdout).strip()[:300]
            raise PushDeliveryError(f"hermes send rc={proc.returncode}: {detail}")
