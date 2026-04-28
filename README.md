# Protiviti Operational Audit Assistant

A locally-hosted FastAPI web application that turns audit evidence files into a living, AI-synthesised wiki — and then uses that wiki to execute work programs, surface findings, and answer auditor questions.

---

## Quick Start

### 1. Clone and install

```bash
git clone https://github.com/AndrewGalazka/wikitool.git
cd wikitool
pip install -r requirements.txt
```

### 2. Configure environment

```bash
cp .env.example .env
```

Open `.env` and fill in your credentials:

| Variable | Purpose |
|---|---|
| `AZURE_ENDPOINT` | Your Azure OpenAI endpoint URL. **Leave blank to use standard OpenAI (sandbox/testing mode).** |
| `AZURE_OPENAI_DEPLOYMENT` | Azure deployment name (e.g. `gpt-4o`) |
| `API_KEY` | Azure OpenAI API key |
| `OPENAI_API_KEY` | Standard OpenAI key (used when `AZURE_ENDPOINT` is blank) |

### 3. Run the app

```bash
uvicorn main:app --reload --port 8000
```

Open [http://localhost:8000](http://localhost:8000) in your browser.

---

## LLM Dual-Path

The app automatically detects which LLM provider to use at startup:

- **Azure OpenAI** — used when `AZURE_ENDPOINT` is set in `.env`. This is the recommended path for local corporate use.
- **Standard OpenAI** — used when `AZURE_ENDPOINT` is blank. This is the default for sandbox/testing.

No code changes are needed to switch between them — just update `.env`.

---

## Features

### Audit Dashboard
Create, view, close, reopen, and delete audits. Each audit has its own isolated workspace, database, and file storage.

### Sources (File Ingestion)
Upload evidence and guidance documents. Supported formats:

| Category | Formats |
|---|---|
| Documents | PDF, Word (.docx), PowerPoint (.pptx), Excel (.xlsx), CSV |
| Images | PNG, JPG, TIFF (OCR via Markitdown) |
| Web | HTML, Markdown, plain text |

Files are converted to Markdown in the background using [Markitdown](https://github.com/microsoft/markitdown). A **lint pass** runs automatically after every ingestion — the LLM scans the wiki for contradictions and gaps introduced by the new file.

Duplicate file detection is built in. If you upload a file with the same name or content hash, you can skip, version, or replace.

### Evidence Wiki
AI-synthesised wiki built from all ingested evidence files. Pages are categorised by type:

- **Source** — one page per ingested file summarising its content
- **Person** — individuals mentioned across evidence
- **Process** — business processes identified
- **Control** — controls referenced or tested
- **System** — IT systems and applications
- **Evidence Area** — thematic groupings of evidence
- **Finding** — issues surfaced during ingestion or work program execution

Each page shows backlinks, issues flagged by the lint pass, and provenance (which source files contributed).

### Guidance Browser
Guidance documents (standards, policies, frameworks) are stored as faithful Markdown copies and are never synthesised into the wiki. Browse and read them in a dedicated tab.

### Knowledge Graph
Interactive force-directed graph of all wiki entities and their relationships. Rebuilt on demand via the **Refresh Graph** button. Nodes with issues are highlighted in amber. Filter by entity type or show issues-only view.

### Work Program
Upload a work program as `.xlsx` or `.csv`. The app **auto-detects** the test ID and description columns — no manual mapping required on first upload. You can adjust the mapping later if detection is incorrect.

For each row, an AI agent:
1. Searches the evidence wiki for relevant content
2. Drafts a conclusion with cited evidence references
3. Flags open questions if evidence is insufficient
4. Sets status to `completed`, `pending_evidence`, or `open_questions`

Rows can be run individually or all at once. Verified rows are locked and skipped on re-runs. Export the completed work program to Excel.

### Chat
Ask questions about your audit evidence (wiki scope) or guidance documents (guidance scope). Responses include source citations. Useful answers can be promoted directly to the wiki.

### Findings
Generate structured findings from completed work program rows. Each finding follows the **5C framework**: Condition, Criteria, Cause, Consequence, Corrective Action. Edit findings in-place and export to Word (.docx).

### Token Usage
A live token counter in the header tracks total LLM token consumption for the audit session.

---

## Project Structure

```
wikitool/
├── main.py                  # FastAPI app — all routes
├── core/
│   ├── llm_client.py        # Dual-path LLM client (Azure / OpenAI)
│   ├── database.py          # SQLite schema and helpers
│   ├── ingestion.py         # File conversion and wiki synthesis pipeline
│   ├── wiki_agent.py        # Wiki page synthesis and lint pass
│   ├── work_program_agent.py# Per-row agent loop
│   ├── chat_agent.py        # Chat with evidence / guidance scope
│   ├── graph_builder.py     # Knowledge graph derivation
│   ├── findings.py          # Findings generation and Word export
│   └── token_tracker.py     # Token usage tracking
├── templates/
│   ├── dashboard.html       # Audit list and creation
│   └── workspace.html       # Full audit workspace (all tabs)
├── static/
│   ├── css/
│   │   ├── style.css        # Base styles and Protiviti branding
│   │   └── workspace.css    # Workspace-specific styles
│   └── js/
│       ├── dashboard.js     # Dashboard interactions
│       ├── workspace.js     # All workspace tab logic
│       └── graph.js         # vis-network graph rendering
├── data/                    # Auto-created — SQLite DB and uploaded files
├── requirements.txt
├── .env.example
└── README.md
```

---

## Data Storage

All data is stored locally in the `data/` directory:

- `data/audit_assistant.db` — SQLite database (audits, sources, wiki, work program, chat, tokens)
- `data/uploads/<audit_id>/` — original uploaded files
- `data/markdown/<audit_id>/` — Markitdown-converted files

The `data/` directory is gitignored. **Back it up separately** if you need to preserve audit data.

---

## Notes

- The app runs entirely on your laptop — no cloud services required beyond the LLM API.
- The knowledge graph is rebuilt on demand only (not after every upload) to avoid performance overhead.
- Lint runs after every individual file ingestion. On large upload sessions, this will accumulate LLM token costs — monitor the token counter in the header.
- Audio and video file ingestion are not supported by design.
