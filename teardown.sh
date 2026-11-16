#!/usr/bin/env bash
# Remove everything one environment created. Default = dry run.
# Usage: ./teardown.sh <target> [--apply]
set -euo pipefail
cd "$(dirname "$0")"
[[ $# -lt 1 ]] && { echo "usage: ./teardown.sh <target> [--apply]"; exit 1; }
export BEV_TARGET="$1"; shift
"${PYTHON:-python3}" scripts/teardown.py "$@"
