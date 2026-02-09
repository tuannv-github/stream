# Stream Metrics (InfluxDB + Grafana)

## Start

```bash
docker compose up -d
```

- **InfluxDB**: http://localhost:8086  
- **Grafana**: http://localhost:3000 (admin / admin123)

## Dashboard loading

The `load-dashboard` service runs on startup and imports all `.json` files from `grafana/dashboards/` into Grafana via the API. Dashboards are imported (not provisioned), so you can edit and save freely.

## Export / reload dashboard

**Export to file:** **Share** → **Export** → **Save to file** → save to `grafana/dashboards/` to backup.

**Reload from file:** Add or update JSON in `grafana/dashboards/`, then run `docker compose run --rm load-dashboard` to re-import (overwrites existing dashboards with the same UID).
