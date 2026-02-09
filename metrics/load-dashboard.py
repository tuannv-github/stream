#!/usr/bin/env python3
"""
Load dashboard(s) into Grafana on startup via API.
Waits for Grafana to be ready, then imports JSON files from grafana/dashboards/.
"""
import json
import os
import sys
import time
import urllib.request
import urllib.error
import base64

GRAFANA_URL = os.environ.get("GRAFANA_URL", "http://grafana:3000")
GRAFANA_USER = os.environ.get("GRAFANA_USER", "admin")
GRAFANA_PASSWORD = os.environ.get("GRAFANA_PASSWORD", "admin123")
DASHBOARDS_DIR = os.environ.get("DASHBOARDS_DIR", "/dashboards")
MAX_WAIT = 60
RETRY_INTERVAL = 2


def wait_for_grafana():
    """Wait for Grafana to be ready."""
    url = f"{GRAFANA_URL}/api/health"
    for i in range(MAX_WAIT // RETRY_INTERVAL):
        try:
            req = urllib.request.Request(url)
            with urllib.request.urlopen(req, timeout=5) as resp:
                if resp.status == 200:
                    return True
        except (urllib.error.URLError, OSError) as e:
            print(f"Waiting for Grafana... ({e})")
        time.sleep(RETRY_INTERVAL)
    return False


def import_dashboard(path: str) -> bool:
    """Import a dashboard JSON file into Grafana."""
    with open(path) as f:
        content = json.load(f)
    # Remove id so Grafana accepts (overwrite uses uid)
    content.pop("id", None)
    # API expects {"dashboard": {...}, "overwrite": true}
    payload = {"dashboard": content, "overwrite": True}
    data = json.dumps(payload).encode("utf-8")
    url = f"{GRAFANA_URL}/api/dashboards/db"
    req = urllib.request.Request(url, data=data, method="POST")
    req.add_header("Content-Type", "application/json")
    credentials = base64.b64encode(f"{GRAFANA_USER}:{GRAFANA_PASSWORD}".encode()).decode()
    req.add_header("Authorization", f"Basic {credentials}")
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            if resp.status in (200, 201):
                result = json.loads(resp.read().decode())
                print(f"  Imported: {content.get('title', path)} (uid={result.get('uid', '?')})")
                return True
    except urllib.error.HTTPError as e:
        body = e.read().decode() if e.fp else ""
        print(f"  Failed {path}: {e.code} {body}")
    except Exception as e:
        print(f"  Failed {path}: {e}")
    return False


def main():
    print("Load-dashboard service starting...")
    if not wait_for_grafana():
        print("Grafana not ready within timeout", file=sys.stderr)
        sys.exit(1)
    print("Grafana is ready.")
    if not os.path.isdir(DASHBOARDS_DIR):
        print(f"No dashboards dir: {DASHBOARDS_DIR}")
        return
    count = 0
    for name in sorted(os.listdir(DASHBOARDS_DIR)):
        if name.endswith(".json"):
            path = os.path.join(DASHBOARDS_DIR, name)
            if import_dashboard(path):
                count += 1
    print(f"Done. Imported {count} dashboard(s).")


if __name__ == "__main__":
    main()
