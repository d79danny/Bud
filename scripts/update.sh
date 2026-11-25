#!/usr/bin/env bash
set -Eeuo pipefail
cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.."
git diff --quiet
git diff --cached --quiet
git pull --ff-only
exec ./scripts/install.sh "$@"
