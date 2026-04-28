"""
chat_agent.py
-------------
Agentic query loop for the Protiviti Operational Audit Assistant.

Implements the Karpathy LLM-Wiki query pattern:

  Step 1 — Index scan:
    The LLM reads index.md and selects the 4-6 most relevant page slugs
    for the user's question. This avoids loading the entire wiki on every query.

  Step 2 — Page loading + link traversal:
    The selected pages are loaded. Any [[wiki-link]] references in those pages
    that are relevant to the question are followed one level deep (max 3 extra pages).

  Step 3 — Synthesis with slug-level citations:
    The LLM answers using only the loaded pages. Every factual claim is cited
    with the page slug in the format [wiki: <slug>] or [guidance: <filename>].

  Step 4 — Promote to wiki (manual):
    The user can promote a valuable answer to a new wiki page via the UI.
    The promote action writes a properly formatted page with frontmatter.

Supports two scopes:
  - evidence: queries the evidence wiki (steps 1-3 above)
  - guidance: queries guidance documents directly (no index step needed)
"""

import json
import logging
import re
import uuid
from datetime import datetime, timezone
from pathlib import Path

from core.llm_client import (
    get_llm_client,
    get_llm_model,
    build_completion_kwargs,
    build_response_format_kwargs,
)
from core.token_tracker import record_usage
from core.wiki_agent import get_index, rebuild_index, append_log, _today

logger = logging.getLogger(__name__)

# Maximum pages to load in a single query (prevents token overflow)
MAX_PAGES_PER_QUERY = 6
# Maximum extra pages to load via [[wiki-link]] traversal
MAX_TRAVERSAL_PAGES = 3


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


# ── Step 1: LLM-driven page selection from index ─────────────────────────────

def _select_pages_from_index(index_md: str, query: str, client, model, audit_id: str, db_conn) -> list[str]:
    """
    Ask the LLM to read index.md and return the slugs of the most relevant pages.
    Returns a list of slug strings (without type/ prefix).
    """
    if not index_md or index_md.strip() == "":
        return []

    selection_prompt = f"""You are navigating an audit knowledge base wiki.

INDEX.MD (full wiki catalog):
{index_md}

USER QUESTION:
{query}

Task: Read the index and identify the 4-6 wiki page slugs that are most relevant to answering this question.
Return ONLY a JSON array of slug strings. Example: ["access-review-process", "iam-system-overview"]
No explanation. No prose. Just the JSON array."""

    resp = client.chat.completions.create(
        model=model,
        messages=[
            {
                "role": "system",
                "content": (
                    "You are a JSON-only API. Return a valid JSON array of slug strings only. "
                    "Start with '[' and end with ']'. No markdown fences, no explanation."
                ),
            },
            {"role": "user", "content": selection_prompt},
        ],
        **build_completion_kwargs(max_tokens=300, temperature=0.0),
        **build_response_format_kwargs(),
    )

    record_usage(audit_id, "chat", resp.usage, db_conn)

    raw = (resp.choices[0].message.content or "[]").strip()
    raw = re.sub(r"^```(?:json)?\s*", "", raw)
    raw = re.sub(r"\s*```$", "", raw).strip()

    try:
        slugs = json.loads(raw)
        if isinstance(slugs, list):
            return [s for s in slugs if isinstance(s, str)][:MAX_PAGES_PER_QUERY]
    except json.JSONDecodeError:
        match = re.search(r"\[.*\]", raw, re.DOTALL)
        if match:
            try:
                slugs = json.loads(match.group(0))
                return [s for s in slugs if isinstance(s, str)][:MAX_PAGES_PER_QUERY]
            except json.JSONDecodeError:
                pass

    logger.warning("_select_pages_from_index: could not parse slug list from LLM. Raw: %s", raw[:200])
    return []


# ── Step 2: Page loading + [[wiki-link]] traversal ───────────────────────────

def _load_pages_by_slugs(audit_id: str, slugs: list[str], db_conn) -> list[dict]:
    """
    Load wiki pages matching the given slugs.
    Slug matching: checks metadata.id (type/slug) and the slug part after /.
    """
    if not slugs:
        return []

    all_pages = db_conn.execute(
        "SELECT id, page_type, title, content, metadata FROM wiki_pages WHERE audit_id=?",
        (audit_id,)
    ).fetchall()

    slug_set = set(s.lower() for s in slugs)
    matched = []
    for p in all_pages:
        try:
            meta = json.loads(p["metadata"] or "{}")
            full_id = meta.get("id", "").lower()
            short_slug = full_id.split("/")[-1] if "/" in full_id else full_id
        except Exception:
            full_id = ""
            short_slug = ""

        if short_slug in slug_set or full_id in slug_set:
            matched.append(dict(p))

    return matched


def _traverse_links(
    loaded_pages: list[dict],
    query: str,
    audit_id: str,
    db_conn,
    max_extra: int = MAX_TRAVERSAL_PAGES,
) -> list[dict]:
    """
    Follow [[wiki-link]] references in loaded pages one level deep.
    Only loads pages whose slugs appear in the content of already-loaded pages
    and that haven't been loaded yet. Limits to max_extra additional pages.
    """
    already_loaded_ids = {p["id"] for p in loaded_pages}
    linked_slugs: set[str] = set()

    for p in loaded_pages:
        content = p.get("content") or ""
        found = re.findall(r"\[\[([^\]|]+)(?:\|[^\]]*)?\]\]", content)
        linked_slugs.update(s.lower() for s in found)

    if not linked_slugs:
        return []

    # Load linked pages not already in context
    extra_pages = _load_pages_by_slugs(audit_id, list(linked_slugs), db_conn)
    extra_pages = [p for p in extra_pages if p["id"] not in already_loaded_ids]

    # Simple relevance filter: prefer pages whose title/content overlaps with query terms
    query_terms = set(query.lower().split())
    def relevance(p: dict) -> int:
        text = (p.get("title", "") + " " + (p.get("content") or "")).lower()
        return sum(1 for t in query_terms if t in text)

    extra_pages.sort(key=relevance, reverse=True)
    return extra_pages[:max_extra]


# ── Step 3: Synthesis with slug-level citations ───────────────────────────────

def _build_page_context(pages: list[dict]) -> str:
    """Format loaded wiki pages into a context block for the LLM."""
    if not pages:
        return "No relevant wiki pages found."

    parts = []
    for p in pages:
        try:
            meta = json.loads(p.get("metadata") or "{}")
            slug = meta.get("id", "").split("/")[-1] or p["id"][:8]
        except Exception:
            slug = p["id"][:8]
        content_preview = (p.get("content") or "")[:2000]
        parts.append(f"--- PAGE: [[{slug}]] ({p.get('page_type', 'unknown')}) ---\n{content_preview}")

    return "\n\n".join(parts)


def _build_guidance_context(audit_id: str, query: str, db_conn) -> str:
    """Load guidance documents and build context for guidance-scope queries."""
    sources = db_conn.execute(
        """SELECT filename, markdown_path FROM sources
           WHERE audit_id=? AND file_type='guidance' AND status='ready'""",
        (audit_id,)
    ).fetchall()

    if not sources:
        return "No guidance documents have been uploaded for this audit."

    # Simple keyword relevance scoring for guidance docs
    query_terms = set(query.lower().split())
    scored = []
    for src in sources:
        if src["markdown_path"] and Path(src["markdown_path"]).exists():
            content = Path(src["markdown_path"]).read_text(encoding="utf-8")
            score = sum(content.lower().count(t) for t in query_terms)
            scored.append((score, src["filename"], content))

    scored.sort(key=lambda x: x[0], reverse=True)

    context_parts = []
    total_chars = 0
    for _, filename, content in scored:
        excerpt = content[:3000]
        context_parts.append(f"[GUIDANCE DOCUMENT: {filename}]\n{excerpt}")
        total_chars += len(excerpt)
        if total_chars > 10000:
            break

    return "\n\n".join(context_parts) or "Guidance documents could not be read."


# ── Main entry point ──────────────────────────────────────────────────────────

def chat_response(
    audit_id: str,
    scope: str,
    user_message: str,
    history: list[dict],
    db_conn,
) -> dict:
    """
    Generate a chat response using the Karpathy-style agentic query loop.

    Evidence scope:
      1. LLM reads index.md → selects relevant page slugs
      2. Load selected pages + traverse [[wiki-link]] references
      3. LLM synthesises answer with [wiki: <slug>] citations

    Guidance scope:
      Relevance-scored guidance documents loaded directly.

    Returns:
        dict with keys: content (str), citations (list[str]), message_id (str),
                        pages_loaded (int), traversal_pages (int)
    """
    client = get_llm_client()
    model = get_llm_model()

    pages_loaded = 0
    traversal_count = 0

    if scope == "evidence":
        # ── Step 1: Ensure index exists ───────────────────────────────────────
        index_md = get_index(audit_id, db_conn)
        if not index_md:
            # Build index on demand if it doesn't exist yet
            index_md = rebuild_index(audit_id, db_conn)

        # ── Step 2: LLM selects relevant page slugs from index ────────────────
        selected_slugs = _select_pages_from_index(index_md, user_message, client, model, audit_id, db_conn)
        logger.info("Agentic query: selected slugs=%s", selected_slugs)

        # ── Step 3: Load selected pages ───────────────────────────────────────
        primary_pages = _load_pages_by_slugs(audit_id, selected_slugs, db_conn)

        # Fallback: if slug selection returned nothing, load most recent pages
        if not primary_pages:
            primary_pages = [
                dict(r) for r in db_conn.execute(
                    "SELECT id, page_type, title, content, metadata FROM wiki_pages "
                    "WHERE audit_id=? ORDER BY updated_at DESC LIMIT ?",
                    (audit_id, MAX_PAGES_PER_QUERY)
                ).fetchall()
            ]

        # ── Step 4: Traverse [[wiki-link]] references ─────────────────────────
        traversal_pages = _traverse_links(primary_pages, user_message, audit_id, db_conn)
        traversal_count = len(traversal_pages)
        all_pages = primary_pages + traversal_pages
        pages_loaded = len(all_pages)

        context = _build_page_context(all_pages)

        system_prompt = """You are an audit assistant querying a structured evidence wiki.

Answer the question using ONLY the wiki pages provided in the context.
For every factual claim, cite the source page using the format [wiki: <slug>].
If a [[wiki-link]] in the context points to a page not loaded, note it as a potential gap.
Be precise and concise. Do not synthesize beyond what the wiki pages state.
If the wiki does not contain enough information to answer, say so explicitly."""

    else:  # guidance scope
        context = _build_guidance_context(audit_id, user_message, db_conn)
        system_prompt = """You are an audit assistant with access to guidance documents.

Answer the question using ONLY the guidance documents provided.
For every claim, cite the document in the format [guidance: <filename>, <section>].
Be precise. Do not synthesize beyond what is written in the guidance."""

    # ── Build message chain ───────────────────────────────────────────────────
    messages = [{"role": "system", "content": system_prompt}]

    # Include recent conversation history (last 6 exchanges = 12 messages)
    for msg in history[-12:]:
        messages.append({"role": msg["role"], "content": msg["content"]})

    messages.append({
        "role": "user",
        "content": f"CONTEXT:\n{context}\n\n---\n\nQUESTION: {user_message}",
    })

    resp = client.chat.completions.create(
        model=model,
        messages=messages,
        **build_completion_kwargs(max_tokens=1800, temperature=0.2),
    )

    record_usage(audit_id, "chat", resp.usage, db_conn)

    content = resp.choices[0].message.content or ""

    # Extract structured citations: [wiki: slug] and [guidance: filename, section]
    wiki_citations = re.findall(r"\[wiki:\s*([^\]]+)\]", content)
    guidance_citations = re.findall(r"\[guidance:\s*([^\]]+)\]", content)
    citations = [f"wiki: {c.strip()}" for c in wiki_citations] + \
                [f"guidance: {c.strip()}" for c in guidance_citations]

    # Log the query to wiki log
    append_log(
        audit_id,
        f"query | scope={scope} | pages_loaded={pages_loaded} | traversal={traversal_count} | "
        f"q={user_message[:80]}{'...' if len(user_message) > 80 else ''}",
        db_conn,
    )

    message_id = str(uuid.uuid4())
    return {
        "message_id": message_id,
        "content": content,
        "citations": citations,
        "pages_loaded": pages_loaded,
        "traversal_pages": traversal_count,
    }


# ── Promote to wiki (manual action) ──────────────────────────────────────────

def promote_to_wiki(audit_id: str, message_id: str, db_conn) -> str | None:
    """
    Promote a chat response to a new wiki page with proper Karpathy-style formatting.
    The LLM writes the page with YAML frontmatter, structured sections, and a compiled-from footer.
    Returns the new page ID or None on failure.
    """
    msg = db_conn.execute(
        "SELECT content FROM chat_messages WHERE id=? AND audit_id=? AND scope='evidence'",
        (message_id, audit_id)
    ).fetchone()

    if not msg:
        return None

    client = get_llm_client()
    model = get_llm_model()

    promote_prompt = f"""You are maintaining an audit knowledge base wiki.

A chat response has been selected for promotion to a permanent wiki page.
Format it as a proper wiki page following the Karpathy LLM-Wiki pattern.

CHAT RESPONSE TO PROMOTE:
{msg['content']}

Write a complete wiki page with:
1. YAML frontmatter (id, type, source_ids, tags, created)
2. # Title
3. Structured sections (Key Details, Audit Relevance, Evidence)
4. See Also section with [[wiki-link]] references
5. *Compiled from: chat synthesis {_today()}* footer

Choose the most appropriate page_type from: evidence_area, control, process, finding, person, system, source.
Return ONLY the raw markdown page content. No explanation."""

    resp = client.chat.completions.create(
        model=model,
        messages=[
            {"role": "system", "content": "You are an audit wiki writer. Return only the raw markdown page content."},
            {"role": "user", "content": promote_prompt},
        ],
        **build_completion_kwargs(max_tokens=2000, temperature=0.1),
    )

    record_usage(audit_id, "chat", resp.usage, db_conn)
    page_content = resp.choices[0].message.content or ""

    # Extract page_type from frontmatter if present
    page_type = "evidence_area"
    type_match = re.search(r"^type:\s*(\S+)", page_content, re.MULTILINE)
    if type_match:
        page_type = type_match.group(1).strip()

    # Extract title from # heading
    title = f"Chat Synthesis — {_today()}"
    title_match = re.search(r"^# (.+)$", page_content, re.MULTILINE)
    if title_match:
        title = title_match.group(1).strip()

    # Extract slug from frontmatter id field
    slug = title.lower().replace(" ", "-")[:40]
    id_match = re.search(r"^id:\s*(.+)$", page_content, re.MULTILINE)
    if id_match:
        slug = id_match.group(1).strip().split("/")[-1]

    metadata = {
        "id": f"{page_type}/{slug}",
        "sources": ["chat-synthesis"],
        "tags": ["chat-synthesis"],
        "links": re.findall(r"\[\[([^\]|]+)(?:\|[^\]]*)?\]\]", page_content),
    }

    page_id = str(uuid.uuid4())
    db_conn.execute(
        """INSERT INTO wiki_pages (id, audit_id, page_type, title, content, metadata, created_at, updated_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        (page_id, audit_id, page_type, title, page_content, json.dumps(metadata), _now(), _now())
    )
    db_conn.commit()

    # Rebuild index to include the new page
    from core.wiki_agent import rebuild_index as _rebuild
    _rebuild(audit_id, db_conn)

    append_log(audit_id, f"promote | chat message promoted to wiki page: {title}", db_conn)

    return page_id
