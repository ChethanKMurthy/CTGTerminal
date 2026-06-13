"""Knowledge-graph persistence using NetworkX (free, embedded, no server).

The graph is held in memory and persisted to a pickle on disk. Nodes carry a
`kind` (stock/sector/index/macro/commodity/fx/theme) and arbitrary metrics;
edges carry a `relation` and weight.
"""
from __future__ import annotations

import pickle
import threading
from pathlib import Path
from typing import Any

import networkx as nx

from ..config import DATA_DIR
from ..logging_conf import get_logger

log = get_logger("storage.graph")

GRAPH_PATH: Path = DATA_DIR / "knowledge_graph.gpickle"
_lock = threading.Lock()


def load_graph() -> nx.MultiDiGraph:
    if GRAPH_PATH.exists():
        try:
            with open(GRAPH_PATH, "rb") as fh:
                g = pickle.load(fh)
            if isinstance(g, nx.MultiDiGraph):
                return g
        except Exception as exc:  # noqa: BLE001
            log.warning("Failed to load graph (%s); starting fresh", exc)
    return nx.MultiDiGraph()


def save_graph(g: nx.MultiDiGraph) -> None:
    with _lock:
        tmp = GRAPH_PATH.with_suffix(".tmp")
        with open(tmp, "wb") as fh:
            pickle.dump(g, fh)
        tmp.replace(GRAPH_PATH)
    log.info("Graph saved: %d nodes, %d edges", g.number_of_nodes(), g.number_of_edges())


def graph_to_dict(g: nx.MultiDiGraph, max_edges: int = 1200) -> dict[str, Any]:
    """Serialise to a vis-friendly dict for the dashboard."""
    nodes = [{"id": n, **{k: _safe(v) for k, v in d.items()}} for n, d in g.nodes(data=True)]
    edges = []
    for u, v, d in g.edges(data=True):
        edges.append(
            {
                "source": u,
                "target": v,
                "relation": d.get("relation", "rel"),
                "weight": round(float(d.get("weight", 1.0)), 3),
            }
        )
        if len(edges) >= max_edges:
            break
    return {"nodes": nodes, "edges": edges}


def _safe(v: Any) -> Any:
    try:
        import math

        if isinstance(v, float) and (math.isnan(v) or math.isinf(v)):
            return None
    except Exception:  # noqa: BLE001
        pass
    return v
