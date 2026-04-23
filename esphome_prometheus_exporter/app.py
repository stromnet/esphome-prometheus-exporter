from __future__ import annotations

import argparse
import asyncio
import logging
import signal
from typing import Any, Optional

from prometheus_client import start_http_server

from .config import ExporterConfig, NodeIdentity, apply_aioesphomeapi_tuning, apply_prometheus_tuning, load_config, node_identity
from .metrics import MetricStore
from .node import NodeExporter


async def reload_exporters(
    *,
    config_path: str,
    secrets_path: Optional[str],
    metric_store: MetricStore,
    exporters: dict[NodeIdentity, NodeExporter],
    tasks: dict[NodeIdentity, asyncio.Task[Any]],
) -> tuple[ExporterConfig, MetricStore, dict[NodeIdentity, NodeExporter], dict[NodeIdentity, asyncio.Task[Any]]]:
    exporter_config, nodes = load_config(config_path, secrets_path=secrets_path)
    apply_aioesphomeapi_tuning(exporter_config)
    apply_prometheus_tuning(exporter_config)

    if exporter_config.static_label_keys != metric_store.static_label_keys:
        for exporter in exporters.values():
            exporter.stop()
        if tasks:
            await asyncio.gather(*tasks.values(), return_exceptions=True)
        exporters = {}
        tasks = {}
        old_metric_store = metric_store
        old_metric_store.close()
        metric_store = MetricStore(exporter_config=exporter_config, registry=old_metric_store.registry)
    else:
        metric_store.exporter_config = exporter_config

    desired = {node_identity(node): node for node in nodes}
    current = set(exporters)
    desired_keys = set(desired)

    keys_to_remove = set(current - desired_keys)
    for key in current & desired_keys:
        if exporters[key].config != desired[key]:
            keys_to_remove.add(key)

    for key in keys_to_remove:
        exporters[key].stop()
    removed_tasks = [tasks.pop(key) for key in keys_to_remove if key in tasks]
    if removed_tasks:
        await asyncio.gather(*removed_tasks, return_exceptions=True)
    for key in keys_to_remove:
        exporters.pop(key, None)

    for key, node in desired.items():
        if key not in exporters:
            exporter = NodeExporter(node, metric_store=metric_store)
            exporters[key] = exporter
            tasks[key] = asyncio.create_task(exporter.run_forever(), name=f"node:{exporter.config.name}")

    return exporter_config, metric_store, exporters, tasks


async def amain() -> None:
    parser = argparse.ArgumentParser(description="Export ESPHome sensor values as Prometheus metrics")
    parser.add_argument("config", help="Path to YAML config file")
    parser.add_argument("--secrets", help="Path to secrets YAML file (default: secrets.yaml next to config)")
    parser.add_argument("--log-level", default="INFO", help="Logging level (default: INFO)")
    args = parser.parse_args()

    logging.basicConfig(level=getattr(logging, args.log_level.upper(), logging.INFO), format="%(asctime)s %(levelname)s %(message)s")

    exporter_config, nodes = load_config(args.config, secrets_path=args.secrets)
    apply_aioesphomeapi_tuning(exporter_config)
    apply_prometheus_tuning(exporter_config)
    start_http_server(exporter_config.listen_port)
    logging.info("Prometheus metrics listening on 0.0.0.0:%d", exporter_config.listen_port)

    metric_store = MetricStore(exporter_config=exporter_config)
    exporters = {node_identity(node): NodeExporter(node, metric_store=metric_store) for node in nodes}
    tasks = {key: asyncio.create_task(exporter.run_forever(), name=f"node:{exporter.config.name}") for key, exporter in exporters.items()}

    stop_event = asyncio.Event()
    reload_event = asyncio.Event()

    def _shutdown() -> None:
        if not stop_event.is_set():
            logging.info("Shutdown requested")
            stop_event.set()
            for exporter in exporters.values():
                exporter.stop()

    def _reload() -> None:
        if not stop_event.is_set():
            logging.info("Reload requested")
            reload_event.set()

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, _shutdown)
        except NotImplementedError:
            pass
    try:
        loop.add_signal_handler(signal.SIGHUP, _reload)
    except NotImplementedError:
        pass

    while not stop_event.is_set():
        stop_task = asyncio.create_task(stop_event.wait())
        reload_task = asyncio.create_task(reload_event.wait())
        done, pending = await asyncio.wait([stop_task, reload_task], return_when=asyncio.FIRST_COMPLETED)
        for task in pending:
            task.cancel()
        await asyncio.gather(*pending, return_exceptions=True)
        await asyncio.gather(*done, return_exceptions=True)

        if reload_event.is_set() and not stop_event.is_set():
            reload_event.clear()
            try:
                new_config, metric_store, exporters, tasks = await reload_exporters(
                    config_path=args.config,
                    secrets_path=args.secrets,
                    metric_store=metric_store,
                    exporters=exporters,
                    tasks=tasks,
                )
                if new_config.listen_port != exporter_config.listen_port:
                    logging.warning("listen_port change ignored on reload (restart required)")
                exporter_config = new_config
            except Exception:
                logging.exception("Config reload failed")

    await asyncio.gather(*tasks.values(), return_exceptions=True)


def main() -> None:
    asyncio.run(amain())
