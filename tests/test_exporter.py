import asyncio
import math
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
from aioesphomeapi.model import (
    BinarySensorInfo,
    BinarySensorState,
    SensorInfo,
    SensorState,
    SensorStateClass,
    TextSensorInfo,
    TextSensorState,
)
from prometheus_client import CollectorRegistry

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from esphome_prometheus_exporter.app import create_logged_task, reload_exporters
from esphome_prometheus_exporter.config import (
    ExporterConfig,
    FilterConfig,
    NodeConfig,
    apply_aioesphomeapi_tuning,
    apply_prometheus_tuning,
    load_config,
)
from esphome_prometheus_exporter.metrics import MetricStore, _is_nan, metric_name_for_entity, sanitize_metric_component
from esphome_prometheus_exporter.node import NodeExporter, should_export_entity

exporter = SimpleNamespace(
    ExporterConfig=ExporterConfig,
    FilterConfig=FilterConfig,
    MetricStore=MetricStore,
    NodeConfig=NodeConfig,
    NodeExporter=NodeExporter,
    _is_nan=_is_nan,
    apply_aioesphomeapi_tuning=apply_aioesphomeapi_tuning,
    apply_prometheus_tuning=apply_prometheus_tuning,
    load_config=load_config,
    metric_name_for_entity=metric_name_for_entity,
    reload_exporters=reload_exporters,
    sanitize_metric_component=sanitize_metric_component,
    should_export_entity=should_export_entity,
)


class FakeClient:
    def __init__(self, entities=None, device_info=None):
        self.entities = entities or []
        self.device_info_value = device_info
        self.subscribed_callback = None

    async def device_info(self):
        return self.device_info_value

    async def list_entities_services(self):
        return self.entities, []

    def subscribe_states(self, callback):
        self.subscribed_callback = callback


def make_node_exporter(exporter_config=None, node_config=None):
    registry = CollectorRegistry()
    metric_store = exporter.MetricStore(exporter_config=exporter_config, registry=registry)
    node = node_config or exporter.NodeConfig(name="kitchen", host="kitchen.local")
    node_exporter = exporter.NodeExporter(node, metric_store=metric_store)
    return registry, metric_store, node_exporter


def make_total_increasing_sensor(**kwargs):
    entity = SensorInfo(**kwargs)
    try:
        entity.state_class = SensorStateClass.TOTAL_INCREASING
    except Exception:
        object.__setattr__(entity, "state_class", SensorStateClass.TOTAL_INCREASING)
    return entity


def test_sanitize_metric_component():
    assert exporter.sanitize_metric_component("Temperature °C") == "temperature_c"
    assert exporter.sanitize_metric_component("  12 volts ") == "v_12_volts"
    assert exporter.sanitize_metric_component("") == "value"


def test_metric_name_for_entity_uses_device_class_and_unit():
    entity = SensorInfo(key=1, object_id="living_room_temp", name="Living Room Temp", unit_of_measurement="°C", device_class="temperature")
    assert exporter.metric_name_for_entity(entity) == "esphome_temperature_celsius"


def test_metric_name_for_entity_without_device_class_is_disabled():
    entity = SensorInfo(key=1, object_id="mb21_apparent_power", name="mb21 Apparent Power", unit_of_measurement="VA")
    assert exporter.metric_name_for_entity(entity) == ""


def test_should_export_entity_filters():
    entity = SensorInfo(key=1, object_id="temp1", name="Temperature 1", unit_of_measurement="°C", device_class="temperature")
    filters = exporter.FilterConfig(include_object_ids=["temp1"], include_device_classes=["temperature"], include_name_regex="Temperature")
    assert exporter.should_export_entity(entity, filters) is True
    filters = exporter.FilterConfig(exclude_object_ids=["temp1"])
    assert exporter.should_export_entity(entity, filters) is False


def test_load_config(tmp_path):
    path = tmp_path / "config.yaml"
    path.write_text(
        """
listen_port: 9200
labels:
  site: home
  env: prod
nodes:
  - name: livingroom
    host: 192.168.1.10
    port: 6053
    password: secret
    labels:
      room: livingroom
      env: dev
    filters:
      include_object_ids: [temp1]
      include_device_classes: [temperature]
""",
        encoding="utf-8",
    )
    config, nodes = exporter.load_config(str(path))
    assert config.listen_port == 9200
    assert config.keepalive_seconds == 20.0
    assert config.keepalive_timeout_ratio == 4.5
    assert len(nodes) == 1
    assert nodes[0].name == "livingroom"
    assert nodes[0].host == "192.168.1.10"
    assert nodes[0].password == "secret"
    assert nodes[0].static_labels == {"site": "home", "env": "dev", "room": "livingroom"}
    assert nodes[0].filters.include_object_ids == ["temp1"]
    assert nodes[0].filters.include_device_classes == ["temperature"]
    assert config.static_label_keys == ["env", "room", "site"]


def test_load_config_can_override_keepalive_settings(tmp_path):
    path = tmp_path / "config.yaml"
    path.write_text(
        """
listen_port: 9200
keepalive_seconds: 10
keepalive_timeout_ratio: 2.5
nodes:
  - name: livingroom
    host: 192.168.1.10
""",
        encoding="utf-8",
    )
    config, _ = exporter.load_config(str(path))
    assert config.keepalive_seconds == 10.0
    assert config.keepalive_timeout_ratio == 2.5
    assert config.include_created_metrics is False


def test_load_config_can_enable_created_metrics(tmp_path):
    path = tmp_path / "config.yaml"
    path.write_text(
        """
include_created_metrics: true
nodes:
  - name: livingroom
    host: 192.168.1.10
""",
        encoding="utf-8",
    )
    config, _ = exporter.load_config(str(path))
    assert config.include_created_metrics is True


def test_apply_aioesphomeapi_tuning():
    import aioesphomeapi.connection as aio_connection

    old_present = hasattr(aio_connection, "KEEP_ALIVE_TIMEOUT_RATIO")
    old_ratio = getattr(aio_connection, "KEEP_ALIVE_TIMEOUT_RATIO", None)
    try:
        exporter.apply_aioesphomeapi_tuning(exporter.ExporterConfig(keepalive_timeout_ratio=2.0))
        assert aio_connection.KEEP_ALIVE_TIMEOUT_RATIO == 2.0
    finally:
        if old_present:
            aio_connection.KEEP_ALIVE_TIMEOUT_RATIO = old_ratio
        else:
            delattr(aio_connection, "KEEP_ALIVE_TIMEOUT_RATIO")


def test_apply_prometheus_tuning(monkeypatch):
    calls = []

    monkeypatch.setattr("prometheus_client.disable_created_metrics", lambda: calls.append("disable"))
    monkeypatch.setattr("prometheus_client.enable_created_metrics", lambda: calls.append("enable"))

    exporter.apply_prometheus_tuning(exporter.ExporterConfig(include_created_metrics=False))
    exporter.apply_prometheus_tuning(exporter.ExporterConfig(include_created_metrics=True))

    assert calls == ["disable", "enable"]


def test_load_config_supports_esphome_style_secrets(tmp_path):
    (tmp_path / "secrets.yaml").write_text(
        """
api_password: supersecret
noise_psk: abc123
""",
        encoding="utf-8",
    )
    path = tmp_path / "config.yaml"
    path.write_text(
        """
listen_port: 9200
nodes:
  - name: livingroom
    host: 192.168.1.10
    password: !secret api_password
    noise_psk: !secret noise_psk
""",
        encoding="utf-8",
    )
    config, nodes = exporter.load_config(str(path))
    assert config.listen_port == 9200
    assert nodes[0].password == "supersecret"
    assert nodes[0].noise_psk == "abc123"


def test_load_config_supports_custom_secrets_file(tmp_path):
    (tmp_path / "my-secrets.yaml").write_text(
        """
api_password: customsecret
""",
        encoding="utf-8",
    )
    path = tmp_path / "config.yaml"
    path.write_text(
        """
nodes:
  - name: livingroom
    host: 192.168.1.10
    password: !secret api_password
""",
        encoding="utf-8",
    )
    _, nodes = exporter.load_config(str(path), secrets_path=str(tmp_path / "my-secrets.yaml"))
    assert nodes[0].password == "customsecret"


def test_load_config_raises_for_missing_secret(tmp_path):
    path = tmp_path / "config.yaml"
    path.write_text(
        """
nodes:
  - name: livingroom
    host: 192.168.1.10
    password: !secret api_password
""",
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="Secret 'api_password' not found"):
        exporter.load_config(str(path))


def test_create_logged_task_logs_exceptions(caplog):
    async def boom():
        raise RuntimeError("boom")

    async def runner():
        create_logged_task(boom(), name="node:test")
        await asyncio.sleep(0)

    with caplog.at_level("ERROR"):
        asyncio.run(runner())

    assert "Background task node:test failed" in caplog.text
    assert "RuntimeError: boom" in caplog.text


def test_reload_exporters_adds_and_removes_nodes(tmp_path, monkeypatch):
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        """
nodes:
  - name: node1
    host: 10.0.0.1
""",
        encoding="utf-8",
    )
    registry = CollectorRegistry()
    metric_store = exporter.MetricStore(registry=registry)
    exporters = {}
    tasks = {}

    async def fake_run_forever(self):
        await self.stop_event.wait()

    monkeypatch.setattr(exporter.NodeExporter, "run_forever", fake_run_forever)

    async def runner():
        nonlocal exporters, tasks
        _, _, exporters, tasks = await exporter.reload_exporters(
            config_path=str(config_path),
            secrets_path=None,
            metric_store=metric_store,
            exporters=exporters,
            tasks=tasks,
        )
        assert list(exporters) == [("node1", "10.0.0.1", 6053)]

        config_path.write_text(
            """
nodes:
  - name: node2
    host: 10.0.0.2
""",
            encoding="utf-8",
        )
        _, _, exporters, tasks = await exporter.reload_exporters(
            config_path=str(config_path),
            secrets_path=None,
            metric_store=metric_store,
            exporters=exporters,
            tasks=tasks,
        )
        assert list(exporters) == [("node2", "10.0.0.2", 6053)]

        for exporter_instance in exporters.values():
            exporter_instance.stop()
        await asyncio.gather(*tasks.values(), return_exceptions=True)

    asyncio.run(runner())


def test_on_connect_filters_entities_and_counts_reconnects_and_errors():
    node_config = exporter.NodeConfig(
        name="kitchen",
        host="kitchen.local",
        static_labels={"site": "home"},
        filters=exporter.FilterConfig(include_device_classes=["temperature"]),
    )
    registry, _, node_exporter = make_node_exporter(exporter.ExporterConfig(static_label_keys=["site"]), node_config=node_config)
    temp = SensorInfo(key=1, object_id="temp1", name="Temperature", unit_of_measurement="°C", device_class="temperature")
    power = SensorInfo(key=2, object_id="power1", name="Power", unit_of_measurement="W")
    node_exporter.client = FakeClient(entities=[temp, power])

    asyncio.run(node_exporter._on_connect())
    assert set(node_exporter.entities_by_key) == {(0, 1)}
    asyncio.run(node_exporter._on_disconnect(False))
    asyncio.run(node_exporter._on_connect())
    asyncio.run(node_exporter._on_connect_error(RuntimeError("boom")))

    assert registry.get_sample_value("esphome_node_reconnects_total", labels={"node": "kitchen", "host": "kitchen.local", "site": "home"}) == 1.0
    assert registry.get_sample_value("esphome_node_connection_errors_total", labels={"node": "kitchen", "host": "kitchen.local", "site": "home"}) == 1.0
    assert registry.get_sample_value("esphome_node_disconnects_total", labels={"node": "kitchen", "host": "kitchen.local", "site": "home", "expected": "false"}) == 1.0


def test_reload_exporters_recreates_metric_store_for_static_label_key_changes(tmp_path, monkeypatch):
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        """
labels:
  site: home
nodes:
  - name: node1
    host: 10.0.0.1
""",
        encoding="utf-8",
    )
    registry = CollectorRegistry()
    metric_store = exporter.MetricStore(exporter.ExporterConfig(static_label_keys=["site"]), registry=registry)
    exporters = {}
    tasks = {}

    async def fake_run_forever(self):
        await self.stop_event.wait()

    monkeypatch.setattr(exporter.NodeExporter, "run_forever", fake_run_forever)

    async def runner():
        nonlocal metric_store, exporters, tasks
        _, metric_store, exporters, tasks = await exporter.reload_exporters(
            config_path=str(config_path),
            secrets_path=None,
            metric_store=metric_store,
            exporters=exporters,
            tasks=tasks,
        )

        config_path.write_text(
            """
labels:
  site: home
  env: prod
nodes:
  - name: node1
    host: 10.0.0.1
""",
            encoding="utf-8",
        )
        _, metric_store, exporters, tasks = await exporter.reload_exporters(
            config_path=str(config_path),
            secrets_path=None,
            metric_store=metric_store,
            exporters=exporters,
            tasks=tasks,
        )
        assert metric_store.static_label_keys == ["env", "site"]
        assert list(exporters) == [("node1", "10.0.0.1", 6053)]

        for exporter_instance in exporters.values():
            exporter_instance.stop()
        await asyncio.gather(*tasks.values(), return_exceptions=True)

    asyncio.run(runner())


def test_reload_exporters_restarts_changed_node(tmp_path, monkeypatch):
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        """
nodes:
  - name: node1
    host: 10.0.0.1
    password: old
""",
        encoding="utf-8",
    )
    registry = CollectorRegistry()
    metric_store = exporter.MetricStore(registry=registry)
    exporters = {}
    tasks = {}

    async def fake_run_forever(self):
        await self.stop_event.wait()

    monkeypatch.setattr(exporter.NodeExporter, "run_forever", fake_run_forever)

    async def runner():
        nonlocal exporters, tasks
        _, _, exporters, tasks = await exporter.reload_exporters(
            config_path=str(config_path),
            secrets_path=None,
            metric_store=metric_store,
            exporters=exporters,
            tasks=tasks,
        )
        original_exporter = exporters[("node1", "10.0.0.1", 6053)]
        assert original_exporter.config.password == "old"

        config_path.write_text(
            """
nodes:
  - name: node1
    host: 10.0.0.1
    password: new
""",
            encoding="utf-8",
        )
        _, _, exporters, tasks = await exporter.reload_exporters(
            config_path=str(config_path),
            secrets_path=None,
            metric_store=metric_store,
            exporters=exporters,
            tasks=tasks,
        )
        replacement_exporter = exporters[("node1", "10.0.0.1", 6053)]
        assert replacement_exporter is not original_exporter
        assert replacement_exporter.config.password == "new"

        for exporter_instance in exporters.values():
            exporter_instance.stop()
        await asyncio.gather(*tasks.values(), return_exceptions=True)

    asyncio.run(runner())


def test_numeric_state_updates_only_dynamic_metrics_by_default():
    registry, _, node_exporter = make_node_exporter()
    entity = SensorInfo(key=1, object_id="living_room_temp", name="Living Room Temp", unit_of_measurement="°C", device_class="temperature")
    node_exporter.entities_by_key[1] = entity

    node_exporter._handle_state(SensorState(key=1, state=21.5))

    generic_value = registry.get_sample_value(
        "esphome_sensor_value",
        labels={
            "node": "kitchen",
            "entity_key": "1",
            "object_id": "living_room_temp",
            "name": "Living Room Temp",
            "device_id": "0",
            "device_name": "",
            "device_class": "temperature",
            "unit": "°C",
        },
    )
    dynamic_value = registry.get_sample_value(
        "esphome_temperature_celsius",
        labels={"node": "kitchen", "object_id": "living_room_temp", "name": "Living Room Temp", "device_id": "0", "device_name": ""},
    )
    assert generic_value is None
    assert dynamic_value == 21.5


def test_numeric_state_without_device_class_emits_raw_metric_by_default():
    registry, _, node_exporter = make_node_exporter()
    entity = SensorInfo(key=1, object_id="mb21_apparent_power", name="mb21 Apparent Power", unit_of_measurement="VA")
    node_exporter.entities_by_key[1] = entity

    node_exporter._handle_state(SensorState(key=1, state=42.0))

    assert registry.get_sample_value(
        "esphome_sensor_value",
        labels={
            "node": "kitchen",
            "entity_key": "1",
            "object_id": "mb21_apparent_power",
            "name": "mb21 Apparent Power",
            "device_id": "0",
            "device_name": "",
            "device_class": "",
            "unit": "VA",
        },
    ) == 42.0
    assert registry.get_sample_value(
        "esphome_apparent_power_va",
        labels={"node": "kitchen", "object_id": "mb21_apparent_power", "name": "mb21 Apparent Power", "device_id": "0", "device_name": ""},
    ) is None


def test_numeric_state_without_device_class_can_emit_raw_metric_when_enabled():
    registry, _, node_exporter = make_node_exporter(exporter.ExporterConfig(emit_raw_metrics=True))
    entity = SensorInfo(key=1, object_id="mb21_apparent_power", name="mb21 Apparent Power", unit_of_measurement="VA")
    node_exporter.entities_by_key[1] = entity

    node_exporter._handle_state(SensorState(key=1, state=42.0))

    assert registry.get_sample_value(
        "esphome_sensor_value",
        labels={
            "node": "kitchen",
            "entity_key": "1",
            "object_id": "mb21_apparent_power",
            "name": "mb21 Apparent Power",
            "device_id": "0",
            "device_name": "",
            "device_class": "",
            "unit": "VA",
        },
    ) == 42.0
    assert registry.get_sample_value(
        "esphome_apparent_power_va",
        labels={"node": "kitchen", "object_id": "mb21_apparent_power", "name": "mb21 Apparent Power", "device_id": "0", "device_name": ""},
    ) is None


def test_numeric_state_with_specific_metric_can_emit_raw_metrics_when_enabled():
    registry, _, node_exporter = make_node_exporter(exporter.ExporterConfig(emit_raw_metrics=True))
    entity = SensorInfo(key=1, object_id="living_room_temp", name="Living Room Temp", unit_of_measurement="°C", device_class="temperature")
    node_exporter.entities_by_key[1] = entity

    node_exporter._handle_state(SensorState(key=1, state=21.5))

    generic_value = registry.get_sample_value(
        "esphome_sensor_value",
        labels={
            "node": "kitchen",
            "entity_key": "1",
            "object_id": "living_room_temp",
            "name": "Living Room Temp",
            "device_id": "0",
            "device_name": "",
            "device_class": "temperature",
            "unit": "°C",
        },
    )
    dynamic_value = registry.get_sample_value(
        "esphome_temperature_celsius",
        labels={"node": "kitchen", "object_id": "living_room_temp", "name": "Living Room Temp", "device_id": "0", "device_name": ""},
    )
    assert generic_value == 21.5
    assert dynamic_value == 21.5


def test_numeric_total_increasing_sensor_uses_counter_metric():
    registry, _, node_exporter = make_node_exporter()
    entity = make_total_increasing_sensor(
        key=1,
        object_id="energy_today",
        name="Energy Today",
        unit_of_measurement="kWh",
        device_class="energy",
    )
    node_exporter.entities_by_key[1] = entity

    node_exporter._handle_state(SensorState(key=1, state=10.0))
    node_exporter._handle_state(SensorState(key=1, state=12.5))

    assert registry.get_sample_value(
        "esphome_energy_kilowatt_hours_total",
        labels={"node": "kitchen", "object_id": "energy_today", "name": "Energy Today", "device_id": "0", "device_name": ""},
    ) == 12.5
    assert registry.get_sample_value(
        "esphome_energy_kilowatt_hours",
        labels={"node": "kitchen", "object_id": "energy_today", "name": "Energy Today", "device_id": "0", "device_name": ""},
    ) is None


def test_numeric_total_increasing_sensor_counter_handles_reset():
    registry, _, node_exporter = make_node_exporter()
    entity = make_total_increasing_sensor(
        key=1,
        object_id="energy_today",
        name="Energy Today",
        unit_of_measurement="kWh",
        device_class="energy",
    )
    node_exporter.entities_by_key[1] = entity

    node_exporter._handle_state(SensorState(key=1, state=10.0))
    node_exporter._handle_state(SensorState(key=1, state=12.5))
    node_exporter._handle_state(SensorState(key=1, state=1.5))

    assert registry.get_sample_value(
        "esphome_energy_kilowatt_hours_total",
        labels={"node": "kitchen", "object_id": "energy_today", "name": "Energy Today", "device_id": "0", "device_name": ""},
    ) == 1.5


def test_binary_state_updates_only_dynamic_metric_by_default():
    registry, _, node_exporter = make_node_exporter()
    entity = BinarySensorInfo(key=2, object_id="front_door", name="Front Door", device_class="door")
    node_exporter.entities_by_key[2] = entity

    node_exporter._handle_state(BinarySensorState(key=2, state=True))

    generic_value = registry.get_sample_value(
        "esphome_binary_sensor_value",
        labels={
            "node": "kitchen",
            "entity_key": "2",
            "object_id": "front_door",
            "name": "Front Door",
            "device_id": "0",
            "device_name": "",
            "device_class": "door",
        },
    )
    dynamic_value = registry.get_sample_value(
        "esphome_door",
        labels={"node": "kitchen", "object_id": "front_door", "name": "Front Door", "device_id": "0", "device_name": ""},
    )
    assert generic_value is None
    assert dynamic_value == 1.0


def test_binary_state_without_device_class_emits_raw_metric_by_default():
    registry, _, node_exporter = make_node_exporter()
    entity = BinarySensorInfo(key=2, object_id="garage_motion", name="Garage Motion")
    node_exporter.entities_by_key[2] = entity

    node_exporter._handle_state(BinarySensorState(key=2, state=True))

    generic_value = registry.get_sample_value(
        "esphome_binary_sensor_value",
        labels={
            "node": "kitchen",
            "entity_key": "2",
            "object_id": "garage_motion",
            "name": "Garage Motion",
            "device_id": "0",
            "device_name": "",
            "device_class": "",
        },
    )
    assert generic_value == 1.0


def test_binary_state_with_specific_metric_can_emit_raw_when_enabled():
    registry, _, node_exporter = make_node_exporter(exporter.ExporterConfig(emit_raw_metrics=True))
    entity = BinarySensorInfo(key=2, object_id="front_door", name="Front Door", device_class="door")
    node_exporter.entities_by_key[2] = entity

    node_exporter._handle_state(BinarySensorState(key=2, state=True))

    generic_value = registry.get_sample_value(
        "esphome_binary_sensor_value",
        labels={
            "node": "kitchen",
            "entity_key": "2",
            "object_id": "front_door",
            "name": "Front Door",
            "device_id": "0",
            "device_name": "",
            "device_class": "door",
        },
    )
    dynamic_value = registry.get_sample_value(
        "esphome_door",
        labels={"node": "kitchen", "object_id": "front_door", "name": "Front Door", "device_id": "0", "device_name": ""},
    )
    assert generic_value == 1.0
    assert dynamic_value == 1.0


def test_text_state_updates_info_metrics():
    registry, _, node_exporter = make_node_exporter()
    entity = TextSensorInfo(key=3, object_id="firmware_status", name="Firmware Status")
    node_exporter.entities_by_key[3] = entity

    node_exporter._handle_state(TextSensorState(key=3, state="ok"))

    generic_value = registry.get_sample_value(
        "esphome_text_sensor_info",
        labels={
            "node": "kitchen",
            "entity_key": "3",
            "object_id": "firmware_status",
            "name": "Firmware Status",
            "device_id": "0",
            "device_name": "",
            "device_class": "",
            "value": "ok",
        },
    )
    dynamic_value = registry.get_sample_value(
        "esphome_firmware_status_info",
        labels={"node": "kitchen", "object_id": "firmware_status", "name": "Firmware Status", "device_id": "0", "device_name": "", "value": "ok"},
    )
    assert generic_value == 1.0
    assert dynamic_value is None

    node_exporter._handle_state(TextSensorState(key=3, state="warn"))

    old_generic_value = registry.get_sample_value(
        "esphome_text_sensor_info",
        labels={
            "node": "kitchen",
            "entity_key": "3",
            "object_id": "firmware_status",
            "name": "Firmware Status",
            "device_id": "0",
            "device_name": "",
            "device_class": "",
            "value": "ok",
        },
    )
    old_dynamic_value = registry.get_sample_value(
        "esphome_firmware_status_info",
        labels={"node": "kitchen", "object_id": "firmware_status", "name": "Firmware Status", "device_id": "0", "device_name": "", "value": "ok"},
    )
    new_generic_value = registry.get_sample_value(
        "esphome_text_sensor_info",
        labels={
            "node": "kitchen",
            "entity_key": "3",
            "object_id": "firmware_status",
            "name": "Firmware Status",
            "device_id": "0",
            "device_name": "",
            "device_class": "",
            "value": "warn",
        },
    )
    new_dynamic_value = registry.get_sample_value(
        "esphome_firmware_status_info",
        labels={"node": "kitchen", "object_id": "firmware_status", "name": "Firmware Status", "device_id": "0", "device_name": "", "value": "warn"},
    )
    assert old_generic_value in (0.0, None)
    assert old_dynamic_value in (0.0, None)
    assert new_generic_value == 1.0
    assert new_dynamic_value is None


def test_stale_cleanup_removes_old_metrics():
    registry, metric_store, node_exporter = make_node_exporter(exporter.ExporterConfig(emit_raw_metrics=True))
    entity = SensorInfo(key=1, object_id="living_room_temp", name="Living Room Temp", unit_of_measurement="°C", device_class="temperature")
    node_exporter.entities_by_key[1] = entity
    node_exporter._handle_state(SensorState(key=1, state=20.0))

    metric_store.cleanup_stale_entities("kitchen", {})

    generic_value = registry.get_sample_value(
        "esphome_sensor_value",
        labels={
            "node": "kitchen",
            "entity_key": "1",
            "object_id": "living_room_temp",
            "name": "Living Room Temp",
            "device_id": "0",
            "device_name": "",
            "device_class": "temperature",
            "unit": "°C",
        },
    )
    dynamic_value = registry.get_sample_value(
        "esphome_temperature_celsius",
        labels={"node": "kitchen", "object_id": "living_room_temp", "name": "Living Room Temp", "device_id": "0", "device_name": ""},
    )
    assert generic_value is None
    assert dynamic_value is None


def test_disconnect_cleanup_removes_node_metrics():
    registry, metric_store, node_exporter = make_node_exporter(exporter.ExporterConfig(emit_raw_metrics=True))
    numeric_entity = SensorInfo(key=1, object_id="living_room_temp", name="Living Room Temp", unit_of_measurement="°C", device_class="temperature")
    binary_entity = BinarySensorInfo(key=2, object_id="front_door", name="Front Door", device_class="door")
    text_entity = TextSensorInfo(key=3, object_id="firmware_status", name="Firmware Status")
    node_exporter.entities_by_key = {1: numeric_entity, 2: binary_entity, 3: text_entity}

    node_exporter._handle_state(SensorState(key=1, state=20.0))
    node_exporter._handle_state(BinarySensorState(key=2, state=True))
    node_exporter._handle_state(TextSensorState(key=3, state="ok"))
    metric_store.node_entities.labels(node="kitchen", host="kitchen.local").set(3)

    metric_store.cleanup_node_metrics("kitchen", "kitchen.local")

    assert registry.get_sample_value(
        "esphome_sensor_value",
        labels={
            "node": "kitchen",
            "entity_key": "1",
            "object_id": "living_room_temp",
            "name": "Living Room Temp",
            "device_id": "0",
            "device_name": "",
            "device_class": "temperature",
            "unit": "°C",
        },
    ) is None
    assert registry.get_sample_value(
        "esphome_temperature_celsius",
        labels={"node": "kitchen", "object_id": "living_room_temp", "name": "Living Room Temp", "device_id": "0", "device_name": ""},
    ) is None
    assert registry.get_sample_value(
        "esphome_binary_sensor_value",
        labels={
            "node": "kitchen",
            "entity_key": "2",
            "object_id": "front_door",
            "name": "Front Door",
            "device_id": "0",
            "device_name": "",
            "device_class": "door",
        },
    ) is None
    assert registry.get_sample_value(
        "esphome_door",
        labels={"node": "kitchen", "object_id": "front_door", "name": "Front Door", "device_id": "0", "device_name": ""},
    ) is None
    assert registry.get_sample_value(
        "esphome_text_sensor_info",
        labels={
            "node": "kitchen",
            "entity_key": "3",
            "object_id": "firmware_status",
            "name": "Firmware Status",
            "device_id": "0",
            "device_name": "",
            "device_class": "",
            "value": "ok",
        },
    ) is None
    assert registry.get_sample_value(
        "esphome_firmware_status_info",
        labels={"node": "kitchen", "object_id": "firmware_status", "name": "Firmware Status", "device_id": "0", "device_name": "", "value": "ok"},
    ) is None
    assert registry.get_sample_value(
        "esphome_node_entities",
        labels={"node": "kitchen", "host": "kitchen.local"},
    ) is None


def test_is_nan():
    assert exporter._is_nan(math.nan) is True
    assert exporter._is_nan(1.0) is False
    assert exporter._is_nan("abc") is False


def test_on_connect_discovers_entities_and_subscribes():
    node_config = exporter.NodeConfig(name="kitchen", host="kitchen.local", static_labels={"site": "home"})
    registry, metric_store, node_exporter = make_node_exporter(exporter.ExporterConfig(static_label_keys=["site"]), node_config=node_config)
    entity = SensorInfo(key=1, object_id="living_room_temp", name="Living Room Temp", unit_of_measurement="°C", device_class="temperature")
    node_exporter.client = FakeClient(entities=[entity])

    asyncio.run(node_exporter._on_connect())

    assert node_exporter.entities_by_key[(0, 1)] == entity
    assert node_exporter.client.subscribed_callback == node_exporter._handle_state
    assert registry.get_sample_value("esphome_node_up", labels={"node": "kitchen", "host": "kitchen.local", "site": "home"}) == 1.0
    assert registry.get_sample_value("esphome_node_entities", labels={"node": "kitchen", "host": "kitchen.local", "site": "home"}) == 1.0
    assert registry.get_sample_value("esphome_node_scrape_success", labels={"node": "kitchen", "host": "kitchen.local", "site": "home"}) == 1.0


def test_on_connect_populates_device_labels_from_device_info():
    registry, _, node_exporter = make_node_exporter()
    entity = SensorInfo(key=1, device_id=2, object_id="living_room_temp", name="Living Room Temp", unit_of_measurement="°C", device_class="temperature")
    device_info = SimpleNamespace(
        name="kitchen-node",
        friendly_name="Kitchen Node",
        devices=[SimpleNamespace(device_id=2, name="Probe A")],
    )
    node_exporter.client = FakeClient(entities=[entity], device_info=device_info)

    asyncio.run(node_exporter._on_connect())
    node_exporter._handle_state(SensorState(key=1, device_id=2, state=21.5))

    assert registry.get_sample_value(
        "esphome_temperature_celsius",
        labels={"node": "kitchen", "object_id": "living_room_temp", "name": "Living Room Temp", "device_id": "2", "device_name": "Probe A"},
    ) == 21.5


def test_state_lookup_uses_device_id_and_key_tuple():
    registry, _, node_exporter = make_node_exporter()
    entity_a = SensorInfo(key=1, device_id=2, object_id="probe_a_temp", name="Probe A Temp", unit_of_measurement="°C", device_class="temperature")
    entity_b = SensorInfo(key=1, device_id=3, object_id="probe_b_temp", name="Probe B Temp", unit_of_measurement="°C", device_class="temperature")
    device_info = SimpleNamespace(
        name="kitchen-node",
        friendly_name="Kitchen Node",
        devices=[SimpleNamespace(device_id=2, name="Probe A"), SimpleNamespace(device_id=3, name="Probe B")],
    )
    node_exporter.client = FakeClient(entities=[entity_a, entity_b], device_info=device_info)

    asyncio.run(node_exporter._on_connect())
    node_exporter._handle_state(SensorState(key=1, device_id=3, state=21.5))

    assert registry.get_sample_value(
        "esphome_temperature_celsius",
        labels={"node": "kitchen", "object_id": "probe_a_temp", "name": "Probe A Temp", "device_id": "2", "device_name": "Probe A"},
    ) is None
    assert registry.get_sample_value(
        "esphome_temperature_celsius",
        labels={"node": "kitchen", "object_id": "probe_b_temp", "name": "Probe B Temp", "device_id": "3", "device_name": "Probe B"},
    ) == 21.5


def test_on_disconnect_removes_metrics():
    registry, metric_store, node_exporter = make_node_exporter(exporter.ExporterConfig(emit_raw_metrics=True))
    entity = SensorInfo(key=1, object_id="living_room_temp", name="Living Room Temp", unit_of_measurement="°C", device_class="temperature")
    node_exporter.entities_by_key[1] = entity
    node_exporter._handle_state(SensorState(key=1, state=21.5))
    metric_store.node_entities.labels(node="kitchen", host="kitchen.local").set(1)
    metric_store.node_up.labels(node="kitchen", host="kitchen.local").set(1)

    asyncio.run(node_exporter._on_disconnect(False))

    assert node_exporter.entities_by_key == {}
    assert registry.get_sample_value(
        "esphome_temperature_celsius",
        labels={"node": "kitchen", "object_id": "living_room_temp", "name": "Living Room Temp", "device_id": "0", "device_name": ""},
    ) is None
    assert registry.get_sample_value("esphome_node_entities", labels={"node": "kitchen", "host": "kitchen.local"}) is None
    assert registry.get_sample_value("esphome_node_up", labels={"node": "kitchen", "host": "kitchen.local"}) == 0.0
