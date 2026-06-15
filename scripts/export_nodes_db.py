#!/usr/bin/env python3
"""Export MeshLink data from MeshMerge API routes to per-dataset CSV files and one JSON file."""

from __future__ import annotations

import argparse
import csv
import json
from pathlib import Path
from typing import Any
from urllib import error, parse, request


DEFAULT_API_URL = "https://meshmerge.fly.dev"
def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Export MeshLink data from MeshMerge API routes to CSV files and a full JSON dump."
    )
    parser.add_argument(
        "--api-url",
        default=DEFAULT_API_URL,
        help=f"Base API URL. Default: {DEFAULT_API_URL}",
    )
    parser.add_argument(
        "--token",
        default=None,
        help="Bearer token for API authentication.",
    )
    parser.add_argument(
        "--csv-dir",
        default="export_csv",
        help="Directory for per-dataset CSV exports. Default: export_csv",
    )
    parser.add_argument(
        "--json-out",
        default="full_export.json",
        help="Output path for the combined JSON export. Default: full_export.json",
    )
    parser.add_argument(
        "--timeout",
        type=int,
        default=60,
        help="HTTP timeout in seconds. Default: 60",
    )
    parser.add_argument(
        "--packet-limit",
        type=int,
        default=100,
        help="How many recent packets to fetch per node. Default: 100",
    )
    parser.add_argument(
        "--traceroute-limit",
        type=int,
        default=1000,
        help="How many traceroutes to fetch from the global endpoint. Default: 1000",
    )
    return parser.parse_args()


def build_url(api_url: str, path: str, params: dict[str, Any] | None = None) -> str:
    url = f"{api_url.rstrip('/')}/{path.lstrip('/')}"
    if params:
        query = parse.urlencode({key: value for key, value in params.items() if value is not None})
        if query:
            url = f"{url}?{query}"
    return url


def fetch_json(url: str, token: str | None, timeout: int) -> dict[str, Any]:
    headers = {
        "Accept": "application/json",
    }
    if token:
        headers["Authorization"] = f"Bearer {token}"

    req = request.Request(url, headers=headers, method="GET")
    try:
        with request.urlopen(req, timeout=timeout) as response:
            charset = response.headers.get_content_charset() or "utf-8"
            payload = response.read().decode(charset)
    except error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"API request failed with HTTP {exc.code}: {detail}") from exc
    except error.URLError as exc:
        raise RuntimeError(f"API request failed: {exc.reason}") from exc

    data = json.loads(payload)
    if not isinstance(data, dict):
        raise ValueError("Expected top-level JSON object from API")
    return data


def get_items(payload: dict[str, Any], items_key: str) -> list[dict[str, Any]]:
    items = payload.get(items_key)
    if items is None:
        return []
    if not isinstance(items, list):
        raise ValueError(f"Expected list in response field '{items_key}'")
    return [item for item in items if isinstance(item, dict)]


def get_object(payload: dict[str, Any], object_key: str) -> dict[str, Any]:
    value = payload.get(object_key)
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise ValueError(f"Expected object in response field '{object_key}'")
    return value


def flatten_dict(value: dict[str, Any], prefix: str = "") -> dict[str, Any]:
    flat: dict[str, Any] = {}

    for key, item in value.items():
        flat_key = f"{prefix}.{key}" if prefix else key
        if isinstance(item, dict):
            flat.update(flatten_dict(item, flat_key))
        elif isinstance(item, list):
            flat[flat_key] = json.dumps(item, ensure_ascii=False)
        else:
            flat[flat_key] = item

    return flat


def export_from_routes(
    api_url: str,
    token: str | None,
    timeout: int,
    packet_limit: int,
    traceroute_limit: int,
) -> dict[str, list[dict[str, Any]]]:
    export_data: dict[str, list[dict[str, Any]]] = {}

    nodes_payload = fetch_json(build_url(api_url, "/api/nodes"), token, timeout)
    nodes = get_items(nodes_payload, "nodes")
    export_data["nodes"] = [flatten_dict(node) for node in nodes]

    packets: list[dict[str, Any]] = []
    neighbors: list[dict[str, Any]] = []

    for node in nodes:
        node_id = node.get("node_id")
        if not node_id:
            continue

        packets_payload = fetch_json(
            build_url(api_url, f"/api/nodes/{node_id}/packets", {"limit": packet_limit}),
            token,
            timeout,
        )
        for packet in get_items(packets_payload, "packets"):
            packets.append(flatten_dict(packet))

        neighbors_payload = fetch_json(
            build_url(api_url, f"/api/nodes/{node_id}/neighbors"),
            token,
            timeout,
        )
        for neighbor in get_items(neighbors_payload, "neighbors"):
            neighbors.append(flatten_dict(neighbor))

    if packets:
        export_data["packets"] = packets
    if neighbors:
        export_data["neighbors"] = neighbors

    topology_payload = fetch_json(build_url(api_url, "/api/topology"), token, timeout)
    topology_links = get_items(topology_payload, "links")
    if topology_links:
        export_data["topology"] = [flatten_dict(link) for link in topology_links]

    traceroutes_payload = fetch_json(
        build_url(api_url, "/api/traceroutes", {"limit": traceroute_limit}),
        token,
        timeout,
    )
    traceroutes = get_items(traceroutes_payload, "traceroutes")
    if traceroutes:
        export_data["traceroutes"] = [flatten_dict(trace) for trace in traceroutes]

    stats_payload = fetch_json(build_url(api_url, "/api/stats"), token, timeout)
    statistics = get_object(stats_payload, "statistics")
    if statistics:
        export_data["statistics"] = [flatten_dict(statistics)]

    export_data["metadata"] = [
        {
            "source_api_url": api_url.rstrip("/"),
            "packet_limit_per_node": packet_limit,
            "traceroute_limit": traceroute_limit,
            "node_count": len(export_data.get("nodes", [])),
            "packet_count": len(export_data.get("packets", [])),
            "neighbor_count": len(export_data.get("neighbors", [])),
            "topology_count": len(export_data.get("topology", [])),
            "traceroute_count": len(export_data.get("traceroutes", [])),
        }
    ]

    return export_data


def write_csv(csv_dir: Path, dataset_name: str, rows: list[dict[str, Any]]) -> None:
    csv_dir.mkdir(parents=True, exist_ok=True)
    output_path = csv_dir / f"{dataset_name}.csv"

    with output_path.open("w", newline="", encoding="utf-8") as handle:
        if not rows:
            handle.write("")
            return

        fieldnames = sorted({key for row in rows for key in row.keys()})
        writer = csv.DictWriter(handle, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def write_json(json_path: Path, data: dict[str, list[dict[str, Any]]]) -> None:
    with json_path.open("w", encoding="utf-8") as handle:
        json.dump(data, handle, indent=2)


def main() -> int:
    args = parse_args()
    csv_dir = Path(args.csv_dir)
    json_path = Path(args.json_out)
    export_data = export_from_routes(
        args.api_url,
        args.token,
        args.timeout,
        args.packet_limit,
        args.traceroute_limit,
    )

    for dataset_name, rows in export_data.items():
        write_csv(csv_dir, dataset_name, rows)

    write_json(json_path, export_data)

    print(f"Fetched data from {args.api_url.rstrip('/')}")
    print(f"Exported {len(export_data)} dataset(s)")
    print(f"CSV directory: {csv_dir}")
    print(f"JSON file: {json_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
