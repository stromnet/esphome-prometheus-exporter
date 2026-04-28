# ESPHome Prometheus Exporter

A small Python exporter that connects directly to one or more ESPHome nodes over the native ESPHome API, subscribes to sensor updates, and exposes them as Prometheus metrics.

## Features

- Connects to multiple ESPHome nodes from a YAML config file
- Supports numeric sensors, binary sensors, and text sensors
- Auto-reconnects when a node disconnects
- Exposes generic metrics for binary and text entities
- Exposes normalized per-entity metrics when `device_class` is available, such as `esphome_temperature_celsius`
- Always exports a raw numeric metric when no specific metric name can be derived
- Per-node static labels added to all metrics
- Per-node entity filtering by object id, device class, and name regex
- Reconnect/error/disconnect counters per node
- Configurable ESPHome API keepalive interval and timeout ratio
- Removes stale metrics when entities disappear after rediscovery
- Publishes per-node health metrics

## Files

- `esphome_prometheus_exporter/` - exporter package (`__main__.py`, config, metrics, node, app)
- `esphome_exporter.example.yaml` - example configuration
- `requirements.txt` - runtime dependencies
- `tests/test_exporter.py` - pytest-based unit tests
- `secrets.example.yaml` - example secrets file for `!secret` references

## Installation

```bash
python3 -m virtualenv .pi-venv
source .pi-venv/bin/activate
python -m pip install -r requirements.txt
```

## Configuration

Example:

```yaml
listen_port: 9108
emit_raw_metrics: false
include_created_metrics: false
keepalive_seconds: 20
keepalive_timeout_ratio: 4.5
labels:
  site: home
nodes:
  - name: livingroom
    host: 192.168.1.10
    port: 6053
    password: !secret livingroom_api_password
    labels:
      site: home
      room: livingroom
    filters:
      exclude_object_ids: [wifi_signal]
      exclude_name_regex: "Debug|Uptime"

  - name: garage
    host: garage-esphome.local
    password: !secret garage_api_password
    labels:
      site: home
      room: garage
    # noise_psk: !secret garage_noise_psk
```

The config loader supports ESPHome-style `!secret` references. By default, secrets are read from a `secrets.yaml` file in the same directory as the main config file. You can override this with `--secrets /path/to/secrets.yaml`.

`keepalive_seconds` controls how often aioesphomeapi sends pings. `keepalive_timeout_ratio` overrides `aioesphomeapi.connection.KEEP_ALIVE_TIMEOUT_RATIO`, so the effective pong timeout becomes `keepalive_seconds * keepalive_timeout_ratio`.

`include_created_metrics` controls whether Prometheus client `*_created` companion series are emitted for counters. It defaults to `false`.

Example `secrets.yaml`:

```yaml
livingroom_api_password: ""
garage_api_password: "supersecret"
garage_noise_psk: "base64-encoded-psk"
```

## Running

```bash
python3 -m esphome_prometheus_exporter esphome_exporter.example.yaml
```

With a custom secrets file:

```bash
python3 -m esphome_prometheus_exporter --secrets /path/to/secrets.yaml esphome_exporter.example.yaml
```

Send `SIGHUP` to reload the config and add/remove nodes without a full restart:

```bash
kill -HUP <pid>
```

Reload also restarts existing nodes whose config changed, for example `password`, `noise_psk`, `host`, `port`, or label values. Changes to `listen_port` still require a full process restart.

Changing the set of static label keys on reload is supported. The exporter recreates its metric collectors and restarts node exporters so the new Prometheus label schema takes effect.

Metrics are then available at:

- `http://localhost:9108/metrics`

## Exported metrics

### Generic metrics

- `esphome_sensor_value`
- `esphome_binary_sensor_value`
- `esphome_text_sensor_info`

Note: text metrics are implemented with Prometheus `Info`, so the exporter registers the base collector names without the `_info` suffix and Prometheus exposes them as `*_info` samples.
- `esphome_node_up`
- `esphome_node_scrape_success`
- `esphome_node_last_success_timestamp_seconds`
- `esphome_node_entities`
- `esphome_node_reconnects_total`
- `esphome_node_connection_errors_total`
- `esphome_node_disconnects_total`
- `esphome_sensor_last_update_timestamp_seconds`

`esphome_sensor_value` is always emitted for numeric sensors that do not have a normalized per-entity metric name, such as sensors without `device_class`. For sensors that do have a normalized metric, `esphome_sensor_value` is emitted only when `emit_raw_metrics: true` is enabled.

Entity metrics also include `device_id` and `device_name`. `device_id` is the ESPHome device/subdevice id referenced by the entity, and `device_name` is populated when the node reports device metadata for that id. All metrics also include any global static labels configured at the top-level under `labels:` as well as any per-node labels under `nodes[].labels:`. Per-node labels override global labels with the same key.

### Dynamic per-entity metrics

The exporter creates normalized metric names only when `device_class` is available. Examples:

- `esphome_temperature_celsius`
- `esphome_humidity_percent`
- `esphome_door`

For numeric sensors with `state_class: total_increasing`, the exporter uses a Prometheus `Counter` for the normalized per-entity metric instead of a `Gauge`. In Prometheus exposition, those appear with the usual `_total` suffix, for example `esphome_energy_kilowatt_hours_total`. The optional `*_created` companion series are disabled by default and can be enabled with `include_created_metrics: true`.

If `device_class` is missing, the exporter does not try to guess a normalized metric name from `object_id` or `name`. In that case, it emits only the raw `esphome_sensor_value` metric.

These metrics use labels:

- `node`
- `object_id`
- `name`
- `device_id`
- `device_name`
- any global static labels configured under top-level `labels:`
- any per-node labels configured under `nodes[].labels:`

### Filtering

Per-node filtering is supported with:

- `include_object_ids`
- `exclude_object_ids`
- `include_device_classes`
- `exclude_device_classes`
- `include_name_regex`
- `exclude_name_regex`

Filtered entities are ignored entirely and produce no metrics.

## Running tests

Use the project virtualenv's pytest directly:

```bash
python3 -m virtualenv .pi-venv
source .pi-venv/bin/activate
python -m pip install -r requirements.txt
.pi-venv/bin/pytest -v
```

## Notes

- The exporter requires the ESPHome native API to be enabled on each node.
- For encrypted API connections, set `noise_psk` in the YAML config.
- This exporter is subscription-based, so values update whenever ESPHome pushes new states.
