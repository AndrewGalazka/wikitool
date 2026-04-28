"""
wiki_agent.py
-------------
Evidence wiki synthesis agent for the Protiviti Operational Audit Assistant.

Implements the Karpathy LLM-Wiki pattern (2026):
  - Each ingested source produces one or more structured wiki pages
  - Pages follow the atomic-wiki format: YAML frontmatter + titled sections +
    [[wiki-link]] cross-references + See Also + compiled-from footer
  - A lint pass runs after every ingestion to flag contradictions, gaps, and
    orphan references
  - An index.md and log.md are maintained for navigation and audit trail
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

logger = logging.getLogger(__name__)

# ── Page types aligned to audit dimensions ────────────────────────────────────
PAGE_TYPES = [
    "source",        # direct summary of an ingested document
    "person",        # named individual (auditee, owner, approver)
    "process",       # business or IT process
    "control",       # control activity or safeguard
    "system",        # application, platform, or infrastructure component
    "evidence_area", # thematic grouping of evidence (e.g. "Access Management")
    "finding",       # potential audit finding or observation
]

# ── Wiki schema communicated to the LLM ──────────────────────────────────────
WIKI_SCHEMA = """
You are maintaining an audit knowledge base using the Karpathy LLM-Wiki pattern.

PAGE FORMAT — every page you create or update MUST follow this exact structure:

```
---
id: <page_type>/<slug>
type: <page_type>
source_ids: ["<source_filename>"]
tags: [<comma-separated audit tags>]
created: <YYYY-MM-DD>
---

# Page Title

Opening paragraph: why this entity/concept matters to the audit, key facts.

## Key Details
Integrated content written as coherent prose. First mention of a related concept
gets a [[wiki-link]]; subsequent mentions in the same page do not repeat it.

## Audit Relevance
How this entity/concept relates to audit objectives, risks, or controls.

## Evidence
What evidence supports the facts on this page. Cite source filenames.

---

**See also**
- [[related-page-slug]] — one-line description

---
*Compiled from: <source_filename>*
```

RULES:
1. id slug: all lowercase, hyphens only, 3-6 words. Example: "control/access-review-process"
2. [[wiki-link]] slugs must match the id field of an existing or newly created page (without the type/ prefix)
3. First line after frontmatter must be # Title
4. Target 400-800 words per page for audit pages
5. Always create a "source" page summarising the ingested document
6. Create separate pages for each distinct person, process, control, system, or evidence area found
7. Only create a "finding" page if a clear control gap or deficiency is explicitly stated
8. For updates, merge new information into existing content — do not replace wholesale
9. Cite the source filename in the Evidence section and in the compiled-from footer
10. Use specific dates (as of YYYY-MM-DD) not "currently" or "latest"
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _today() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def _parse_json_response(raw: str, context: str = "") -> list:
    """Robustly parse a JSON array from an LLM response."""
    raw = raw.strip()
    # Strip markdown code fences
    raw = re.sub(r"^```(?:json)?\s*", "", raw)
    raw = re.sub(r"\s*```$", "", raw).strip()

    try:
        parsed = json.loads(raw)
        if isinstance(parsed, list):
            items = [item for item in parsed if isinstance(item, dict)]
            skipped = len(parsed) - len(items)
            if skipped:
                logger.warning("%s: skipped %d non-dict items in LLM response", context, skipped)
            return items
        elif isinstance(parsed, dict):
            return [parsed]
    except json.JSONDecodeError:
        pass

    # Last-resort: extract first JSON array found anywhere in the response
    match = re.search(r"\[.*\]", raw, re.DOTALL)
    if match:
        try:
            parsed = json.loads(match.group(0))
            return [item for item in parsed if isinstance(item, dict)]
        except json.JSONDecodeError:
            pass

    logger.error("%s: could not parse LLM response as JSON. Raw (first 500): %s", context, raw[:500])
    return []


def synthesize_evidence(source_id: str, audit_id: str, db_conn) -> None:
    """
    Read a converted source and synthesize/update evidence wiki pages.
    Follows the Karpathy LLM-Wiki pattern — each page has YAML frontmatter,
    structured sections, [[wiki-link]] cross-references, and a compiled-from footer.
    Called after successful ingestion.
    """
    source = db_conn.execute(
        "SELECT * FROM sources WHERE id=?", (source_id,)
    ).fetchone()

    if not source or source["status"] != "ready" or not source["markdown_path"]:
        logger.warning("synthesize_evidence: source not ready id=%s", source_id)
        return

    md_content = Path(source["markdown_path"]).read_text(encoding="utf-8")
    # Truncate very large files to avoid token overflow
    md_excerpt = md_content[:14000]

    # Build a compact index of existing wiki pages for cross-reference awareness
    existing_pages = db_conn.execute(
        "SELECT id, page_type, title, metadata FROM wiki_pages WHERE audit_id=?",
        (audit_id,)
    ).fetchall()

    existing_index = ""
    if existing_pages:
        lines = []
        for p in existing_pages[:30]:
            try:
                meta = json.loads(p["metadata"] or "{}")
                slug = meta.get("id", "")
            except Exception:
                slug = ""
            lines.append(f"  - [{p['page_type']}] {p['title']} | id={p['id']} | slug={slug}")
        existing_index = "EXISTING WIKI PAGES (for cross-reference and update decisions):\n" + "\n".join(lines)
    else:
        existing_index = "EXISTING WIKI PAGES: None yet — this is the first ingestion."

    client = get_llm_client()
    model = get_llm_model()

    prompt = f"""{WIKI_SCHEMA}

---

NEW SOURCE TO INGEST
Filename: {source['filename']}
Date ingested: {_today()}

CONTENT (excerpt, up to 14000 chars):
{md_excerpt}

---

{existing_index}

---

TASK:
Analyze the source content and return a JSON array of wiki page operations.

Each operation must be a JSON object with these fields:
- "action": "create" or "update"
- "page_type": one of {PAGE_TYPES}
- "title": concise human-readable page title (not the slug)
- "slug": the id slug (e.g. "access-review-process") — lowercase, hyphens, 3-6 words
- "content": the FULL page body following the format above (including YAML frontmatter, all sections, See Also, compiled-from footer)
- "metadata": object with keys:
    - "id": "<page_type>/<slug>"
    - "sources": ["<source_filename>"]
    - "tags": [list of audit-relevant tags]
    - "links": [list of slugs this page links to via [[wiki-link]]]
- "page_id": (ONLY for "update" action) the database ID of the existing page to update

Return ONLY a valid JSON array. No explanation, no prose, no markdown fences."""

    resp = client.chat.completions.create(
        model=model,
        messages=[
            {
                "role": "system",
                "content": (
                    "You are a JSON-only API implementing the Karpathy LLM-Wiki pattern for audit knowledge bases. "
                    "Respond with a single valid JSON array of operation objects. "
                    "Start with '[' and end with ']'. No markdown fences, no explanation."
                ),
            },
            {"role": "user", "content": prompt},
        ],
        **build_completion_kwargs(max_tokens=4000, temperature=0.1),
        **build_response_format_kwargs(),
    )

    record_usage(audit_id, "ingestion", resp.usage, db_conn)

    raw = resp.choices[0].message.content or "[]"
    operations = _parse_json_response(raw, context="synthesize_evidence")

    created_count = 0
    updated_count = 0

    for op in operations:
        try:
            action = op.get("action", "create")
            content = op.get("content", "")
            metadata = op.get("metadata", {})

            # Ensure metadata always has sources list containing this filename
            if "sources" not in metadata:
                metadata["sources"] = []
            if source["filename"] not in metadata["sources"]:
                metadata["sources"].append(source["filename"])

            if action == "create":
                page_id = str(uuid.uuid4())
                db_conn.execute(
                    """INSERT INTO wiki_pages (id, audit_id, page_type, title, content, metadata, created_at, updated_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        page_id,
                        audit_id,
                        op.get("page_type", "source"),
                        op.get("title", "Untitled"),
                        content,
                        json.dumps(metadata),
                        _now(),
                        _now(),
                    ),
                )
                created_count += 1

            elif action == "update" and op.get("page_id"):
                # Merge sources list with existing metadata
                existing = db_conn.execute(
                    "SELECT metadata FROM wiki_pages WHERE id=? AND audit_id=?",
                    (op["page_id"], audit_id)
                ).fetchone()
                if existing:
                    try:
                        existing_meta = json.loads(existing["metadata"] or "{}")
                        existing_sources = existing_meta.get("sources", [])
                        for s in metadata.get("sources", []):
                            if s not in existing_sources:
                                existing_sources.append(s)
                        metadata["sources"] = existing_sources
                    except Exception:
                        pass

                db_conn.execute(
                    "UPDATE wiki_pages SET content=?, metadata=?, updated_at=? WHERE id=? AND audit_id=?",
                    (content, json.dumps(metadata), _now(), op["page_id"], audit_id),
                )
                updated_count += 1

        except Exception as exc:
            logger.error("Wiki operation failed for op %s — %s", op.get("title", "?"), exc)

    db_conn.commit()
    logger.info(
        "Wiki synthesis complete: source_id=%s, created=%d, updated=%d",
        source_id, created_count, updated_count,
    )

    # Run lint pass after every ingestion (as required by spec)
    run_lint_pass(audit_id, db_conn)


def run_lint_pass(audit_id: str, db_conn) -> None:
    """
    Two-layer lint pass following the Karpathy/llm-atomic-wiki pattern:
    1. Structural checks (missing frontmatter, broken [[links]], orphan pages)
    2. LLM semantic checks (contradictions, concept gaps, expired claims)
    Inserts lint_issues records for any problems found.
    """
    pages = db_conn.execute(
        "SELECT id, page_type, title, content, metadata FROM wiki_pages WHERE audit_id=?",
        (audit_id,)
    ).fetchall()

    if not pages:
        return

    # ── Layer 1: Structural lint ──────────────────────────────────────────────
    structural_issues = []
    all_slugs = set()

    for p in pages:
        try:
            meta = json.loads(p["metadata"] or "{}")
            slug = meta.get("id", "")
            if slug:
                # Extract just the slug part after type/
                all_slugs.add(slug.split("/")[-1])
        except Exception:
            pass

    for p in pages:
        content = p["content"] or ""
        # Check for YAML frontmatter
        if not content.strip().startswith("---"):
            structural_issues.append({
                "issue_type": "gap",
                "description": f'Page "{p["title"]}" is missing YAML frontmatter. It should start with ---.',
                "page_ids": [p["id"]],
            })
        # Check for # Title as first content line after frontmatter
        lines = content.strip().splitlines()
        has_title = any(line.startswith("# ") for line in lines[:10])
        if not has_title:
            structural_issues.append({
                "issue_type": "gap",
                "description": f'Page "{p["title"]}" is missing a # Title heading after frontmatter.',
                "page_ids": [p["id"]],
            })
        # Check for ghost [[wiki-links]]
        linked_slugs = re.findall(r"\[\[([^\]|]+)(?:\|[^\]]*)?\]\]", content)
        for linked_slug in linked_slugs:
            if linked_slug not in all_slugs:
                structural_issues.append({
                    "issue_type": "unresolved_reference",
                    "description": f'Page "{p["title"]}" contains [[{linked_slug}]] but no page with that slug exists.',
                    "page_ids": [p["id"]],
                })

    # ── Layer 2: LLM semantic lint ────────────────────────────────────────────
    pages_summary = "\n\n".join(
        f"[ID:{p['id']}][{p['page_type']}] {p['title']}\n{(p['content'] or '')[:600]}"
        for p in pages[:25]
    )

    client = get_llm_client()
    model = get_llm_model()

    lint_prompt = f"""You are an audit quality reviewer performing an LLM lint pass on a wiki knowledge base.

WIKI PAGES (summaries):
{pages_summary}

Check for the following issues and return a JSON array:

1. CONTRADICTIONS — two pages state conflicting facts about the same entity or control
2. GAPS — a concept, control, or entity is referenced across multiple pages but has no dedicated page
3. UNRESOLVED_REFERENCE — a finding or risk claim lacks supporting evidence pages

For each issue return:
- "issue_type": "contradiction" | "gap" | "unresolved_reference"
- "description": clear, specific description of the issue (quote the conflicting text if applicable)
- "page_ids": list of affected page IDs using the [ID:...] values above

Return ONLY a valid JSON array. If no issues found, return [].
"""

    resp = client.chat.completions.create(
        model=model,
        messages=[
            {
                "role": "system",
                "content": "You are a JSON-only API. Return a valid JSON array only. No prose, no fences.",
            },
            {"role": "user", "content": lint_prompt},
        ],
        **build_completion_kwargs(max_tokens=2000, temperature=0.1),
        **build_response_format_kwargs(),
    )

    record_usage(audit_id, "lint", resp.usage, db_conn)

    raw = resp.choices[0].message.content or "[]"
    llm_issues = _parse_json_response(raw, context="run_lint_pass")

    all_issues = structural_issues + llm_issues

    # Clear old unresolved issues before inserting fresh ones
    db_conn.execute("DELETE FROM lint_issues WHERE audit_id=? AND resolved=0", (audit_id,))

    for issue in all_issues:
        if not isinstance(issue, dict):
            continue
        issue_id = str(uuid.uuid4())
        page_ids = issue.get("page_ids", [])
        primary_page = page_ids[0] if page_ids else None
        db_conn.execute(
            """INSERT INTO lint_issues (id, audit_id, page_id, issue_type, description, source_pages, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (
                issue_id,
                audit_id,
                primary_page,
                issue.get("issue_type", "gap"),
                issue.get("description", ""),
                json.dumps(page_ids),
                _now(),
            ),
        )

    db_conn.commit()
    logger.info(
        "Lint pass complete: audit_id=%s, structural=%d, llm=%d, total=%d",
        audit_id, len(structural_issues), len(llm_issues), len(all_issues),
    )
