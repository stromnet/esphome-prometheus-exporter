from __future__ import annotations

import asyncio
import logging
import re
from typing import Any, Optional

from aioesphomeapi import APIClient
from aioesphomeapi.model import BinarySensorInfo, SensorInfo, TextSensorInfo
from aioesphomeapi.reconnect_logic import ReconnectLogic

from .config import FilterConfig, NodeConfig
from .metrics import MetricStore, _is_nan, base_labels, device_class, unit_of_measurement


def is_sensor_entity(entity: Any) -> bool:
    return isinstance(entity, SensorInfo)


def is_binary_sensor_entity(entity: Any) -> bool:
    return isinstance(entity, BinarySensorInfo)


def is_text_sensor_entity(entity: Any) -> bool:
    return isinstance(entity, TextSensorInfo)


def should_export_entity(entity: Any, filters: FilterConfig) -> bool:
    object_id = getattr(entity, "object_id", "") or ""
    entity_device_class = getattr(entity, "device_class", "") or ""
    name = getattr(entity, "name", object_id) or object_id

    if filters.include_object_ids and object_id not in filters.include_object_ids:
        return False
    if object_id in filters.exclude_object_ids:
        return False
    if filters.include_device_classes and entity_device_class not in filters.include_device_classes:
        return False
    if entity_device_class in filters.exclude_device_classes:
        return False
    if filters.include_name_regex and not re.search(filters.include_name_regex, name):
        return False
    if filters.exclude_name_regex and re.search(filters.exclude_name_regex, name):
        return False
    return True


class NodeExporter:
    def __init__(self, config: NodeConfig, metric_store: Optional[MetricStore] = None):
        self.config = config
        self.metric_store = metric_store or MetricStore()
        self.entities_by_key: dict[int, Any] = {}
        self.stop_event = asyncio.Event()
        self.client: Any = None
        self.had_successful_connect = False

    def _node_metric_labels(self) -> dict[str, str]:
        return {"node": self.config.name, "host": self.config.host, **{k: self.config.static_labels.get(k, "") for k in self.metric_store.static_label_keys}}

    async def run_forever(self) -> None:
        self.client = APIClient(
            address=self.config.host,
            port=self.config.port,
            password=self.config.password,
            noise_psk=self.config.noise_psk,
            keepalive=self.metric_store.exporter_config.keepalive_seconds,
            client_info="esphome-prometheus-exporter",
        )
        logic = ReconnectLogic(
            client=self.client,
            name=self.config.name,
            on_connect=self._on_connect,
            on_disconnect=self._on_disconnect,
            on_connect_error=self._on_connect_error,
        )

        await logic.start()
        try:
            await self.stop_event.wait()
        finally:
            await logic.stop()
            self.entities_by_key = {}
            self.metric_store.cleanup_node_metrics(self.config.name, self.config.host, self.config.static_labels)
            self.metric_store.node_up.labels(**self._node_metric_labels()).set(0)
            try:
                await self.client.disconnect()
            except Exception:
                pass

    async def _on_connect(self) -> None:
        node_labels = self._node_metric_labels()
        if self.had_successful_connect:
            self.metric_store.node_reconnects.labels(**node_labels).inc()
        self.had_successful_connect = True
        self.metric_store.node_up.labels(**node_labels).set(1)
        entities, _services = await self.client.list_entities_services()
        filtered_entities = [entity for entity in entities if should_export_entity(entity, self.config.filters)]
        self.entities_by_key = {entity.key: entity for entity in filtered_entities}
        self.metric_store.cleanup_stale_entities(self.config.name, self.entities_by_key)
        for entity in filtered_entities:
            self.metric_store.register_entity(self.config.name, entity, self.config.static_labels)
        self.metric_store.node_entities.labels(**node_labels).set(len(self.entities_by_key))
        self.metric_store.node_scrape_success.labels(**node_labels).set(1)
        self.metric_store.node_last_success.labels(**node_labels).set_to_current_time()
        logging.info("%s: discovered %d entities (%d exported)", self.config.name, len(entities), len(self.entities_by_key))
        self.client.subscribe_states(self._handle_state)

    async def _on_disconnect(self, expected_disconnect: bool) -> None:
        node_labels = self._node_metric_labels()
        self.metric_store.node_disconnects.labels(**node_labels, expected=str(bool(expected_disconnect)).lower()).inc()
        self.entities_by_key = {}
        self.metric_store.cleanup_node_metrics(self.config.name, self.config.host, self.config.static_labels)
        self.metric_store.node_up.labels(**node_labels).set(0)
        if not self.stop_event.is_set():
            logging.warning("%s: disconnected%s", self.config.name, " (expected)" if expected_disconnect else "")

    async def _on_connect_error(self, err: Exception) -> None:
        node_labels = self._node_metric_labels()
        self.metric_store.node_connection_errors.labels(**node_labels).inc()
        self.metric_store.node_scrape_success.labels(**node_labels).set(0)
        logging.exception("%s: connection failed", self.config.name, exc_info=err)

    def stop(self) -> None:
        self.stop_event.set()

    def _handle_state(self, state: Any) -> None:
        entity = self.entities_by_key.get(getattr(state, "key", None))
        if entity is None:
            return

        labels = base_labels(entity, self.config.name, self.config.static_labels)
        labels["device_class"] = device_class(entity)
        labels["unit"] = unit_of_measurement(entity)
        labels["dynamic_metric_name"] = self.metric_store._metric_name_for_entity(entity)
        self.metric_store.last_update.labels(node=labels["node"], entity_key=labels["entity_key"], object_id=labels["object_id"], name=labels["name"], **self.metric_store._extract_static_labels(labels)).set_to_current_time()
        self.metric_store.entity_labels_by_id[self.metric_store._entity_id(self.config.name, entity)] = labels

        if is_sensor_entity(entity):
            value = getattr(state, "state", None)
            if value is None or _is_nan(value):
                return
            self.metric_store.set_numeric(entity, labels, float(value))
            return

        if is_binary_sensor_entity(entity):
            value = getattr(state, "state", None)
            if value is None:
                return
            self.metric_store.set_binary(entity, labels, bool(value))
            return

        if is_text_sensor_entity(entity):
            self.metric_store.set_text(entity, labels, getattr(state, "state", None))
