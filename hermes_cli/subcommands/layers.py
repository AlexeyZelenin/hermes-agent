"""``hermes layers`` subcommand parser — sealed-core / userland layer ops.

Handler injected (``cmd_layers``) to avoid importing ``main``.
"""

from __future__ import annotations

from typing import Callable


def build_layers_parser(subparsers, *, cmd_layers: Callable) -> None:
    """Attach the ``layers`` subcommand group to ``subparsers``."""
    layers_parser = subparsers.add_parser(
        "layers",
        help="Inspect/back up the sealed-core (code) and userland (state) layers",
        description="Manage Hermes' two state layers: sealed-core (the "
        "immutable install/code tree) and userland (HERMES_HOME state). "
        "Toggle self-modify, back up either layer, and restore after a bad "
        "update.",
    )
    actions = layers_parser.add_subparsers(dest="layers_action", required=True)

    actions.add_parser("status", help="Show layer roots and the self-modify toggle")
    actions.add_parser("unlock", help="Allow the system to modify its sealed-core")
    actions.add_parser("lock", help="Forbid the system from modifying its sealed-core")

    backup_p = actions.add_parser("backup", help="Back up a layer (default: both)")
    backup_p.add_argument(
        "--layer",
        choices=["sealed-core", "userland", "both"],
        default="both",
        help="Which layer(s) to back up (default: both)",
    )

    actions.add_parser("list", help="List existing layer backups")

    restore_p = actions.add_parser("restore", help="Restore a layer from a backup id")
    restore_p.add_argument("backup_id", help="Backup id (see `hermes layers list`)")
    restore_p.add_argument(
        "--clean",
        action="store_true",
        default=False,
        help="Snapshot restore: also remove files not present in the backup",
    )

    layers_parser.set_defaults(func=cmd_layers)
