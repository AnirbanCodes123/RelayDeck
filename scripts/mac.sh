#!/bin/sh
set -eu
cd "$(dirname "$0")/.."
case "${1:-start}" in
  start)
    python3 scripts/mac_host_monitor.py --background
    docker compose -f docker-compose.yml -f docker-compose.mac.yml up -d --build
    ;;
  stop)
    docker compose -f docker-compose.yml -f docker-compose.mac.yml stop
    python3 scripts/mac_host_monitor.py --stop
    ;;
  *)
    echo "Usage: $0 [start|stop]" >&2
    exit 1
    ;;
esac
