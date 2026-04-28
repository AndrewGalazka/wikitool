"""
chat_agent.py
-------------
Chat agent for the Protiviti Operational Audit Assistant.

Supports two mutually exclusive scopes:
- evidence: searches and reads the evidence wiki
- guidance: reads guidance documents

Evidence-scope responses can be promoted to wiki pages.
"""

import json
import logging
import re
import uuid
from datetime import datetime, timezone
from pathlib import Path

from core.llm_client import get_llm_client, get_llm_model, build_completion_kwargs, build_response_format_kwargs
from core.token_tracker import record_usage
from core.work_program_agent import search_wiki

logger = logging.getLogger(__name__)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def chat_response(audit_id: str, scope: str, user_message: str, history: list[dict], db_conn) -> dict:
    """
    Generate a chat response for the given scope.

    Returns:
        dict with keys: content (str), citations (list), message_id (str)
    """
    client = get_llm_client()
    model = get_llm_model()

    if scope == "evidence":
        context = _build_evidence_context(audit_id, user_message, db_conn)
        system_prompt = """You are an audit assistant with access to the evidence wiki for this audit.
Answer questions based only on the evidence provided. For every factual claim, include a citation
in the format [Source: <page_title>]. Be concise and precise."""
    else:
        context = _build_guidance_context(audit_id, db_conn)
        system_prompt = """You are an audit assistant with access to the guidance documents for this audit.
Answer questions based only on the guidance provided. For every claim, cite the document and section
in the format [Guidance: <document_name>, <section>]. Be precise and do not synthesize beyond what is written."""

    messages = [{"role": "system", "content": system_prompt}]

    # Add recent history (last 6 exchanges)
    for msg in history[-12:]:
        messages.append({"role": msg["role"], "content": msg["content"]})

    messages.append({
        "role": "user",
        "content": f"CONTEXT:\n{context}\n\nQUESTION: {user_message}"
    })

    resp = client.chat.completions.create(
        model=model,
        messages=messages,
        **build_completion_kwargs(max_tokens=1500, temperature=0.3),
    )

    record_usage(audit_id, "chat", resp.usage, db_conn)

    content = resp.choices[0].message.content or ""

    # Extract citations from response text
    citations = re.findall(r"\[(?:Source|Guidance): ([^\]]+)\]", content)

    message_id = str(uuid.uuid4())
    return {
        "message_id": message_id,
        "content": content,
        "citations": citations,
    }


def _build_evidence_context(audit_id: str, query: str, db_conn) -> str:
    """Search wiki and build context string for evidence-scope chat."""
    pages = search_wiki(audit_id, query, db_conn, limit=6)
    if not pages:
        return "No relevant evidence pages found in the wiki."
    return "\n\n".join(
        f"[{p['page_type'].upper()}] {p['title']}\n{p['content'][:1200]}"
        for p in pages
    )


def _build_guidance_context(audit_id: str, db_conn) -> str:
    """Load guidance documents and build context string for guidance-scope chat."""
    sources = db_conn.execute(
        """SELECT filename, markdown_path FROM sources
           WHERE audit_id=? AND file_type='guidance' AND status='ready'""",
        (audit_id,)
    ).fetchall()

    if not sources:
        return "No guidance documents have been uploaded for this audit."

    context_parts = []
    for src in sources:
        if src["markdown_path"] and Path(src["markdown_path"]).exists():
            content = Path(src["markdown_path"]).read_text(encoding="utf-8")[:4000]
            context_parts.append(f"[GUIDANCE DOCUMENT: {src['filename']}]\n{content}")

    return "\n\n".join(context_parts) or "Guidance documents could not be read."


def promote_to_wiki(audit_id: str, message_id: str, db_conn) -> str | None:
    """
    Promote a chat response to a new wiki synthesis page.
    Returns the new page ID or None on failure.
    """
    msg = db_conn.execute(
        "SELECT content FROM chat_messages WHERE id=? AND audit_id=? AND scope='evidence'",
        (message_id, audit_id)
    ).fetchone()

    if not msg:
        return None

    page_id = str(uuid.uuid4())
    title = f"Chat Synthesis — {_now()[:10]}"
    db_conn.execute(
        """INSERT INTO wiki_pages (id, audit_id, page_type, title, content, metadata, created_at, updated_at)
           VALUES (?, ?, 'evidence_area', ?, ?, '{}', ?, ?)""",
        (page_id, audit_id, title, msg["content"], _now(), _now())
    )
    db_conn.commit()
    return page_id
