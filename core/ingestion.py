"""
ingestion.py
------------
File ingestion pipeline for the Protiviti Operational Audit Assistant.

Handles:
- Markitdown conversion for all supported formats
- Provenance metadata extraction (page numbers, sheet refs, slide numbers)
- Content hash computation for duplicate detection
- Background async processing with status updates
"""

import asyncio
import hashlib
import json
import logging
import os
import re
import uuid
from datetime import datetime, timezone
from pathlib import Path

logger = logging.getLogger(__name__)

SUPPORTED_EXTENSIONS = {
    ".pdf", ".docx", ".doc", ".pptx", ".ppt",
    ".xlsx", ".xls", ".csv",
    ".png", ".jpg", ".jpeg", ".tiff", ".tif", ".bmp", ".gif",
    ".html", ".htm",
    ".txt", ".md",
}


def compute_hash(file_path: str) -> str:
    """Compute SHA-256 hash of a file."""
    sha = hashlib.sha256()
    with open(file_path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            sha.update(chunk)
    return sha.hexdigest()


def extract_provenance(file_path: str, markdown_content: str) -> dict:
    """
    Extract machine-readable location anchors from the converted markdown.
    Returns a dict with format-specific anchors.
    """
    ext = Path(file_path).suffix.lower()
    provenance: dict = {"file": os.path.basename(file_path), "format": ext}

    if ext == ".pdf":
        # Count page markers inserted by markitdown (<!-- Page N --> or similar)
        pages = re.findall(r"(?:^|\n)#{1,2}\s*Page\s+(\d+)", markdown_content, re.IGNORECASE)
        if not pages:
            pages = re.findall(r"<!-- ?[Pp]age (\d+) ?-->", markdown_content)
        provenance["pages"] = [int(p) for p in pages] if pages else []
        provenance["page_count"] = len(provenance["pages"]) or markdown_content.count("\f") + 1

    elif ext in (".xlsx", ".xls", ".csv"):
        # Extract sheet names from markdown headers
        sheets = re.findall(r"^#{1,3}\s+(.+)$", markdown_content, re.MULTILINE)
        provenance["sheets"] = sheets
        # Count approximate rows per sheet
        row_counts = {}
        for sheet in sheets:
            pattern = re.escape(sheet)
            section = re.search(rf"#{1,3}\s+{pattern}(.*?)(?=#{1,3}\s|\Z)", markdown_content, re.DOTALL)
            if section:
                rows = section.group(1).count("\n|")
                row_counts[sheet] = rows
        provenance["row_counts"] = row_counts

    elif ext in (".pptx", ".ppt"):
        # Extract slide markers
        slides = re.findall(r"(?:^|\n)#{1,2}\s*Slide\s+(\d+)", markdown_content, re.IGNORECASE)
        provenance["slides"] = [int(s) for s in slides]
        provenance["slide_count"] = len(provenance["slides"])

    elif ext in (".docx", ".doc"):
        # Extract heading structure for section navigation
        headings = re.findall(r"^(#{1,4})\s+(.+)$", markdown_content, re.MULTILINE)
        provenance["sections"] = [{"level": len(h[0]), "title": h[1].strip()} for h in headings]

    return provenance


async def convert_file(source_id: str, file_path: str, db_conn) -> None:
    """
    Run Markitdown conversion in a thread pool and update source status.
    Runs the lint pass after successful conversion.
    """
    from markitdown import MarkItDown

    try:
        # Update status to converting
        now = datetime.now(timezone.utc).isoformat()
        db_conn.execute(
            "UPDATE sources SET status='converting', updated_at=? WHERE id=?",
            (now, source_id)
        )
        db_conn.commit()

        # Run conversion in thread pool to avoid blocking the event loop
        loop = asyncio.get_event_loop()
        md_result = await loop.run_in_executor(None, _run_markitdown, file_path)

        if md_result is None:
            raise RuntimeError("Markitdown returned no content")

        # Determine output path
        row = db_conn.execute("SELECT audit_id, filename FROM sources WHERE id=?", (source_id,)).fetchone()
        audit_id = row["audit_id"]
        md_dir = Path("audits") / audit_id / "markdown"
        md_dir.mkdir(parents=True, exist_ok=True)
        md_path = md_dir / (Path(file_path).stem + ".md")
        md_path.write_text(md_result, encoding="utf-8")

        # Extract provenance
        provenance = extract_provenance(file_path, md_result)

        now = datetime.now(timezone.utc).isoformat()
        db_conn.execute(
            """UPDATE sources
               SET status='ready', markdown_path=?, provenance_meta=?, updated_at=?
               WHERE id=?""",
            (str(md_path), json.dumps(provenance), now, source_id)
        )
        db_conn.commit()
        logger.info("Ingestion complete: source_id=%s path=%s", source_id, file_path)

    except Exception as exc:
        logger.exception("Ingestion failed: source_id=%s", source_id)
        now = datetime.now(timezone.utc).isoformat()
        db_conn.execute(
            "UPDATE sources SET status='failed', error_message=?, updated_at=? WHERE id=?",
            (str(exc), now, source_id)
        )
        db_conn.commit()


def _run_markitdown(file_path: str) -> str | None:
    """Synchronous Markitdown conversion (runs in thread pool)."""
    try:
        from markitdown import MarkItDown
        md = MarkItDown()
        result = md.convert(file_path)
        return result.text_content if result else None
    except Exception as exc:
        logger.error("Markitdown error for %s: %s", file_path, exc)
        raise
