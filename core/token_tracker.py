"""
token_tracker.py
----------------
Records LLM token usage per audit for display in the UI.
"""

import uuid
from datetime import datetime, timezone


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def record_usage(audit_id: str, operation: str, usage, db_conn) -> None:
    """
    Record token usage from an OpenAI API response.
    usage: openai.types.CompletionUsage object (has prompt_tokens, completion_tokens, total_tokens)
    """
    if usage is None:
        return
    try:
        db_conn.execute(
            """INSERT INTO token_usage (id, audit_id, operation, prompt_tokens, completion_tokens, total_tokens, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (str(uuid.uuid4()), audit_id, operation,
             getattr(usage, "prompt_tokens", 0),
             getattr(usage, "completion_tokens", 0),
             getattr(usage, "total_tokens", 0),
             _now())
        )
        db_conn.commit()
    except Exception:
        pass  # Non-critical — never let token tracking break the main flow


def get_audit_token_totals(audit_id: str, db_conn) -> dict:
    """Return total token usage for an audit, broken down by operation."""
    rows = db_conn.execute(
        """SELECT operation,
                  SUM(prompt_tokens) as prompt,
                  SUM(completion_tokens) as completion,
                  SUM(total_tokens) as total
           FROM token_usage WHERE audit_id=?
           GROUP BY operation""",
        (audit_id,)
    ).fetchall()

    result = {"by_operation": {}, "grand_total": 0}
    for row in rows:
        result["by_operation"][row["operation"]] = {
            "prompt": row["prompt"],
            "completion": row["completion"],
            "total": row["total"],
        }
        result["grand_total"] += row["total"]
    return result
