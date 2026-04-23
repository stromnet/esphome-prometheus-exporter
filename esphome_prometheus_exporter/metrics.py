from __future__ import annotations

import math
import re
from typing import Any, Optional

from aioesphomeapi.model import SensorStateClass
from prometheus_client import CollectorRegistry, Counter, Gauge, Info, REGISTRY

from .config import ExporterConfig


def _is_nan(value: Any) -> bool:
    try:
        return bool(math.isnan(float(value)))
    except Exception:
        return False


def base_labels(entity: Any, node: str, static_labels: Optional[dict[str, str]] = None) -> dict[str, str]:
    return {
        "node": node,
        "entity_key": str(getattr(entity, "key", "")),
        "object_id": getattr(entity, "object_id", "") or "",
        "name": getattr(entity, "name", getattr(entity, "object_id", "")) or "",
        **(static_labels or {}),
    }


def device_class(entity: Any) -> str:
    return getattr(entity, "device_class", "") or ""


def unit_of_measurement(entity: Any) -> str:
    return getattr(entity, "unit_of_measurement", "") or ""


def state_class(entity: Any) -> Any:
    return getattr(entity, "state_class", None)


def is_total_increasing(entity: Any) -> bool:
    return state_class(entity) is SensorStateClass.TOTAL_INCREASING


def sanitize_metric_component(value: str) -> str:
    sanitized = re.sub(r"[^a-zA-Z0-9_]", "_", value.strip().lower())
    sanitized = re.sub(r"_+", "_", sanitized).strip("_")
    if not sanitized:
        return "value"
    if sanitized[0].isdigit():
        sanitized = f"v_{sanitized}"
    return sanitized


def normalized_unit_suffix(unit: str) -> str:
    unit = unit.strip().lower()
    unit_map = {
        "%": "percent",
        "°c": "celsius",
        "c": "celsius",
        "°f": "fahrenheit",
        "f": "fahrenheit",
        "w": "watts",
        "kw": "kilowatts",
        "v": "volts",
        "va": "va",
        "hz": "hz",
        "a": "amps",
        "ma": "milliamps",
        "pa": "pascals",
        "hpa": "hectopascals",
        "kpa": "kilopascals",
        "bar": "bar",
        "lx": "lux",
        "s": "seconds",
        "ms": "milliseconds",
        "min": "minutes",
        "h": "hours",
        "wh": "watt_hours",
        "kwh": "kilowatt_hours",
        "db": "decibels",
        "b": "bytes",
        "kb": "kilobytes",
        "mb": "megabytes",
        "gb": "gigabytes",
    }
    if unit in unit_map:
        return unit_map[unit]
    return sanitize_metric_component(unit)


def metric_name_for_entity(entity: Any) -> str:
    device = device_class(entity)
    if not device:
        return ""
    base = sanitize_metric_component(device)
    unit = unit_of_measurement(entity)
    suffix = normalized_unit_suffix(unit) if unit else ""
    if suffix and base.endswith(f"_{suffix}"):
        suffix = ""
    parts = ["esphome", base]
    if suffix:
        parts.append(suffix)
    return "_".join(part for part in parts if part)


class MetricStore:
    def __init__(self, exporter_config: Optional[ExporterConfig] = None, registry: CollectorRegistry = REGISTRY):
        self.exporter_config = exporter_config or ExporterConfig()
        self.registry = registry
        self.static_label_keys = list(self.exporter_config.static_label_keys)
        self.dynamic_gauges: dict[str, Gauge] = {}
        self.dynamic_counters: dict[str, Counter] = {}
        self.text_infos: dict[str, Info] = {}
        self.entity_labels_by_id: dict[tuple[str, str], dict[str, str]] = {}
        self.text_values_by_entity: dict[tuple[str, str], str] = {}
        self.numeric_values_by_entity: dict[tuple[str, str], float] = {}

        entity_base_labels = ["node", "entity_key", "object_id", "name", *self.static_label_keys]
        dynamic_base_labels = ["node", "object_id", "name", *self.static_label_keys]
        node_base_labels = ["node", "host", *self.static_label_keys]

        self.numeric_fallback = Gauge(
            "esphome_sensor_value",
            "ESPHome numeric sensor value",
            [*entity_base_labels, "device_class", "unit"],
            registry=registry,
        )
        self.binary_fallback = Gauge(
            "esphome_binary_sensor_value",
            "ESPHome binary sensor value (1=true, 0=false)",
            [*entity_base_labels, "device_class"],
            registry=registry,
        )
        # prometheus_client.Info exposes samples as <name>_info, so the user-visible
        # metric is esphome_text_sensor_info even though the collector name here is
        # esphome_text_sensor.
        self.text_fallback = Info(
            "esphome_text_sensor",
            "ESPHome text sensor state",
            [*entity_base_labels, "device_class"],
            registry=registry,
        )
        self.node_up = Gauge("esphome_node_up", "Whether the ESPHome node is currently connected", node_base_labels, registry=registry)
        self.node_scrape_success = Gauge("esphome_node_scrape_success", "Whether the last ESPHome node poll/discovery cycle succeeded", node_base_labels, registry=registry)
        self.node_last_success = Gauge("esphome_node_last_success_timestamp_seconds", "Unix timestamp of the last successful ESPHome node connection and discovery", node_base_labels, registry=registry)
        self.node_entities = Gauge("esphome_node_entities", "Number of entities currently exported for an ESPHome node", node_base_labels, registry=registry)
        self.node_reconnects = Counter("esphome_node_reconnects_total", "Number of reconnects after the initial successful connection", node_base_labels, registry=registry)
        self.node_connection_errors = Counter("esphome_node_connection_errors_total", "Number of connection errors for an ESPHome node", node_base_labels, registry=registry)
        self.node_disconnects = Counter("esphome_node_disconnects_total", "Number of disconnect events for an ESPHome node", [*node_base_labels, "expected"], registry=registry)
        self.last_update = Gauge("esphome_sensor_last_update_timestamp_seconds", "Unix timestamp of the last state update received for an ESPHome entity", entity_base_labels, registry=registry)
        self.dynamic_base_labels = dynamic_base_labels
        self._collectors = [
            self.numeric_fallback,
            self.binary_fallback,
            self.text_fallback,
            self.node_up,
            self.node_scrape_success,
            self.node_last_success,
            self.node_entities,
            self.node_reconnects,
            self.node_connection_errors,
            self.node_disconnects,
            self.last_update,
        ]

    def register_entity(self, node: str, entity: Any, static_labels: Optional[dict[str, str]] = None) -> None:
        self.entity_labels_by_id[self._entity_id(node, entity)] = base_labels(entity, node, static_labels)

    def cleanup_stale_entities(self, node: str, current_entities: dict[int, Any]) -> None:
        current_ids = {self._entity_id(node, entity) for entity in current_entities.values()}
        stale_ids = [entity_id for entity_id in self.entity_labels_by_id if entity_id[0] == node and entity_id not in current_ids]
        for stale_id in stale_ids:
            labels = self.entity_labels_by_id.pop(stale_id)
            self.text_values_by_entity.pop(stale_id, None)
            self.numeric_values_by_entity.pop(stale_id, None)
            self._remove_entity_metrics(labels)

    def cleanup_node_metrics(self, node: str, host: str, static_labels: Optional[dict[str, str]] = None) -> None:
        stale_ids = [entity_id for entity_id in self.entity_labels_by_id if entity_id[0] == node]
        for stale_id in stale_ids:
            labels = self.entity_labels_by_id.pop(stale_id)
            self.text_values_by_entity.pop(stale_id, None)
            self.numeric_values_by_entity.pop(stale_id, None)
            self._remove_entity_metrics(labels)
        self._safe_remove(self.node_entities, node, host, *self._static_label_values(static_labels))

    def set_numeric(self, entity: Any, labels: dict[str, str], value: float) -> None:
        metric = self._numeric_metric_for_entity(entity)
        if metric is None or self.exporter_config.emit_raw_metrics:
            fallback_labels = {
                "node": labels["node"],
                "entity_key": labels["entity_key"],
                "object_id": labels["object_id"],
                "name": labels["name"],
                **self._extract_static_labels(labels),
                "device_class": device_class(entity),
                "unit": unit_of_measurement(entity),
            }
            self.numeric_fallback.labels(**fallback_labels).set(value)
        if metric is not None:
            metric_labels = {"node": labels["node"], "object_id": labels["object_id"], "name": labels["name"], **self._extract_static_labels(labels)}
            if is_total_increasing(entity):
                entity_id = self._entity_id(labels["node"], entity)
                previous_value = self.numeric_values_by_entity.get(entity_id)
                if previous_value is None:
                    metric.labels(**metric_labels).inc(value)
                elif value >= previous_value:
                    metric.labels(**metric_labels).inc(value - previous_value)
                else:
                    self._safe_remove(metric, labels["node"], labels["object_id"], labels["name"], *self._static_label_values(self._extract_static_labels(labels)))
                    metric.labels(**metric_labels).inc(value)
                self.numeric_values_by_entity[entity_id] = value
            else:
                metric.labels(**metric_labels).set(value)
                self.numeric_values_by_entity[self._entity_id(labels["node"], entity)] = value

    def set_binary(self, entity: Any, labels: dict[str, str], value: bool) -> None:
        numeric_value = 1 if value else 0
        metric = self._binary_metric_for_entity(entity)
        if metric is None or self.exporter_config.emit_raw_metrics:
            fallback_labels = {
                "node": labels["node"],
                "entity_key": labels["entity_key"],
                "object_id": labels["object_id"],
                "name": labels["name"],
                **self._extract_static_labels(labels),
                "device_class": device_class(entity),
            }
            self.binary_fallback.labels(**fallback_labels).set(numeric_value)
        if metric is not None:
            metric.labels(node=labels["node"], object_id=labels["object_id"], name=labels["name"], **self._extract_static_labels(labels)).set(numeric_value)

    def set_text(self, entity: Any, labels: dict[str, str], value: Any) -> None:
        fallback_labels = {
            "node": labels["node"],
            "entity_key": labels["entity_key"],
            "object_id": labels["object_id"],
            "name": labels["name"],
            **self._extract_static_labels(labels),
            "device_class": device_class(entity),
        }
        entity_id = self._entity_id(labels["node"], entity)
        text_value = "" if value is None else str(value)
        old_value = self.text_values_by_entity.get(entity_id)
        if old_value is not None and old_value != text_value:
            self._safe_remove(self.text_fallback, labels["node"], labels["entity_key"], labels["object_id"], labels["name"], *self._static_label_values(self._extract_static_labels(labels)), device_class(entity))
            dynamic_metric = self._text_metric_for_entity(entity)
            if dynamic_metric is not None:
                self._safe_remove(dynamic_metric, labels["node"], labels["object_id"], labels["name"], *self._static_label_values(self._extract_static_labels(labels)))
        self.text_values_by_entity[entity_id] = text_value
        self.text_fallback.labels(**fallback_labels).info({"value": text_value})
        metric = self._text_metric_for_entity(entity)
        if metric is not None:
            metric.labels(node=labels["node"], object_id=labels["object_id"], name=labels["name"], **self._extract_static_labels(labels)).info({"value": text_value})

    def _remove_entity_metrics(self, labels: dict[str, str]) -> None:
        static_values = self._static_label_values(self._extract_static_labels(labels))
        self._safe_remove(self.last_update, labels["node"], labels["entity_key"], labels["object_id"], labels["name"], *static_values)
        self._safe_remove(self.numeric_fallback, labels["node"], labels["entity_key"], labels["object_id"], labels["name"], *static_values, labels.get("device_class", ""), labels.get("unit", ""))
        self._safe_remove(self.binary_fallback, labels["node"], labels["entity_key"], labels["object_id"], labels["name"], *static_values, labels.get("device_class", ""))
        self._safe_remove(self.text_fallback, labels["node"], labels["entity_key"], labels["object_id"], labels["name"], *static_values, labels.get("device_class", ""))
        dynamic_name = labels.get("dynamic_metric_name")
        if dynamic_name:
            if dynamic_name in self.dynamic_gauges:
                self._safe_remove(self.dynamic_gauges[dynamic_name], labels["node"], labels["object_id"], labels["name"], *static_values)
            if dynamic_name in self.dynamic_counters:
                self._safe_remove(self.dynamic_counters[dynamic_name], labels["node"], labels["object_id"], labels["name"], *static_values)
            if dynamic_name in self.text_infos:
                self._safe_remove(self.text_infos[dynamic_name], labels["node"], labels["object_id"], labels["name"], *static_values)

    @staticmethod
    def _safe_remove(metric: Any, *label_values: str) -> None:
        try:
            metric.remove(*label_values)
        except Exception:
            pass

    @staticmethod
    def _entity_id(node: str, entity: Any) -> tuple[str, str]:
        return node, str(getattr(entity, "key", getattr(entity, "object_id", id(entity))))

    def _extract_static_labels(self, labels: dict[str, str]) -> dict[str, str]:
        return {key: labels.get(key, "") for key in self.static_label_keys}

    def _static_label_values(self, static_labels: Optional[dict[str, str]]) -> list[str]:
        static_labels = static_labels or {}
        return [static_labels.get(key, "") for key in self.static_label_keys]

    def _metric_name_for_entity(self, entity: Any) -> str:
        return metric_name_for_entity(entity)

    def _numeric_metric_for_entity(self, entity: Any) -> Optional[Any]:
        metric_name = self._metric_name_for_entity(entity)
        if not metric_name:
            return None
        if is_total_increasing(entity):
            counter = self.dynamic_counters.get(metric_name)
            if counter is None:
                counter = Counter(metric_name, f"ESPHome counter metric for {getattr(entity, 'name', getattr(entity, 'object_id', metric_name))}", self.dynamic_base_labels, registry=self.registry)
                self.dynamic_counters[metric_name] = counter
            return counter
        gauge = self.dynamic_gauges.get(metric_name)
        if gauge is None:
            gauge = Gauge(metric_name, f"ESPHome metric for {getattr(entity, 'name', getattr(entity, 'object_id', metric_name))}", self.dynamic_base_labels, registry=self.registry)
            self.dynamic_gauges[metric_name] = gauge
        return gauge

    def _binary_metric_for_entity(self, entity: Any) -> Optional[Gauge]:
        return self._numeric_metric_for_entity(entity)

    def _text_metric_for_entity(self, entity: Any) -> Optional[Info]:
        metric_name = self._metric_name_for_entity(entity)
        if not metric_name:
            return None
        info = self.text_infos.get(metric_name)
        if info is None:
            # Info collectors expose <metric_name>_info samples.
            info = Info(metric_name, f"ESPHome text metric for {getattr(entity, 'name', getattr(entity, 'object_id', metric_name))}", self.dynamic_base_labels, registry=self.registry)
            self.text_infos[metric_name] = info
        return info

    def close(self) -> None:
        for collector in self._collectors:
            try:
                self.registry.unregister(collector)
            except Exception:
                pass
        for collector in list(self.dynamic_gauges.values()) + list(self.dynamic_counters.values()) + list(self.text_infos.values()):
            try:
                self.registry.unregister(collector)
            except Exception:
                pass
        self.dynamic_gauges.clear()
        self.dynamic_counters.clear()
        self.text_infos.clear()
        self.entity_labels_by_id.clear()
        self.text_values_by_entity.clear()
        self.numeric_values_by_entity.clear()
