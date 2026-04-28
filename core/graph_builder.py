"""
graph_builder.py
----------------
Knowledge graph derivation from the evidence wiki.
Graph is rebuilt on demand (manual refresh) — never stored independently.
"""

import json
import logging
from pathlib import Path

logger = logging.getLogger(__name__)

# Visual color scheme per page type
PAGE_TYPE_COLORS = {
    "source":        "#4A90D9",
    "person":        "#7ED321",
    "process":       "#F5A623",
    "control":       "#9B59B6",
    "system":        "#1ABC9C",
    "evidence_area": "#E74C3C",
    "finding":       "#E91E63",
}


def build_graph(audit_id: str, db_conn) -> dict:
    """
    Derive a graph from wiki pages and lint issues.

    Returns a dict with:
    - nodes: list of {id, label, type, color, has_issues}
    - edges: list of {source, target, label}
    - issues: list of active lint issues
    """
    pages = db_conn.execute(
        "SELECT id, page_type, title, metadata FROM wiki_pages WHERE audit_id=?",
        (audit_id,)
    ).fetchall()

    issues = db_conn.execute(
        "SELECT page_id, issue_type, description FROM lint_issues WHERE audit_id=? AND resolved=0",
        (audit_id,)
    ).fetchall()

    issue_page_ids = {i["page_id"] for i in issues if i["page_id"]}

    nodes = []
    edges = []
    page_title_to_id = {p["title"]: p["id"] for p in pages}

    for page in pages:
        nodes.append({
            "id": page["id"],
            "label": page["title"],
            "type": page["page_type"],
            "color": PAGE_TYPE_COLORS.get(page["page_type"], "#95A5A6"),
            "has_issues": page["id"] in issue_page_ids,
        })

        # Build edges from metadata links
        try:
            meta = json.loads(page["metadata"] or "{}")
            links = meta.get("links", [])
            for linked_title in links:
                target_id = page_title_to_id.get(linked_title)
                if target_id and target_id != page["id"]:
                    edges.append({
                        "source": page["id"],
                        "target": target_id,
                        "label": "links to",
                    })
        except (json.JSONDecodeError, TypeError):
            pass

    return {
        "nodes": nodes,
        "edges": edges,
        "issues": [
            {
                "page_id": i["page_id"],
                "issue_type": i["issue_type"],
                "description": i["description"],
            }
            for i in issues
        ],
    }
