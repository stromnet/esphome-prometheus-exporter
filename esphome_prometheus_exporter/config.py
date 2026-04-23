from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional, TypeAlias

import aioesphomeapi.connection as aio_connection
import prometheus_client
import yaml


NodeIdentity: TypeAlias = tuple[str, str, int]


@dataclass
class FilterConfig:
    include_object_ids: list[str] = field(default_factory=list)
    exclude_object_ids: list[str] = field(default_factory=list)
    include_device_classes: list[str] = field(default_factory=list)
    exclude_device_classes: list[str] = field(default_factory=list)
    include_name_regex: str = ""
    exclude_name_regex: str = ""


@dataclass
class NodeConfig:
    name: str
    host: str
    port: int = 6053
    password: str = ""
    noise_psk: Optional[str] = None
    static_labels: dict[str, str] = field(default_factory=dict)
    filters: FilterConfig = field(default_factory=FilterConfig)


@dataclass
class ExporterConfig:
    listen_port: int = 9108
    emit_raw_metrics: bool = False
    include_created_metrics: bool = False
    keepalive_seconds: float = 20.0
    keepalive_timeout_ratio: float = 4.5
    static_label_keys: list[str] = field(default_factory=list)


class SecretLoader(yaml.SafeLoader):
    pass


def load_secrets(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    with path.open("r", encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}
    if not isinstance(data, dict):
        raise ValueError(f"Secrets file must contain a mapping: {path}")
    return data


def load_yaml_with_secrets(path: str, secrets_path: Optional[str] = None) -> dict[str, Any]:
    config_path = Path(path)
    resolved_secrets_path = Path(secrets_path) if secrets_path else (config_path.parent / "secrets.yaml")
    secrets = load_secrets(resolved_secrets_path)

    def secret_constructor(loader: SecretLoader, node: yaml.nodes.Node) -> Any:
        secret_name = loader.construct_scalar(node)
        if secret_name not in secrets:
            raise ValueError(f"Secret '{secret_name}' not found in {resolved_secrets_path}")
        return secrets[secret_name]

    SecretLoader.add_constructor("!secret", secret_constructor)
    with config_path.open("r", encoding="utf-8") as f:
        return yaml.load(f, Loader=SecretLoader) or {}


def load_config(path: str, secrets_path: Optional[str] = None) -> tuple[ExporterConfig, list[NodeConfig]]:
    raw = load_yaml_with_secrets(path, secrets_path=secrets_path)

    global_static_labels = {str(k): str(v) for k, v in (raw.get("labels", {}) or {}).items()}
    nodes = []
    static_label_keys: set[str] = set(global_static_labels)
    for index, node in enumerate(raw.get("nodes", []), start=1):
        node_static_labels = {str(k): str(v) for k, v in (node.get("labels", {}) or {}).items()}
        static_labels = {**global_static_labels, **node_static_labels}
        static_label_keys.update(static_labels)
        filters_raw = node.get("filters", {}) or {}
        nodes.append(
            NodeConfig(
                name=node.get("name") or f"node{index}",
                host=node["host"],
                port=int(node.get("port", 6053)),
                password=node.get("password", ""),
                noise_psk=node.get("noise_psk"),
                static_labels=static_labels,
                filters=FilterConfig(
                    include_object_ids=list(filters_raw.get("include_object_ids", []) or []),
                    exclude_object_ids=list(filters_raw.get("exclude_object_ids", []) or []),
                    include_device_classes=list(filters_raw.get("include_device_classes", []) or []),
                    exclude_device_classes=list(filters_raw.get("exclude_device_classes", []) or []),
                    include_name_regex=str(filters_raw.get("include_name_regex", "") or ""),
                    exclude_name_regex=str(filters_raw.get("exclude_name_regex", "") or ""),
                ),
            )
        )

    exporter_config = ExporterConfig(
        listen_port=int(raw.get("listen_port", 9108)),
        emit_raw_metrics=bool(raw.get("emit_raw_metrics", raw.get("emit_raw_numeric_metrics", False))),
        include_created_metrics=bool(raw.get("include_created_metrics", False)),
        keepalive_seconds=float(raw.get("keepalive_seconds", 20.0)),
        keepalive_timeout_ratio=float(raw.get("keepalive_timeout_ratio", 4.5)),
        static_label_keys=sorted(static_label_keys),
    )
    if not nodes:
        raise ValueError("No nodes configured. Add entries under 'nodes:' in the YAML file.")
    return exporter_config, nodes


def apply_aioesphomeapi_tuning(exporter_config: ExporterConfig) -> None:
    aio_connection.KEEP_ALIVE_TIMEOUT_RATIO = exporter_config.keepalive_timeout_ratio


def apply_prometheus_tuning(exporter_config: ExporterConfig) -> None:
    if exporter_config.include_created_metrics:
        prometheus_client.enable_created_metrics()
    else:
        prometheus_client.disable_created_metrics()


def node_identity(node: NodeConfig) -> NodeIdentity:
    return node.name, node.host, node.port
