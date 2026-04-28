"""
work_program_agent.py
---------------------
Per-row test execution agent for the Protiviti Operational Audit Assistant.

Each row in the work program is handled by a single orchestrator agent that:
1. Reads the test row
2. Searches and reads relevant evidence wiki pages
3. Reads relevant guidance documents
4. Forms a conclusion with structured citations
5. Writes the conclusion back to the database
"""

import json
import logging
import re
import uuid
from datetime import datetime, timezone
from pathlib import Path

from core.llm_client import get_llm_client, get_llm_model, build_completion_kwargs, build_response_format_kwargs
from core.token_tracker import record_usage

logger = logging.getLogger(__name__)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def search_wiki(audit_id: str, query: str, db_conn, limit: int = 8) -> list[dict]:
    """Simple keyword search over wiki page titles and content."""
    terms = query.lower().split()
    rows = db_conn.execute(
        "SELECT id, page_type, title, content FROM wiki_pages WHERE audit_id=?",
        (audit_id,)
    ).fetchall()

    scored = []
    for row in rows:
        text = (row["title"] + " " + row["content"]).lower()
        score = sum(text.count(t) for t in terms)
        if score > 0:
            scored.append((score, dict(row)))

    scored.sort(key=lambda x: x[0], reverse=True)
    return [item[1] for item in scored[:limit]]


def run_test_row(row_id: str, audit_id: str, db_conn) -> None:
    """
    Execute the agent loop for a single work program row.
    Updates the row with conclusion, citations, status, and open questions.
    """
    row = db_conn.execute(
        "SELECT * FROM work_program_rows WHERE id=? AND audit_id=?",
        (row_id, audit_id)
    ).fetchone()

    if not row:
        logger.error("run_test_row: row not found id=%s", row_id)
        return

    # Skip verified rows
    if row["verified"]:
        logger.info("Skipping verified row: %s", row_id)
        return

    # Mark as running
    db_conn.execute(
        "UPDATE work_program_rows SET status='running', updated_at=? WHERE id=?",
        (_now(), row_id)
    )
    db_conn.commit()

    try:
        # Search for relevant evidence
        search_query = f"{row['description']} {row['objective'] or ''}"
        relevant_pages = search_wiki(audit_id, search_query, db_conn)

        evidence_context = "\n\n".join(
            f"[WIKI PAGE: {p['title']} ({p['page_type']})]\n{p['content'][:1500]}"
            for p in relevant_pages
        ) or "No relevant evidence pages found."

        # Get guidance documents
        guidance_sources = db_conn.execute(
            """SELECT filename, markdown_path FROM sources
               WHERE audit_id=? AND file_type='guidance' AND status='ready'""",
            (audit_id,)
        ).fetchall()

        guidance_context = ""
        for gs in guidance_sources[:2]:  # Limit to 2 guidance docs to control tokens
            if gs["markdown_path"] and Path(gs["markdown_path"]).exists():
                content = Path(gs["markdown_path"]).read_text(encoding="utf-8")[:3000]
                guidance_context += f"\n\n[GUIDANCE: {gs['filename']}]\n{content}"

        client = get_llm_client()
        model = get_llm_model()

        prompt = f"""You are an experienced internal auditor executing a test from an audit work program.

TEST DETAILS:
- Test ID: {row['test_id']}
- Description: {row['description']}
- Objective: {row['objective'] or 'Not specified'}

RELEVANT EVIDENCE FROM WIKI:
{evidence_context}

RELEVANT GUIDANCE:
{guidance_context if guidance_context else "No guidance documents available."}

Execute this test by:
1. Reviewing the evidence against the test objective
2. Forming a clear conclusion
3. Identifying any gaps or open questions

Return a JSON object with these exact keys:
- "status": one of "completed" | "pending_evidence" | "open_questions" | "error"
- "conclusion": markdown string — what was found and the test result (be specific, cite evidence)
- "evidence_references": array of objects, each with "source_file", "location", "quote"
- "open_questions": array of strings — items only a human can answer
- "requested_evidence": array of strings — specific documents needed to complete the test

Return ONLY valid JSON."""

        resp = client.chat.completions.create(
            model=model,
            messages=[{"role": "user", "content": prompt}],
            **build_completion_kwargs(max_tokens=2500, temperature=0.2),
            **build_response_format_kwargs(),
        )

        record_usage(audit_id, "work_program", resp.usage, db_conn)

        raw = resp.choices[0].message.content or "{}"
        try:
            result = json.loads(raw)
        except json.JSONDecodeError:
            match = re.search(r"\{.*\}", raw, re.DOTALL)
            result = json.loads(match.group(0)) if match else {}

        db_conn.execute(
            """UPDATE work_program_rows
               SET status=?, conclusion=?, evidence_references=?,
                   open_questions=?, requested_evidence=?, last_run_at=?, updated_at=?
               WHERE id=?""",
            (
                result.get("status", "completed"),
                result.get("conclusion", ""),
                json.dumps(result.get("evidence_references", [])),
                json.dumps(result.get("open_questions", [])),
                json.dumps(result.get("requested_evidence", [])),
                _now(), _now(), row_id
            )
        )
        db_conn.commit()
        logger.info("Test row complete: row_id=%s status=%s", row_id, result.get("status"))

    except Exception as exc:
        logger.exception("Test row failed: row_id=%s", row_id)
        db_conn.execute(
            "UPDATE work_program_rows SET status='error', conclusion=?, updated_at=? WHERE id=?",
            (f"Agent error: {exc}", _now(), row_id)
        )
        db_conn.commit()
