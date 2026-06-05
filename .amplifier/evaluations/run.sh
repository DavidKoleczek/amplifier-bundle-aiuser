#!/usr/bin/env bash
# Run the standalone AIUser evaluations.
#
# Drives the standalone AIUser (the subject) against a stock Amplifier agent
# running in a Digital Twin Universe, reusing the amplifier_evaluation building
# blocks (DTU, install, Grader). Output goes to
#   <workspace>/.amplifier/evaluation/aiuser/<run-id>/
#
# Usage:
#   evaluations/run.sh                      # run all scenarios
#   evaluations/run.sh 01                   # run only scenarios whose id contains "01"
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
REPO_ROOT="$(cd "$HERE/../.." && pwd)"

# Source the API key from the conventional location if it isn't already exported.
if [ -z "${ANTHROPIC_API_KEY:-}" ] && [ -f "$HOME/.amplifier/keys.env" ]; then
    set -a
    # shellcheck disable=SC1091
    . "$HOME/.amplifier/keys.env"
    set +a
fi
if [ -z "${ANTHROPIC_API_KEY:-}" ]; then
    echo "ERROR: ANTHROPIC_API_KEY is not set (needed by the AIUser, the grader, and the DTU)." >&2
    exit 1
fi

cd "$REPO_ROOT"
exec uv run python .amplifier/evaluations/run_eval.py "$@"
