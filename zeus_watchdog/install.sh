#!/bin/bash
# Install / uninstall the Zeus mechanical watchdog launchd agent.
#
#   ./install.sh install     # template the plist, load it, run once
#   ./install.sh uninstall   # unload and remove the plist
#   ./install.sh status      # show launchctl state and tail the log
#
# The watchdog runs OUTSIDE the gateway so it survives a dead gateway. It is
# stdlib-only, so it keeps working even when the hermes agent packages break.
set -euo pipefail

LABEL="ai.hermes.zeus-watchdog"
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
HERMES_HOME="${HERMES_HOME:-$HOME/.hermes}"
PLIST_DST="$HOME/Library/LaunchAgents/${LABEL}.plist"
TEMPLATE="${REPO}/zeus_watchdog/watchdog.plist.template"
INTERVAL="${WATCHDOG_INTERVAL:-120}"
STDOUT="${HERMES_HOME}/logs/zeus-watchdog.log"
STDERR="${HERMES_HOME}/logs/zeus-watchdog.error.log"

# Prefer the repo venv python; fall back to python3 on PATH.
PYTHON="${REPO}/venv/bin/python"
[ -x "$PYTHON" ] || PYTHON="$(command -v python3)"

cmd="${1:-install}"

render_plist() {
    mkdir -p "$(dirname "$PLIST_DST")" "${HERMES_HOME}/logs"
    sed -e "s#__PYTHON__#${PYTHON}#g" \
        -e "s#__REPO__#${REPO}#g" \
        -e "s#__HOME__#${HERMES_HOME}#g" \
        -e "s#__INTERVAL__#${INTERVAL}#g" \
        -e "s#__STDOUT__#${STDOUT}#g" \
        -e "s#__STDERR__#${STDERR}#g" \
        "$TEMPLATE" > "$PLIST_DST"
    echo "wrote $PLIST_DST (python=$PYTHON interval=${INTERVAL}s)"
}

case "$cmd" in
    install)
        render_plist
        launchctl unload "$PLIST_DST" 2>/dev/null || true
        launchctl load -w "$PLIST_DST"
        echo "loaded $LABEL — running one pass now:"
        PYTHONPATH="$REPO" HERMES_HOME="$HERMES_HOME" "$PYTHON" -m zeus_watchdog --dry-run
        ;;
    uninstall)
        launchctl unload "$PLIST_DST" 2>/dev/null || true
        rm -f "$PLIST_DST"
        echo "removed $LABEL"
        ;;
    status)
        launchctl list | grep "$LABEL" || echo "$LABEL not loaded"
        echo "--- tail ${STDOUT} ---"
        tail -n 20 "$STDOUT" 2>/dev/null || echo "(no log yet)"
        ;;
    *)
        echo "usage: $0 {install|uninstall|status}" >&2
        exit 2
        ;;
esac
