#!/usr/bin/env bash
# Stop and remove the MediaMTX container.
#
# Usage:
#   ./stream/mediamtx/undeploy.sh

set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "${ROOT}"

docker compose down
docker compose ps -a
