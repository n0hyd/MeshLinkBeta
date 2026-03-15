"""
Mesh Stats plugin

Provides a compact $stats summary using the existing node_tracking SQLite data.
This stays read-only and does not schedule any background activity.
"""

from __future__ import annotations

import argparse
import os
import sqlite3
import sys
from datetime import UTC, datetime, timedelta
from typing import List, Optional, Sequence

if __package__ in (None, ""):
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import cfg

if __name__ != "__main__":
    import plugins
    import plugins.libcommand as LibCommand
    import plugins.liblogger as logger
else:
    class _CliLogger:
        @staticmethod
        def info(message):
            print(message, file=sys.stderr)

    logger = _CliLogger()
    plugins = None
    LibCommand = None


DEFAULT_CONFIG = {
    "enabled": True,
    "command_name": "stats",
    "report_title": "Flyover Mesh Status",
    "active_window_hours": 1,
    "last_heard_count": 5,
    "exclude_host_node": True,
    "host_node_id": "",
    "testing_mode": False,
    "debug_logging": False,
}


class HeardNode:
    def __init__(self, node_id: str, display_name: str, last_seen_utc: Optional[datetime]):
        self.node_id = node_id
        self.display_name = display_name
        self.last_seen_utc = last_seen_utc


class MeshStatsSnapshot:
    def __init__(self, total_nodes: int, active_nodes: int, last_heard: List[HeardNode]):
        self.total_nodes = total_nodes
        self.active_nodes = active_nodes
        self.last_heard = last_heard


def _utcnow() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


def _parse_iso8601(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None

    normalized = value.strip()
    if normalized.endswith("Z"):
        normalized = normalized[:-1] + "+00:00"

    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError:
        return None

    if parsed.tzinfo is not None:
        return parsed.astimezone(UTC).replace(tzinfo=None)
    return parsed


def _merge_config(runtime_config: Optional[dict] = None) -> dict:
    merged = dict(DEFAULT_CONFIG)
    if runtime_config:
        merged.update(runtime_config)
    return merged


def _node_database_path(config_data: dict) -> str:
    node_tracking = config_data.get("node_tracking", {})
    return node_tracking.get("database_path", "./nodes.db")


def _debug(enabled: bool, message: str) -> None:
    if enabled:
        logger.info(f"[meshstats] {message}")


def _resolve_host_node_id(interface, config_data: dict) -> str:
    meshstats_config = _merge_config(config_data.get("meshstats"))
    configured = str(meshstats_config.get("host_node_id", "") or "").strip()
    if configured:
        return configured

    if interface is None:
        return ""

    try:
        my_info = interface.getMyNodeInfo() or {}
        user = my_info.get("user", {})
        return str(user.get("id") or "").strip()
    except Exception:
        return ""


def _fetch_snapshot(config_data: dict, host_node_id: str = "") -> MeshStatsSnapshot:
    meshstats_config = _merge_config(config_data.get("meshstats"))
    debug_logging = bool(meshstats_config.get("debug_logging", False))
    db_path = _node_database_path(config_data)

    if not os.path.exists(db_path):
        _debug(debug_logging, f"database not found at {db_path}")
        return MeshStatsSnapshot(0, 0, [])

    active_cutoff = (_utcnow() - timedelta(hours=float(meshstats_config["active_window_hours"]))).isoformat()
    last_heard_count = max(1, int(meshstats_config.get("last_heard_count", 5)))
    exclude_host_node = bool(meshstats_config.get("exclude_host_node", True))

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row

    try:
        cursor = conn.cursor()

        # Schema assumptions:
        # - nodes.last_seen_utc is the correct source for "active" and "last heard".
        # - name resolution prefers long_name, then short_name, then raw node_id.
        # - ignored, airplane, and MQTT-only rows are excluded from the report.
        base_where = """
            FROM nodes
            WHERE COALESCE(is_ignored, 0) = 0
              AND COALESCE(is_airplane, 0) = 0
              AND COALESCE(is_mqtt, 0) = 0
        """

        cursor.execute(f"SELECT COUNT(*) AS count {base_where}")
        total_nodes = int(cursor.fetchone()["count"])

        cursor.execute(f"SELECT COUNT(*) AS count {base_where} AND last_seen_utc >= ?", (active_cutoff,))
        active_nodes = int(cursor.fetchone()["count"])

        last_heard_where = base_where
        params: List[object] = []
        if exclude_host_node and host_node_id:
            last_heard_where += " AND node_id != ?"
            params.append(host_node_id)

        params.append(last_heard_count)
        cursor.execute(
            f"""
            SELECT
                node_id,
                COALESCE(NULLIF(long_name, ''), NULLIF(short_name, ''), node_id) AS display_name,
                last_seen_utc
            {last_heard_where}
            ORDER BY last_seen_utc DESC
            LIMIT ?
            """,
            tuple(params),
        )

        last_heard = [
            HeardNode(
                node_id=row["node_id"],
                display_name=row["display_name"],
                last_seen_utc=_parse_iso8601(row["last_seen_utc"]),
            )
            for row in cursor.fetchall()
        ]

        _debug(
            debug_logging,
            f"report total_nodes={total_nodes} active_nodes={active_nodes} last_heard={len(last_heard)} host_excluded={bool(host_node_id and exclude_host_node)}",
        )

        return MeshStatsSnapshot(total_nodes, active_nodes, last_heard)
    finally:
        conn.close()


def build_stats_report(config_data: dict, interface=None) -> str:
    meshstats_config = _merge_config(config_data.get("meshstats"))
    title = str(meshstats_config["report_title"])
    host_node_id = _resolve_host_node_id(interface, config_data)
    snapshot = _fetch_snapshot(config_data, host_node_id=host_node_id)

    if snapshot.total_nodes <= 0:
        return f"{title}\nNo node data available yet."

    heard_names = ", ".join(node.display_name for node in snapshot.last_heard) or "None"
    active_hours = float(meshstats_config["active_window_hours"])
    active_label = f"{active_hours:g}h"

    return "\n".join(
        [
            title,
            f"Total Nodes: {snapshot.total_nodes}",
            f"Active ({active_label}): {snapshot.active_nodes}",
            "Last 5 Heard:" if int(meshstats_config.get("last_heard_count", 5)) == 5 else f"Last {int(meshstats_config.get('last_heard_count', 5))} Heard:",
            heard_names,
        ]
    )


def _load_config_file(config_path: str) -> dict:
    import yaml

    with open(config_path, "r", encoding="utf-8") as handle:
        return yaml.safe_load(handle) or {}


if plugins is not None:
    class MeshStats(plugins.Base):
        def __init__(self):
            self._config = DEFAULT_CONFIG

        def start(self):
            self._config = _merge_config(cfg.config.get("meshstats"))

            if not self._config.get("enabled", True):
                logger.info("Mesh stats plugin disabled in config")
                return

            command_name = str(self._config.get("command_name", "stats")).strip() or "stats"
            logger.info(f"Loading mesh stats plugin ({command_name})")

            if self._config.get("testing_mode"):
                _debug(bool(self._config.get("debug_logging")), "testing mode enabled")

            LibCommand.simpleCommand().registerCommand(
                command_name,
                "Show a compact mesh activity summary",
                self._handle_stats,
            )

        def _handle_stats(self, packet, interface, client, args):
            del packet, client, args
            return build_stats_report(cfg.config, interface=interface)


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Render the Mesh Stats report from an existing nodes.db file.")
    parser.add_argument("--config", default="config.yml", help="Path to MeshLinkBeta config file")
    args = parser.parse_args(argv)

    config_data = _load_config_file(args.config)
    print(build_stats_report(config_data))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
