"""
wiki_agent.py
-------------
Evidence wiki synthesis agent for the Protiviti Operational Audit Assistant.

After a file is converted to markdown, this agent:
1. Reads the markdown content
2. Reviews existing wiki pages for the audit
3. Creates or updates wiki pages organized around audit dimensions
4. Runs a lint pass to detect contradictions and gaps
"""

import json
import logging
import uuid
from datetime import datetime, timezone
from pathlib import Path

from core.llm_client import get_llm_client, get_llm_model, build_completion_kwargs, build_response_format_kwargs
from core.token_tracker import record_usage

logger = logging.getLogger(__name__)

PAGE_TYPES = ["source", "person", "process", "control", "system", "evidence_area", "finding"]


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def synthesize_evidence(source_id: str, audit_id: str, db_conn) -> None:
    """
    Read a converted source and synthesize/update evidence wiki pages.
    Called after successful ingestion.
    """
    source = db_conn.execute(
        "SELECT * FROM sources WHERE id=?", (source_id,)
    ).fetchone()

    if not source or source["status"] != "ready" or not source["markdown_path"]:
        logger.warning("synthesize_evidence: source not ready id=%s", source_id)
        return

    md_content = Path(source["markdown_path"]).read_text(encoding="utf-8")
    # Truncate very large files to avoid token overflow — use first 12000 chars
    md_excerpt = md_content[:12000]

    # Get existing wiki page summaries for context
    existing_pages = db_conn.execute(
        "SELECT id, page_type, title, content FROM wiki_pages WHERE audit_id=?",
        (audit_id,)
    ).fetchall()
    existing_summary = "\n".join(
        f"[{p['page_type']}] {p['title']}: {p['content'][:300]}..."
        for p in existing_pages[:20]
    )

    client = get_llm_client()
    model = get_llm_model()

    prompt = f"""You are an audit knowledge base assistant. A new evidence file has been ingested.

FILE: {source['filename']}
CONTENT (excerpt):
{md_excerpt}

EXISTING WIKI PAGES (summary):
{existing_summary if existing_summary else "None yet."}

Your task: Analyze the file content and produce a JSON array of wiki page operations.
Each operation must have:
- "action": "create" or "update"
- "page_type": one of {PAGE_TYPES}
- "title": concise page title
- "content": full markdown content for the page
- "metadata": object with keys: related_entities (list), links (list of titles), sources (list of filenames)
- "page_id": (only for "update") the ID of the existing page to update

Rules:
- Always create a "source" page for this file
- Create pages for any people, processes, controls, systems, or evidence areas identified
- If a finding is clearly identified, create a "finding" page
- For updates, merge new information into existing content rather than replacing it
- Keep content factual and cite the source filename

Return ONLY a valid JSON array. No explanation."""

    resp = client.chat.completions.create(
        model=model,
        messages=[{"role": "user", "content": prompt}],
        **build_completion_kwargs(max_tokens=3000, temperature=0.1),
        **build_response_format_kwargs(),
    )

    record_usage(audit_id, "ingestion", resp.usage, db_conn)

    raw = resp.choices[0].message.content or "[]"
    try:
        operations = json.loads(raw)
    except json.JSONDecodeError:
        import re
        match = re.search(r"\[.*\]", raw, re.DOTALL)
        operations = json.loads(match.group(0)) if match else []

    for op in operations:
        try:
            action = op.get("action", "create")
            if action == "create":
                page_id = str(uuid.uuid4())
                db_conn.execute(
                    """INSERT INTO wiki_pages (id, audit_id, page_type, title, content, metadata, created_at, updated_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                    (page_id, audit_id, op.get("page_type", "source"),
                     op.get("title", "Untitled"), op.get("content", ""),
                     json.dumps(op.get("metadata", {})), _now(), _now())
                )
            elif action == "update" and op.get("page_id"):
                db_conn.execute(
                    "UPDATE wiki_pages SET content=?, metadata=?, updated_at=? WHERE id=? AND audit_id=?",
                    (op.get("content", ""), json.dumps(op.get("metadata", {})),
                     _now(), op["page_id"], audit_id)
                )
        except Exception as exc:
            logger.error("Wiki operation failed: %s — %s", op, exc)

    db_conn.commit()
    logger.info("Wiki synthesis complete: source_id=%s, operations=%d", source_id, len(operations))

    # Run lint pass after every ingestion
    run_lint_pass(audit_id, db_conn)


def run_lint_pass(audit_id: str, db_conn) -> None:
    """
    Scan the evidence wiki for contradictions, gaps, and unresolved references.
    Inserts lint_issues records for any problems found.
    """
    pages = db_conn.execute(
        "SELECT id, page_type, title, content FROM wiki_pages WHERE audit_id=?",
        (audit_id,)
    ).fetchall()

    if not pages:
        return

    pages_summary = "\n".join(
        f"[ID:{p['id']}][{p['page_type']}] {p['title']}: {p['content'][:500]}"
        for p in pages
    )

    client = get_llm_client()
    model = get_llm_model()

    prompt = f"""You are an audit quality reviewer. Review the following evidence wiki pages and identify issues.

WIKI PAGES:
{pages_summary}

Identify:
1. CONTRADICTIONS — two pages that state conflicting facts
2. GAPS — a referenced entity (person, system, control) that has no wiki page
3. UNRESOLVED_REFERENCES — a finding or claim that lacks supporting evidence pages

Return a JSON array of issues. Each issue:
- "issue_type": "contradiction" | "gap" | "unresolved_reference"
- "description": clear description of the issue
- "page_ids": list of affected page IDs (use the [ID:...] values above)

Return ONLY valid JSON. If no issues found, return [].
"""

    resp = client.chat.completions.create(
        model=model,
        messages=[{"role": "user", "content": prompt}],
        **build_completion_kwargs(max_tokens=2000, temperature=0.1),
        **build_response_format_kwargs(),
    )

    record_usage(audit_id, "lint", resp.usage, db_conn)

    raw = resp.choices[0].message.content or "[]"
    try:
        issues = json.loads(raw)
    except json.JSONDecodeError:
        import re
        match = re.search(r"\[.*\]", raw, re.DOTALL)
        issues = json.loads(match.group(0)) if match else []

    # Clear old unresolved issues for this audit before inserting new ones
    db_conn.execute(
        "DELETE FROM lint_issues WHERE audit_id=? AND resolved=0", (audit_id,)
    )

    for issue in issues:
        issue_id = str(uuid.uuid4())
        page_ids = issue.get("page_ids", [])
        primary_page = page_ids[0] if page_ids else None
        db_conn.execute(
            """INSERT INTO lint_issues (id, audit_id, page_id, issue_type, description, source_pages, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (issue_id, audit_id, primary_page,
             issue.get("issue_type", "gap"),
             issue.get("description", ""),
             json.dumps(page_ids), _now())
        )

    db_conn.commit()
    logger.info("Lint pass complete: audit_id=%s, issues=%d", audit_id, len(issues))
