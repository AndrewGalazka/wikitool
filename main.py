"""
main.py
-------
Protiviti Operational Audit Assistant — FastAPI Application Entry Point
"""

import asyncio
import hashlib
import json
import logging
import os
import shutil
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import aiofiles
from dotenv import load_dotenv
from fastapi import BackgroundTasks, FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

load_dotenv()

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s — %(message)s")
logger = logging.getLogger(__name__)

from core.database import get_db, init_db
from core.ingestion import compute_hash, convert_file, SUPPORTED_EXTENSIONS
from core.token_tracker import get_audit_token_totals
from core.graph_builder import build_graph

app = FastAPI(title="Protiviti Operational Audit Assistant", version="1.0.0")
app.mount("/static", StaticFiles(directory="static"), name="static")
templates = Jinja2Templates(directory="templates")

# Initialise database on startup
@app.on_event("startup")
def startup():
    init_db()
    logger.info("Database initialised.")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _short_id() -> str:
    return str(uuid.uuid4())


# ══════════════════════════════════════════════════════════════════════════════
# DASHBOARD
# ══════════════════════════════════════════════════════════════════════════════

@app.get("/", response_class=HTMLResponse)
async def dashboard(request: Request):
    db = get_db()
    audits = db.execute(
        "SELECT * FROM audits ORDER BY updated_at DESC"
    ).fetchall()

    audit_list = []
    for a in audits:
        # Work program completion %
        total = db.execute(
            "SELECT COUNT(*) as c FROM work_program_rows WHERE audit_id=?", (a["id"],)
        ).fetchone()["c"]
        done = db.execute(
            "SELECT COUNT(*) as c FROM work_program_rows WHERE audit_id=? AND status='completed'",
            (a["id"],)
        ).fetchone()["c"]
        pct = round((done / total * 100) if total > 0 else 0)
        audit_list.append({**dict(a), "completion_pct": pct, "total_rows": total, "done_rows": done})

    db.close()
    return templates.TemplateResponse(request, "dashboard.html", context={"audits": audit_list})


# ══════════════════════════════════════════════════════════════════════════════
# AUDIT LIFECYCLE
# ══════════════════════════════════════════════════════════════════════════════

@app.post("/audits")
async def create_audit(name: str = Form(...), client: str = Form("")):
    if not name.strip():
        raise HTTPException(400, "Audit name is required")
    db = get_db()
    audit_id = _short_id()
    now = _now()
    db.execute(
        "INSERT INTO audits (id, name, client, status, created_at, updated_at) VALUES (?,?,?,?,?,?)",
        (audit_id, name.strip(), client.strip() or None, "active", now, now)
    )
    db.commit()
    # Create audit directory structure
    for subdir in ["raw/evidence", "raw/guidance", "markdown", "wiki_evidence", "guidance", "work_program", "graph"]:
        Path(f"audits/{audit_id}/{subdir}").mkdir(parents=True, exist_ok=True)
    db.close()
    return JSONResponse({"id": audit_id, "name": name})


@app.post("/audits/{audit_id}/close")
async def close_audit(audit_id: str):
    db = get_db()
    db.execute("UPDATE audits SET status='closed', updated_at=? WHERE id=?", (_now(), audit_id))
    db.commit()
    db.close()
    return {"status": "closed"}


@app.post("/audits/{audit_id}/reopen")
async def reopen_audit(audit_id: str):
    db = get_db()
    db.execute("UPDATE audits SET status='active', updated_at=? WHERE id=?", (_now(), audit_id))
    db.commit()
    db.close()
    return {"status": "active"}


@app.delete("/audits/{audit_id}")
async def delete_audit(audit_id: str, confirm_name: str = Form(...)):
    db = get_db()
    audit = db.execute("SELECT name FROM audits WHERE id=?", (audit_id,)).fetchone()
    if not audit:
        raise HTTPException(404, "Audit not found")
    if confirm_name.strip() != audit["name"]:
        raise HTTPException(400, "Confirmation name does not match")
    db.execute("DELETE FROM audits WHERE id=?", (audit_id,))
    db.commit()
    db.close()
    # Remove audit directory
    audit_dir = Path(f"audits/{audit_id}")
    if audit_dir.exists():
        shutil.rmtree(audit_dir)
    return {"deleted": True}


# ══════════════════════════════════════════════════════════════════════════════
# AUDIT WORKSPACE
# ══════════════════════════════════════════════════════════════════════════════

@app.get("/audits/{audit_id}", response_class=HTMLResponse)
async def audit_workspace(request: Request, audit_id: str):
    db = get_db()
    audit = db.execute("SELECT * FROM audits WHERE id=?", (audit_id,)).fetchone()
    if not audit:
        raise HTTPException(404, "Audit not found")
    sources = db.execute(
        "SELECT * FROM sources WHERE audit_id=? ORDER BY created_at DESC", (audit_id,)
    ).fetchall()
    token_totals = get_audit_token_totals(audit_id, db)
    db.close()
    return templates.TemplateResponse(request, "workspace.html", context={
        "audit": dict(audit),
        "sources": [dict(s) for s in sources],
        "token_totals": token_totals,
    })


# ══════════════════════════════════════════════════════════════════════════════
# FILE INGESTION
# ══════════════════════════════════════════════════════════════════════════════

@app.post("/audits/{audit_id}/upload")
async def upload_file(
    audit_id: str,
    background_tasks: BackgroundTasks,
    file: UploadFile = File(...),
    file_type: str = Form(...),  # evidence | guidance
):
    db = get_db()
    audit = db.execute("SELECT status FROM audits WHERE id=?", (audit_id,)).fetchone()
    if not audit:
        raise HTTPException(404, "Audit not found")
    if audit["status"] == "closed":
        raise HTTPException(403, "Audit is closed")

    ext = Path(file.filename).suffix.lower()
    if ext not in SUPPORTED_EXTENSIONS:
        raise HTTPException(400, f"Unsupported file type: {ext}")

    # Save raw file
    raw_dir = Path(f"audits/{audit_id}/raw/{file_type}")
    raw_dir.mkdir(parents=True, exist_ok=True)
    raw_path = raw_dir / file.filename

    content = await file.read()
    raw_path.write_bytes(content)

    # Compute hash for duplicate detection
    content_hash = hashlib.sha256(content).hexdigest()

    # Check for duplicates
    existing = db.execute(
        "SELECT id, filename FROM sources WHERE audit_id=? AND (filename=? OR content_hash=?)",
        (audit_id, file.filename, content_hash)
    ).fetchone()

    if existing:
        db.close()
        return JSONResponse({
            "duplicate": True,
            "existing_id": existing["id"],
            "existing_filename": existing["filename"],
            "filename": file.filename,
        })

    source_id = _short_id()
    now = _now()
    db.execute(
        """INSERT INTO sources (id, audit_id, filename, file_type, original_path, content_hash, status, created_at, updated_at)
           VALUES (?,?,?,?,?,?,?,?,?)""",
        (source_id, audit_id, file.filename, file_type, str(raw_path), content_hash, "pending", now, now)
    )
    db.commit()

    # Queue background conversion + wiki synthesis
    background_tasks.add_task(_ingest_and_synthesize, source_id, audit_id, str(raw_path), file_type)

    db.close()
    return JSONResponse({"source_id": source_id, "filename": file.filename, "status": "pending"})


@app.post("/audits/{audit_id}/upload/resolve-duplicate")
async def resolve_duplicate(
    audit_id: str,
    background_tasks: BackgroundTasks,
    existing_id: str = Form(...),
    filename: str = Form(...),
    file_type: str = Form(...),
    action: str = Form(...),  # replace | version | skip
):
    db = get_db()
    if action == "skip":
        db.close()
        return {"action": "skipped"}

    raw_dir = Path(f"audits/{audit_id}/raw/{file_type}")
    raw_path = raw_dir / filename

    if not raw_path.exists():
        raise HTTPException(400, "Original upload not found — please re-upload the file")

    content = raw_path.read_bytes()
    content_hash = hashlib.sha256(content).hexdigest()
    now = _now()

    if action == "replace":
        db.execute("DELETE FROM sources WHERE id=?", (existing_id,))
        source_id = _short_id()
        db.execute(
            """INSERT INTO sources (id, audit_id, filename, file_type, original_path, content_hash, status, created_at, updated_at)
               VALUES (?,?,?,?,?,?,?,?,?)""",
            (source_id, audit_id, filename, file_type, str(raw_path), content_hash, "pending", now, now)
        )
    else:  # version
        versioned_name = f"{Path(filename).stem}_v{now[:10]}{Path(filename).suffix}"
        versioned_path = raw_dir / versioned_name
        shutil.copy(raw_path, versioned_path)
        source_id = _short_id()
        db.execute(
            """INSERT INTO sources (id, audit_id, filename, file_type, original_path, content_hash, status, created_at, updated_at)
               VALUES (?,?,?,?,?,?,?,?,?)""",
            (source_id, audit_id, versioned_name, file_type, str(versioned_path), content_hash, "pending", now, now)
        )

    db.commit()
    background_tasks.add_task(_ingest_and_synthesize, source_id, audit_id, str(raw_path), file_type)
    db.close()
    return {"action": action, "source_id": source_id}


async def _ingest_and_synthesize(source_id: str, audit_id: str, file_path: str, file_type: str):
    """Background task: convert file, then synthesize wiki (evidence only)."""
    db = get_db()
    try:
        await convert_file(source_id, file_path, db)
        if file_type == "evidence":
            from core.wiki_agent import synthesize_evidence
            synthesize_evidence(source_id, audit_id, db)
    except Exception as exc:
        logger.exception("Background ingestion failed: source_id=%s", source_id)
    finally:
        db.close()


@app.get("/audits/{audit_id}/sources")
async def list_sources(audit_id: str):
    db = get_db()
    sources = db.execute(
        "SELECT id, filename, file_type, status, error_message, created_at FROM sources WHERE audit_id=? ORDER BY created_at DESC",
        (audit_id,)
    ).fetchall()
    db.close()
    return [dict(s) for s in sources]


@app.get("/audits/{audit_id}/sources/{source_id}/status")
async def source_status(audit_id: str, source_id: str):
    db = get_db()
    src = db.execute("SELECT status, error_message FROM sources WHERE id=? AND audit_id=?", (source_id, audit_id)).fetchone()
    db.close()
    if not src:
        raise HTTPException(404)
    return dict(src)


# ══════════════════════════════════════════════════════════════════════════════
# GUIDANCE CONTENT
# ══════════════════════════════════════════════════════════════════════════════

@app.get("/audits/{audit_id}/sources/{source_id}/content")
async def get_source_content(audit_id: str, source_id: str):
    """Return the markdown content of a converted source file."""
    db = get_db()
    src = db.execute(
        "SELECT markdown_path, filename FROM sources WHERE id=? AND audit_id=?",
        (source_id, audit_id)
    ).fetchone()
    db.close()
    if not src or not src["markdown_path"]:
        raise HTTPException(404, "Content not available")
    md_path = Path(src["markdown_path"])
    if not md_path.exists():
        raise HTTPException(404, "Markdown file not found")
    content = md_path.read_text(encoding="utf-8")
    return {"filename": src["filename"], "content": content}


# ══════════════════════════════════════════════════════════════════════════════
# DELETE SOURCE
# ══════════════════════════════════════════════════════════════════════════════

@app.delete("/audits/{audit_id}/sources/{source_id}")
async def delete_source(audit_id: str, source_id: str):
    """Delete a source file and its associated wiki pages."""
    db = get_db()
    src = db.execute(
        "SELECT original_path, markdown_path, filename FROM sources WHERE id=? AND audit_id=?",
        (source_id, audit_id)
    ).fetchone()
    if not src:
        db.close()
        raise HTTPException(404, "Source not found")

    # Remove files from disk
    for path_col in (src["original_path"], src["markdown_path"]):
        if path_col:
            p = Path(path_col)
            if p.exists():
                p.unlink(missing_ok=True)

    # wiki_pages has no source_id column — provenance is stored in the metadata JSON
    # under the "sources" key (list of filenames). Delete pages whose metadata
    # references this filename AND whose page_type is "source" (the direct page),
    # plus any lint_issues tied to those pages.
    filename = src["filename"]
    candidate_pages = db.execute(
        "SELECT id, metadata FROM wiki_pages WHERE audit_id=?", (audit_id,)
    ).fetchall()
    pages_to_delete = []
    for page in candidate_pages:
        try:
            meta = json.loads(page["metadata"] or "{}")
            page_sources = meta.get("sources", [])
            if filename in page_sources:
                pages_to_delete.append(page["id"])
        except (json.JSONDecodeError, TypeError):
            pass

    for pid in pages_to_delete:
        db.execute("DELETE FROM lint_issues WHERE page_id=?", (pid,))
        db.execute("DELETE FROM wiki_pages WHERE id=?", (pid,))

    # Remove the source record itself
    db.execute("DELETE FROM sources WHERE id=? AND audit_id=?", (source_id, audit_id))
    db.commit()
    db.close()
    logger.info("Deleted source %s (%s) and %d wiki pages", source_id, filename, len(pages_to_delete))
    return {"deleted": source_id, "wiki_pages_removed": len(pages_to_delete)}


# ══════════════════════════════════════════════════════════════════════════════
# EVIDENCE WIKI
# ══════════════════════════════════════════════════════════════════════════════

@app.get("/audits/{audit_id}/wiki")
async def list_wiki_pages(audit_id: str, page_type: Optional[str] = None, q: Optional[str] = None):
    db = get_db()
    if q:
        from core.work_program_agent import search_wiki
        pages = search_wiki(audit_id, q, db, limit=20)
    elif page_type:
        pages = [dict(p) for p in db.execute(
            "SELECT * FROM wiki_pages WHERE audit_id=? AND page_type=? ORDER BY title",
            (audit_id, page_type)
        ).fetchall()]
    else:
        pages = [dict(p) for p in db.execute(
            "SELECT * FROM wiki_pages WHERE audit_id=? ORDER BY page_type, title",
            (audit_id,)
        ).fetchall()]
    db.close()
    return pages


@app.get("/audits/{audit_id}/wiki/{page_id}")
async def get_wiki_page(audit_id: str, page_id: str):
    db = get_db()
    page = db.execute("SELECT * FROM wiki_pages WHERE id=? AND audit_id=?", (page_id, audit_id)).fetchone()
    if not page:
        raise HTTPException(404)
    # Get backlinks
    backlinks = db.execute(
        """SELECT id, title, page_type FROM wiki_pages
           WHERE audit_id=? AND id != ? AND metadata LIKE ?""",
        (audit_id, page_id, f'%{page["title"]}%')
    ).fetchall()
    # Get lint issues for this page
    issues = db.execute(
        "SELECT * FROM lint_issues WHERE page_id=? AND resolved=0", (page_id,)
    ).fetchall()
    db.close()
    return {
        **dict(page),
        "backlinks": [dict(b) for b in backlinks],
        "issues": [dict(i) for i in issues],
    }


@app.put("/audits/{audit_id}/wiki/{page_id}")
async def update_wiki_page(audit_id: str, page_id: str, request: Request):
    body = await request.json()
    db = get_db()
    db.execute(
        "UPDATE wiki_pages SET content=?, updated_at=? WHERE id=? AND audit_id=?",
        (body.get("content", ""), _now(), page_id, audit_id)
    )
    db.commit()
    db.close()
    return {"updated": True}


# ══════════════════════════════════════════════════════════════════════════════
# LINT ISSUES
# ══════════════════════════════════════════════════════════════════════════════

@app.get("/audits/{audit_id}/lint")
async def get_lint_issues(audit_id: str):
    db = get_db()
    issues = db.execute(
        """SELECT li.*, wp.title as page_title
           FROM lint_issues li
           LEFT JOIN wiki_pages wp ON li.page_id = wp.id
           WHERE li.audit_id=? AND li.resolved=0
           ORDER BY li.created_at DESC""",
        (audit_id,)
    ).fetchall()
    db.close()
    return [dict(i) for i in issues]


@app.post("/audits/{audit_id}/lint/{issue_id}/resolve")
async def resolve_lint_issue(audit_id: str, issue_id: str):
    db = get_db()
    db.execute("UPDATE lint_issues SET resolved=1 WHERE id=? AND audit_id=?", (issue_id, audit_id))
    db.commit()
    db.close()
    return {"resolved": True}


# ══════════════════════════════════════════════════════════════════════════════
# KNOWLEDGE GRAPH
# ══════════════════════════════════════════════════════════════════════════════

@app.get("/audits/{audit_id}/graph")
async def get_graph(audit_id: str):
    db = get_db()
    graph = build_graph(audit_id, db)
    db.close()
    return graph


@app.get("/audits/{audit_id}/graph/export-issues")
async def export_graph_issues(audit_id: str):
    """Export lint issues as CSV."""
    import csv
    import io
    db = get_db()
    issues = db.execute(
        """SELECT li.issue_type, li.description, li.source_pages, li.created_at, wp.title as page_title
           FROM lint_issues li
           LEFT JOIN wiki_pages wp ON li.page_id = wp.id
           WHERE li.audit_id=?""",
        (audit_id,)
    ).fetchall()
    db.close()

    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(["Issue Type", "Affected Page", "Description", "Related Pages", "Detected At"])
    for i in issues:
        writer.writerow([i["issue_type"], i["page_title"] or "", i["description"], i["source_pages"] or "", i["created_at"]])

    output.seek(0)
    from fastapi.responses import StreamingResponse
    return StreamingResponse(
        iter([output.getvalue()]),
        media_type="text/csv",
        headers={"Content-Disposition": f"attachment; filename=issues_{audit_id}.csv"}
    )


# ══════════════════════════════════════════════════════════════════════════════
# WORK PROGRAM
# ══════════════════════════════════════════════════════════════════════════════

@app.post("/audits/{audit_id}/work-program/upload")
async def upload_work_program(
    audit_id: str,
    file: UploadFile = File(...),
):
    """Upload a work program and return column headers for mapping."""
    import pandas as pd
    import io

    content = await file.read()
    ext = Path(file.filename).suffix.lower()

    try:
        if ext == ".csv":
            df = pd.read_csv(io.BytesIO(content))
        else:
            df = pd.read_excel(io.BytesIO(content))
    except Exception as exc:
        raise HTTPException(400, f"Could not parse file: {exc}")

    columns = list(df.columns)

    # Auto-detect common column names
    def find_col(candidates):
        for c in candidates:
            for col in columns:
                if c.lower() in col.lower():
                    return col
        return None

    suggested = {
        "test_id_col": find_col(["test id", "id", "ref", "number", "#"]),
        "description_col": find_col(["description", "test", "procedure", "step"]),
        "objective_col": find_col(["objective", "purpose", "goal"]),
    }

    # Save raw file
    wp_dir = Path(f"audits/{audit_id}/work_program")
    wp_dir.mkdir(parents=True, exist_ok=True)
    raw_path = wp_dir / file.filename
    raw_path.write_bytes(content)

    return {
        "filename": file.filename,
        "columns": columns,
        "suggested_mapping": suggested,
        "preview": df.head(3).to_dict(orient="records"),
    }


@app.post("/audits/{audit_id}/work-program/confirm")
async def confirm_work_program(audit_id: str, request: Request):
    """Confirm column mapping and load rows into the database."""
    import pandas as pd
    import io

    body = await request.json()
    filename = body["filename"]
    mapping = body["mapping"]  # {test_id_col, description_col, objective_col}

    wp_dir = Path(f"audits/{audit_id}/work_program")
    raw_path = wp_dir / filename

    if not raw_path.exists():
        raise HTTPException(400, "File not found — please re-upload")

    content = raw_path.read_bytes()
    ext = raw_path.suffix.lower()

    if ext == ".csv":
        df = pd.read_csv(io.BytesIO(content))
    else:
        df = pd.read_excel(io.BytesIO(content))

    db = get_db()
    wp_id = _short_id()
    now = _now()
    db.execute(
        "INSERT INTO work_programs (id, audit_id, filename, column_mapping, created_at) VALUES (?,?,?,?,?)",
        (wp_id, audit_id, filename, json.dumps(mapping), now)
    )

    canonical_cols = {mapping["test_id_col"], mapping["description_col"], mapping.get("objective_col", "")}
    extra_cols = [c for c in df.columns if c not in canonical_cols]

    for _, row in df.iterrows():
        row_id = _short_id()
        extra = {c: str(row.get(c, "")) for c in extra_cols}
        db.execute(
            """INSERT INTO work_program_rows
               (id, work_program_id, audit_id, test_id, description, objective, extra_columns, status, created_at, updated_at)
               VALUES (?,?,?,?,?,?,?,?,?,?)""",
            (row_id, wp_id, audit_id,
             str(row.get(mapping["test_id_col"], "")),
             str(row.get(mapping["description_col"], "")),
             str(row.get(mapping.get("objective_col", ""), "")) if mapping.get("objective_col") else None,
             json.dumps(extra), "pending", now, now)
        )

    db.commit()
    count = db.execute("SELECT COUNT(*) as c FROM work_program_rows WHERE work_program_id=?", (wp_id,)).fetchone()["c"]
    db.close()
    return {"work_program_id": wp_id, "rows_loaded": count}


@app.get("/audits/{audit_id}/work-program/rows")
async def list_work_program_rows(audit_id: str):
    db = get_db()
    rows = db.execute(
        "SELECT * FROM work_program_rows WHERE audit_id=? ORDER BY created_at",
        (audit_id,)
    ).fetchall()
    db.close()
    return [dict(r) for r in rows]


@app.get("/audits/{audit_id}/work-program/rows/{row_id}")
async def get_work_program_row(audit_id: str, row_id: str):
    db = get_db()
    row = db.execute("SELECT * FROM work_program_rows WHERE id=? AND audit_id=?", (row_id, audit_id)).fetchone()
    db.close()
    if not row:
        raise HTTPException(404)
    return dict(row)


@app.post("/audits/{audit_id}/work-program/rows/{row_id}/run")
async def run_row(audit_id: str, row_id: str, background_tasks: BackgroundTasks):
    db = get_db()
    audit = db.execute("SELECT status FROM audits WHERE id=?", (audit_id,)).fetchone()
    if audit["status"] == "closed":
        raise HTTPException(403, "Audit is closed")
    row = db.execute("SELECT verified FROM work_program_rows WHERE id=? AND audit_id=?", (row_id, audit_id)).fetchone()
    if not row:
        raise HTTPException(404)
    if row["verified"]:
        return {"skipped": True, "reason": "Row is verified"}
    db.close()
    background_tasks.add_task(_run_row_bg, row_id, audit_id)
    return {"queued": True}


@app.post("/audits/{audit_id}/work-program/run-all")
async def run_all_rows(audit_id: str, background_tasks: BackgroundTasks):
    db = get_db()
    rows = db.execute(
        "SELECT id FROM work_program_rows WHERE audit_id=? AND verified=0",
        (audit_id,)
    ).fetchall()
    db.close()
    for row in rows:
        background_tasks.add_task(_run_row_bg, row["id"], audit_id)
    return {"queued": len(rows)}


async def _run_row_bg(row_id: str, audit_id: str):
    db = get_db()
    try:
        from core.work_program_agent import run_test_row
        run_test_row(row_id, audit_id, db)
    finally:
        db.close()


@app.put("/audits/{audit_id}/work-program/rows/{row_id}")
async def update_row(audit_id: str, row_id: str, request: Request):
    body = await request.json()
    db = get_db()
    db.execute(
        """UPDATE work_program_rows
           SET conclusion=?, human_notes=?, verified=?, updated_at=?
           WHERE id=? AND audit_id=?""",
        (body.get("conclusion"), body.get("human_notes"), int(body.get("verified", 0)),
         _now(), row_id, audit_id)
    )
    db.commit()
    db.close()
    return {"updated": True}


@app.get("/audits/{audit_id}/work-program/export")
async def export_work_program(audit_id: str):
    """Export work program with agent outputs as XLSX."""
    import pandas as pd
    import io

    db = get_db()
    rows = db.execute(
        "SELECT * FROM work_program_rows WHERE audit_id=? ORDER BY created_at",
        (audit_id,)
    ).fetchall()
    db.close()

    data = []
    for r in rows:
        extra = json.loads(r["extra_columns"] or "{}")
        data.append({
            "Test ID": r["test_id"],
            "Description": r["description"],
            "Objective": r["objective"] or "",
            **extra,
            "Status": r["status"],
            "Conclusion": r["conclusion"] or "",
            "Evidence References": r["evidence_references"] or "",
            "Open Questions": r["open_questions"] or "",
            "Requested Evidence": r["requested_evidence"] or "",
            "Human Notes": r["human_notes"] or "",
            "Verified": "Yes" if r["verified"] else "No",
            "Last Run": r["last_run_at"] or "",
        })

    df = pd.DataFrame(data)
    output = io.BytesIO()
    with pd.ExcelWriter(output, engine="openpyxl") as writer:
        df.to_excel(writer, index=False, sheet_name="Work Program")
    output.seek(0)

    from fastapi.responses import StreamingResponse
    return StreamingResponse(
        iter([output.getvalue()]),
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f"attachment; filename=work_program_{audit_id}.xlsx"}
    )


# ══════════════════════════════════════════════════════════════════════════════
# CHAT
# ══════════════════════════════════════════════════════════════════════════════

@app.get("/audits/{audit_id}/chat")
async def get_chat_history(audit_id: str, scope: str = "evidence"):
    db = get_db()
    messages = db.execute(
        "SELECT * FROM chat_messages WHERE audit_id=? AND scope=? ORDER BY created_at",
        (audit_id, scope)
    ).fetchall()
    db.close()
    return [dict(m) for m in messages]


@app.post("/audits/{audit_id}/chat")
async def send_chat_message(audit_id: str, request: Request):
    body = await request.json()
    scope = body.get("scope", "evidence")
    user_message = body.get("message", "").strip()

    if not user_message:
        raise HTTPException(400, "Message is required")

    db = get_db()
    audit = db.execute("SELECT status FROM audits WHERE id=?", (audit_id,)).fetchone()
    if audit["status"] == "closed":
        raise HTTPException(403, "Audit is closed")

    # Save user message
    user_msg_id = _short_id()
    db.execute(
        "INSERT INTO chat_messages (id, audit_id, scope, role, content, created_at) VALUES (?,?,?,?,?,?)",
        (user_msg_id, audit_id, scope, "user", user_message, _now())
    )
    db.commit()

    # Get history
    history = [dict(m) for m in db.execute(
        "SELECT role, content FROM chat_messages WHERE audit_id=? AND scope=? ORDER BY created_at",
        (audit_id, scope)
    ).fetchall()]

    from core.chat_agent import chat_response
    result = chat_response(audit_id, scope, user_message, history, db)

    # Save assistant message
    db.execute(
        "INSERT INTO chat_messages (id, audit_id, scope, role, content, citations, created_at) VALUES (?,?,?,?,?,?,?)",
        (result["message_id"], audit_id, scope, "assistant", result["content"],
         json.dumps(result["citations"]), _now())
    )
    db.commit()
    db.close()
    return result


@app.post("/audits/{audit_id}/chat/{message_id}/promote")
async def promote_chat_to_wiki(audit_id: str, message_id: str):
    db = get_db()
    from core.chat_agent import promote_to_wiki
    page_id = promote_to_wiki(audit_id, message_id, db)
    db.close()
    if not page_id:
        raise HTTPException(400, "Could not promote message to wiki")
    return {"page_id": page_id}


# ══════════════════════════════════════════════════════════════════════════════
# FINDINGS
# ══════════════════════════════════════════════════════════════════════════════

@app.get("/audits/{audit_id}/findings")
async def list_findings(audit_id: str):
    db = get_db()
    findings = db.execute(
        "SELECT * FROM findings WHERE audit_id=? ORDER BY created_at",
        (audit_id,)
    ).fetchall()
    db.close()
    return [dict(f) for f in findings]


@app.post("/audits/{audit_id}/findings/generate")
async def generate_findings_endpoint(audit_id: str, background_tasks: BackgroundTasks):
    db = get_db()
    audit = db.execute("SELECT status FROM audits WHERE id=?", (audit_id,)).fetchone()
    if audit["status"] == "closed":
        raise HTTPException(403, "Audit is closed")
    db.close()
    background_tasks.add_task(_generate_findings_bg, audit_id)
    return {"queued": True}


async def _generate_findings_bg(audit_id: str):
    db = get_db()
    try:
        from core.findings import generate_findings
        generate_findings(audit_id, db)
    finally:
        db.close()


@app.put("/audits/{audit_id}/findings/{finding_id}")
async def update_finding(audit_id: str, finding_id: str, request: Request):
    body = await request.json()
    db = get_db()
    db.execute(
        """UPDATE findings SET title=?, condition=?, criteria=?, cause=?,
           consequence=?, corrective_action=?, sub_issues=?, updated_at=?
           WHERE id=? AND audit_id=?""",
        (body.get("title"), body.get("condition"), body.get("criteria"),
         body.get("cause"), body.get("consequence"), body.get("corrective_action"),
         json.dumps(body.get("sub_issues", [])), _now(), finding_id, audit_id)
    )
    db.commit()
    db.close()
    return {"updated": True}


@app.get("/audits/{audit_id}/findings/export")
async def export_findings(audit_id: str):
    """Export findings as Word document."""
    import tempfile
    db = get_db()
    with tempfile.NamedTemporaryFile(suffix=".docx", delete=False) as tmp:
        output_path = tmp.name
    from core.findings import export_findings_docx
    export_findings_docx(audit_id, db, output_path)
    db.close()
    audit = get_db().execute("SELECT name FROM audits WHERE id=?", (audit_id,)).fetchone()
    filename = f"findings_{audit['name'].replace(' ', '_')}.docx"
    return FileResponse(output_path, filename=filename, media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document")


# ══════════════════════════════════════════════════════════════════════════════
# TOKEN USAGE
# ══════════════════════════════════════════════════════════════════════════════

@app.get("/audits/{audit_id}/tokens")
async def get_token_usage(audit_id: str):
    db = get_db()
    totals = get_audit_token_totals(audit_id, db)
    db.close()
    return totals


if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("PORT", 8000))
    uvicorn.run("main:app", host="0.0.0.0", port=port, reload=True)
