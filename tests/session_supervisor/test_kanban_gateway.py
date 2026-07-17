"""HermesSendNotifier: exit-code contract of the `hermes send` subprocess adapter."""

import sys

import pytest

from session_supervisor.kanban_gateway import HermesSendNotifier, PushDeliveryError

OK_ARGV = (sys.executable, "-c", "import sys; sys.exit(0)")
FAIL_ARGV = (sys.executable, "-c", "import sys; sys.stderr.write('boom'); sys.exit(1)")


def test_send_zero_exit_is_success():
    HermesSendNotifier(argv_prefix=OK_ARGV).send("text")


def test_send_nonzero_exit_raises_with_stderr():
    with pytest.raises(PushDeliveryError, match="rc=1.*boom"):
        HermesSendNotifier(argv_prefix=FAIL_ARGV).send("text")
