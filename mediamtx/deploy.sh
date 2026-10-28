#!/usr/bin/env bash
# Start MediaMTX (https://github.com/bluenviron/mediamtx).
#
# Usage:
#   ./stream/mediamtx/deploy.sh

set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "${ROOT}"

if [[ ! -f mediamtx.yaml ]]; then
  echo "error: missing ${ROOT}/mediamtx.yaml" >&2
  exit 1
fi

docker compose pull
docker compose up -d
docker compose ps
