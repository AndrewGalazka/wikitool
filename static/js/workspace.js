/* workspace.js — Protiviti Operational Audit Assistant */

/* ── Toast ───────────────────────────────────────────────────────────────── */
function toast(msg, type = 'info') {
  let container = document.getElementById('toastContainer');
  if (!container) {
    container = document.createElement('div');
    container.id = 'toastContainer';
    container.className = 'toast-container';
    document.body.appendChild(container);
  }
  const t = document.createElement('div');
  t.className = `toast toast-${type}`;
  t.textContent = msg;
  container.appendChild(t);
  setTimeout(() => t.remove(), 4000);
}

/* ── Tab Switching ───────────────────────────────────────────────────────── */
function switchTab(name) {
  document.querySelectorAll('.tab-btn').forEach(b => b.classList.remove('active'));
  document.querySelectorAll('.tab-pane').forEach(p => p.classList.remove('active'));
  document.querySelector(`[data-tab="${name}"]`).classList.add('active');
  document.getElementById(`tab-${name}`).classList.add('active');

  if (name === 'wiki')        loadWiki();
  if (name === 'guidance')    loadGuidance();
  if (name === 'workprogram') loadWorkProgram();
  if (name === 'findings')    loadFindings();
  if (name === 'chat')        loadChat();
}

/* ── Audit Lifecycle ─────────────────────────────────────────────────────── */
async function closeAudit() {
  if (!confirm('Close this audit? Uploads and agent runs will be disabled.')) return;
  await fetch(`/audits/${AUDIT_ID}/close`, { method: 'POST' });
  location.reload();
}

async function reopenAudit() {
  await fetch(`/audits/${AUDIT_ID}/reopen`, { method: 'POST' });
  location.reload();
}

function closeModal(id) {
  document.getElementById(id).style.display = 'none';
}

/* ── File Upload ─────────────────────────────────────────────────────────── */
let _pendingDuplicate = null;

function initUpload() {
  const zone = document.getElementById('uploadZone');
  const input = document.getElementById('fileInput');

  zone.addEventListener('dragover', e => { e.preventDefault(); zone.classList.add('drag-over'); });
  zone.addEventListener('dragleave', () => zone.classList.remove('drag-over'));
  zone.addEventListener('drop', e => {
    e.preventDefault();
    zone.classList.remove('drag-over');
    handleFiles(e.dataTransfer.files);
  });

  input.addEventListener('change', () => handleFiles(input.files));
}

function getUploadType() {
  return document.querySelector('input[name="uploadType"]:checked').value;
}

async function handleFiles(files) {
  if (AUDIT_STATUS === 'closed') { toast('Audit is closed — uploads not allowed.', 'error'); return; }
  for (const file of files) {
    await uploadFile(file, getUploadType());
  }
}

async function uploadFile(file, fileType) {
  const fd = new FormData();
  fd.append('file', file);
  fd.append('file_type', fileType);

  const resp = await fetch(`/audits/${AUDIT_ID}/upload`, { method: 'POST', body: fd });
  const data = await resp.json();

  if (data.duplicate) {
    _pendingDuplicate = { existingId: data.existing_id, filename: data.filename, fileType };
    document.getElementById('dupFilename').textContent = data.filename;
    document.getElementById('duplicateModal').style.display = 'flex';
    return;
  }

  addSourceToList(data.source_id, file.name, fileType, 'pending');
  pollSourceStatus(data.source_id);
}

async function resolveDuplicate(action) {
  closeModal('duplicateModal');
  if (!_pendingDuplicate) return;
  const { existingId, filename, fileType } = _pendingDuplicate;

  const fd = new FormData();
  fd.append('existing_id', existingId);
  fd.append('filename', filename);
  fd.append('file_type', fileType);
  fd.append('action', action);

  const resp = await fetch(`/audits/${AUDIT_ID}/upload/resolve-duplicate`, { method: 'POST', body: fd });
  const data = await resp.json();

  if (data.action !== 'skipped') {
    addSourceToList(data.source_id, filename, fileType, 'pending');
    pollSourceStatus(data.source_id);
  }
  _pendingDuplicate = null;
}

function addSourceToList(sourceId, filename, fileType, status) {
  const list = document.getElementById('sourcesList');
  const icon = fileType === 'evidence' ? '📄' : '📖';
  const div = document.createElement('div');
  div.className = 'source-item';
  div.id = `source-${sourceId}`;
  div.innerHTML = `
    <div class="source-icon">${icon}</div>
    <div class="source-info">
      <div class="source-name">${filename}</div>
      <div class="source-meta text-muted text-sm">${fileType.charAt(0).toUpperCase() + fileType.slice(1)} &bull; just now</div>
    </div>
    <div class="source-status">
      <span class="status-badge status-${status}" id="status-${sourceId}">${status.charAt(0).toUpperCase() + status.slice(1)}</span>
    </div>`;
  list.prepend(div);
}

function pollSourceStatus(sourceId) {
  const interval = setInterval(async () => {
    const resp = await fetch(`/audits/${AUDIT_ID}/sources/${sourceId}/status`);
    if (!resp.ok) { clearInterval(interval); return; }
    const data = await resp.json();
    const badge = document.getElementById(`status-${sourceId}`);
    if (badge) {
      badge.className = `status-badge status-${data.status}`;
      badge.textContent = data.status.charAt(0).toUpperCase() + data.status.slice(1);
    }
    if (data.status === 'ready') {
      clearInterval(interval);
      toast(`File ready: ${sourceId.slice(0, 8)}...`, 'success');
      refreshTokenCounter();
    } else if (data.status === 'failed') {
      clearInterval(interval);
      toast('File conversion failed.', 'error');
    }
  }, 3000);
}

async function refreshTokenCounter() {
  const resp = await fetch(`/audits/${AUDIT_ID}/tokens`);
  if (!resp.ok) return;
  const data = await resp.json();
  document.getElementById('tokenTotal').textContent = (data.grand_total || 0).toLocaleString();
}

/* ── Wiki ────────────────────────────────────────────────────────────────── */
let _wikiPages = [];
let _currentPageId = null;

async function loadWiki(typeFilter = '', query = '') {
  let url = `/audits/${AUDIT_ID}/wiki`;
  if (query) url += `?q=${encodeURIComponent(query)}`;
  else if (typeFilter) url += `?page_type=${typeFilter}`;

  const resp = await fetch(url);
  _wikiPages = await resp.json();
  renderWikiSidebar(_wikiPages);
}

function renderWikiSidebar(pages) {
  const sidebar = document.getElementById('wikiSidebar');
  if (!pages.length) {
    sidebar.innerHTML = '<div class="text-muted" style="padding:16px">No wiki pages yet.</div>';
    return;
  }

  const grouped = {};
  for (const p of pages) {
    if (!grouped[p.page_type]) grouped[p.page_type] = [];
    grouped[p.page_type].push(p);
  }

  const typeLabels = { source:'Sources', person:'People', process:'Processes', control:'Controls',
                       system:'Systems', evidence_area:'Evidence Areas', finding:'Findings' };

  let html = '';
  for (const [type, items] of Object.entries(grouped)) {
    html += `<div class="wiki-type-group">
      <div class="wiki-type-header">${typeLabels[type] || type}</div>`;
    for (const p of items) {
      html += `<div class="wiki-page-item ${p.id === _currentPageId ? 'active' : ''}"
                    onclick="openWikiPage('${p.id}')" data-id="${p.id}">
        <div class="wiki-page-title">${escHtml(p.title)}</div>
      </div>`;
    }
    html += '</div>';
  }
  sidebar.innerHTML = html;
}

async function openWikiPage(pageId) {
  _currentPageId = pageId;
  document.querySelectorAll('.wiki-page-item').forEach(el => {
    el.classList.toggle('active', el.dataset.id === pageId);
  });

  const resp = await fetch(`/audits/${AUDIT_ID}/wiki/${pageId}`);
  const page = await resp.json();

  const typeLabels = { source:'Source', person:'Person', process:'Process', control:'Control',
                       system:'System', evidence_area:'Evidence Area', finding:'Finding' };

  let issuesHtml = '';
  if (page.issues && page.issues.length) {
    issuesHtml = '<div class="wiki-issues">';
    for (const i of page.issues) {
      issuesHtml += `<div class="issue-chip">
        <div><div class="issue-type">${i.issue_type.replace('_',' ')}</div><div>${escHtml(i.description)}</div></div>
      </div>`;
    }
    issuesHtml += '</div>';
  }

  let backlinksHtml = '';
  if (page.backlinks && page.backlinks.length) {
    backlinksHtml = '<div class="wiki-backlinks"><h4>Referenced by</h4>';
    for (const b of page.backlinks) {
      backlinksHtml += `<span class="backlink-chip" onclick="openWikiPage('${b.id}')">${escHtml(b.title)}</span>`;
    }
    backlinksHtml += '</div>';
  }

  document.getElementById('wikiViewer').innerHTML = `
    <div class="wiki-page-content">
      <div class="wiki-meta-bar">
        <span class="badge badge-active">${typeLabels[page.page_type] || page.page_type}</span>
        <span class="text-muted text-sm">Updated ${page.updated_at.slice(0,10)}</span>
      </div>
      ${issuesHtml}
      <div id="wikiPageBody">${renderMarkdown(page.content)}</div>
      ${backlinksHtml}
    </div>`;
}

function searchWiki(q) {
  clearTimeout(window._wikiSearchTimer);
  window._wikiSearchTimer = setTimeout(() => loadWiki('', q), 350);
}

function filterWikiByType(type) {
  loadWiki(type);
}

/* ── Wiki Sub-tabs (Karpathy LLM-Wiki: Pages / Index / Log) ────────────── */
let _currentWikiSubtab = 'pages';
function switchWikiSubtab(name) {
  _currentWikiSubtab = name;
  document.querySelectorAll('.wiki-subtab').forEach(btn => btn.classList.remove('active'));
  const activeBtn = document.getElementById('wikiSubtab' + name.charAt(0).toUpperCase() + name.slice(1));
  if (activeBtn) activeBtn.classList.add('active');
  document.getElementById('wikiSubPanePages').style.display = name === 'pages' ? '' : 'none';
  document.getElementById('wikiSubPaneIndex').style.display = name === 'index' ? '' : 'none';
  document.getElementById('wikiSubPaneLog').style.display   = name === 'log'   ? '' : 'none';
  const searchEl = document.getElementById('wikiSearch');
  const filterEl = document.getElementById('wikiTypeFilter');
  if (searchEl) searchEl.style.display = name === 'pages' ? '' : 'none';
  if (filterEl) filterEl.style.display = name === 'pages' ? '' : 'none';
  if (name === 'index') loadWikiIndex();
  if (name === 'log')   loadWikiLog();
}

async function loadWikiIndex() {
  const el = document.getElementById('wikiIndexContent');
  const updEl = document.getElementById('wikiIndexUpdated');
  el.innerHTML = '<div class="text-muted">Loading index...</div>';
  try {
    const resp = await fetch(`/audits/${AUDIT_ID}/wiki-index`);
    const data = await resp.json();
    if (!data.content) {
      el.innerHTML = '<div class="text-muted">No index yet. Upload evidence files to build the wiki.</div>';
      return;
    }
    el.innerHTML = renderMarkdown(data.content);
    if (updEl && data.updated_at) updEl.textContent = 'Last rebuilt: ' + data.updated_at.slice(0, 10);
  } catch(e) {
    el.innerHTML = '<div class="text-muted">Failed to load index.</div>';
  }
}

async function rebuildWikiIndex() {
  const el = document.getElementById('wikiIndexContent');
  const updEl = document.getElementById('wikiIndexUpdated');
  el.innerHTML = '<div class="text-muted">Rebuilding index...</div>';
  try {
    const resp = await fetch(`/audits/${AUDIT_ID}/wiki-index/rebuild`, { method: 'POST' });
    const data = await resp.json();
    el.innerHTML = renderMarkdown(data.content || '');
    if (updEl) updEl.textContent = 'Rebuilt: ' + new Date().toISOString().slice(0, 10);
    toast('Index rebuilt.', 'success');
  } catch(e) {
    el.innerHTML = '<div class="text-muted">Rebuild failed.</div>';
    toast('Index rebuild failed.', 'error');
  }
}

async function loadWikiLog() {
  const el = document.getElementById('wikiLogContent');
  el.innerHTML = '<div class="text-muted">Loading log...</div>';
  try {
    const resp = await fetch(`/audits/${AUDIT_ID}/wiki-log?limit=100`);
    const data = await resp.json();
    el.innerHTML = data.log ? renderMarkdown(data.log) : '<div class="text-muted">No log entries yet.</div>';
  } catch(e) {
    el.innerHTML = '<div class="text-muted">Failed to load log.</div>';
  }
}

/* ── Guidance ────────────────────────────────────────────────────────────── */
async function loadGuidance() {
  const resp = await fetch(`/audits/${AUDIT_ID}/sources`);
  const sources = await resp.json();
  const guidance = sources.filter(s => s.file_type === 'guidance' && s.status === 'ready');

  const list = document.getElementById('guidanceList');
  if (!guidance.length) {
    list.innerHTML = '<div class="text-muted" style="padding:16px">No guidance documents ready yet.</div>';
    return;
  }

  list.innerHTML = guidance.map(g => `
    <div class="guidance-item" data-id="${g.id}">
      <div class="guidance-item-name" onclick="openGuidanceDoc('${g.id}', '${escHtml(g.filename)}')">📖 ${escHtml(g.filename)}</div>
      <button class="guidance-delete-btn" title="Delete document" onclick="deleteGuidanceDoc('${g.id}', '${escHtml(g.filename)}')">&#128465;</button>
    </div>`).join('');
}

async function openGuidanceDoc(sourceId, filename) {
  document.querySelectorAll('.guidance-item').forEach(el => {
    el.classList.toggle('active', el.dataset.id === sourceId);
  });
  document.getElementById('guidanceViewer').innerHTML = '<div class="text-muted">Loading...</div>';

  // Fetch the markdown content via a dedicated endpoint
  const resp = await fetch(`/audits/${AUDIT_ID}/sources/${sourceId}/content`);
  if (!resp.ok) {
    document.getElementById('guidanceViewer').innerHTML = '<div class="text-muted">Could not load document.</div>';
    return;
  }
  const data = await resp.json();
  document.getElementById('guidanceViewer').innerHTML = `
    <h2 style="margin-bottom:16px">${escHtml(filename)}</h2>
    <div>${renderMarkdown(data.content)}</div>`;
}

async function deleteGuidanceDoc(sourceId, filename) {
  if (!confirm(`Delete "${filename}"?\n\nThis will remove the document and any wiki pages derived from it. This cannot be undone.`)) return;

  const resp = await fetch(`/audits/${AUDIT_ID}/sources/${sourceId}`, { method: 'DELETE' });
  if (!resp.ok) { toast('Delete failed.', 'error'); return; }

  // Clear viewer if this doc was open
  const viewer = document.getElementById('guidanceViewer');
  if (viewer && document.querySelector(`.guidance-item.active[data-id="${sourceId}"]`)) {
    viewer.innerHTML = '<div class="guidance-empty text-muted">Select a document to view it.</div>';
  }

  toast(`"${filename}" deleted.`, 'success');
  loadGuidance();
}

/* ── Work Program — Auto-detect, no confirmation screen ─────────────────── */
let _wpFilename = null;
let _wpMapping  = null;

function openWPUpload() {
  const section = document.getElementById('wpUploadSection');
  section.style.display = section.style.display === 'none' ? 'block' : 'none';
}

document.addEventListener('DOMContentLoaded', () => {
  initUpload();

  const wpInput = document.getElementById('wpFileInput');
  if (wpInput) {
    wpInput.addEventListener('change', async () => {
      const file = wpInput.files[0];
      if (!file) return;
      document.getElementById('wpFileName').textContent = file.name;

      const fd = new FormData();
      fd.append('file', file);
      const resp = await fetch(`/audits/${AUDIT_ID}/work-program/upload`, { method: 'POST', body: fd });
      if (!resp.ok) { toast('Failed to parse work program file.', 'error'); return; }

      const data = await resp.json();
      _wpFilename = data.filename;
      _wpMapping  = data.suggested_mapping;

      // Auto-confirm immediately — no mapper screen shown
      await _confirmWorkProgram();
    });
  }
});

async function _confirmWorkProgram() {
  const resp = await fetch(`/audits/${AUDIT_ID}/work-program/confirm`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ filename: _wpFilename, mapping: _wpMapping }),
  });
  if (!resp.ok) { toast('Failed to load work program.', 'error'); return; }
  const data = await resp.json();
  toast(`Work program loaded: ${data.rows_loaded} rows`, 'success');
  document.getElementById('wpUploadSection').style.display = 'none';
  loadWorkProgram();
}

async function loadWorkProgram() {
  const resp = await fetch(`/audits/${AUDIT_ID}/work-program/rows`);
  const rows = await resp.json();
  renderWPTable(rows);
}

function renderWPTable(rows) {
  const tbody = document.getElementById('wpTableBody');
  if (!rows.length) {
    tbody.innerHTML = '<tr><td colspan="5" class="text-muted text-center">No work program loaded.</td></tr>';
    return;
  }

  tbody.innerHTML = rows.map(r => `
    <tr class="${r.verified ? 'wp-row-verified' : ''}" id="wprow-${r.id}">
      <td>${escHtml(r.test_id)}</td>
      <td>${escHtml(r.description.slice(0, 80))}${r.description.length > 80 ? '…' : ''}</td>
      <td><span class="wp-status wp-status-${r.status}">${r.status.replace('_',' ')}</span></td>
      <td>${r.verified ? '<span class="verified-check">✓</span>' : ''}</td>
      <td>
        <button class="btn btn-sm btn-outline" onclick="openRowModal('${r.id}')">View</button>
        ${!r.verified ? `<button class="btn btn-sm btn-primary" onclick="runSingleRow('${r.id}')">Run</button>` : ''}
      </td>
    </tr>`).join('');
}

async function runSingleRow(rowId) {
  const resp = await fetch(`/audits/${AUDIT_ID}/work-program/rows/${rowId}/run`, { method: 'POST' });
  const data = await resp.json();
  if (data.skipped) { toast('Row is verified — skipped.', 'info'); return; }
  toast('Row queued for execution.', 'info');
  pollRowStatus(rowId);
}

async function runAllRows() {
  const resp = await fetch(`/audits/${AUDIT_ID}/work-program/run-all`, { method: 'POST' });
  const data = await resp.json();
  toast(`${data.queued} rows queued.`, 'info');
  setTimeout(loadWorkProgram, 2000);
  setTimeout(loadWorkProgram, 8000);
  setTimeout(() => { loadWorkProgram(); refreshTokenCounter(); }, 20000);
}

function pollRowStatus(rowId) {
  const interval = setInterval(async () => {
    const resp = await fetch(`/audits/${AUDIT_ID}/work-program/rows/${rowId}`);
    if (!resp.ok) { clearInterval(interval); return; }
    const row = await resp.json();
    const tr = document.getElementById(`wprow-${rowId}`);
    if (tr) {
      const badge = tr.querySelector('.wp-status');
      if (badge) {
        badge.className = `wp-status wp-status-${row.status}`;
        badge.textContent = row.status.replace('_', ' ');
      }
    }
    if (!['pending','running'].includes(row.status)) {
      clearInterval(interval);
      refreshTokenCounter();
    }
  }, 3000);
}

/* Row Detail Modal */
let _currentRowId = null;

async function openRowModal(rowId) {
  _currentRowId = rowId;
  const resp = await fetch(`/audits/${AUDIT_ID}/work-program/rows/${rowId}`);
  const row = await resp.json();

  document.getElementById('rowModalTitle').textContent = `Test ${row.test_id}`;

  let refsHtml = '';
  try {
    const refs = JSON.parse(row.evidence_references || '[]');
    if (refs.length) {
      refsHtml = refs.map(r => `<div class="citation-item">
        <div class="citation-source">${escHtml(r.source_file || '')}</div>
        <div class="citation-location text-muted">${escHtml(r.location || '')}</div>
        ${r.quote ? `<div class="citation-quote">"${escHtml(r.quote)}"</div>` : ''}
      </div>`).join('');
    }
  } catch(e) {}

  let oqHtml = '';
  try {
    const oqs = JSON.parse(row.open_questions || '[]');
    if (oqs.length) oqHtml = '<ul>' + oqs.map(q => `<li>${escHtml(q)}</li>`).join('') + '</ul>';
  } catch(e) {}

  document.getElementById('rowModalBody').innerHTML = `
    <div class="row-detail-section">
      <h4>Description</h4>
      <p>${escHtml(row.description)}</p>
      ${row.objective ? `<p class="text-muted text-sm">Objective: ${escHtml(row.objective)}</p>` : ''}
    </div>
    <div class="row-detail-section">
      <h4>Conclusion</h4>
      <textarea id="rowConclusion" rows="5">${row.conclusion || ''}</textarea>
    </div>
    ${refsHtml ? `<div class="row-detail-section"><h4>Evidence References</h4>${refsHtml}</div>` : ''}
    ${oqHtml ? `<div class="row-detail-section"><h4>Open Questions</h4>${oqHtml}</div>` : ''}
    <div class="row-detail-section">
      <h4>Human Notes</h4>
      <textarea id="rowNotes" rows="3">${row.human_notes || ''}</textarea>
    </div>
    <div class="row-detail-section">
      <label style="display:flex;align-items:center;gap:8px;cursor:pointer">
        <input type="checkbox" id="rowVerified" ${row.verified ? 'checked' : ''}>
        <strong>Mark as Verified</strong> (locks row from future agent runs)
      </label>
    </div>`;

  document.getElementById('rowModal').style.display = 'flex';
}

async function saveRowEdits() {
  const conclusion = document.getElementById('rowConclusion').value;
  const notes      = document.getElementById('rowNotes').value;
  const verified   = document.getElementById('rowVerified').checked ? 1 : 0;

  await fetch(`/audits/${AUDIT_ID}/work-program/rows/${_currentRowId}`, {
    method: 'PUT',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ conclusion, human_notes: notes, verified }),
  });

  closeModal('rowModal');
  toast('Row saved.', 'success');
  loadWorkProgram();
}

async function exportWorkProgram() {
  window.location.href = `/audits/${AUDIT_ID}/work-program/export`;
}

/* ── Chat ────────────────────────────────────────────────────────────────── */
let _chatScope = 'evidence';

function setChatScope(scope) {
  _chatScope = scope;
  loadChat();
}

async function loadChat() {
  const resp = await fetch(`/audits/${AUDIT_ID}/chat?scope=${_chatScope}`);
  const messages = await resp.json();
  const container = document.getElementById('chatMessages');

  if (!messages.length) {
    container.innerHTML = '<div class="chat-welcome"><p>Ask questions about your audit evidence or guidance documents.</p></div>';
    return;
  }

  container.innerHTML = messages.map(m => renderChatMsg(m)).join('');
  container.scrollTop = container.scrollHeight;
}

function renderChatMsg(m) {
  const citations = m.citations ? JSON.parse(m.citations) : [];
  const citHtml = citations.length
    ? `<div class="chat-citations">${citations.map(c => `<span class="citation-tag">${escHtml(c)}</span>`).join('')}</div>`
    : '';
  const promoteBtn = (m.role === 'assistant' && _chatScope === 'evidence')
    ? `<button class="promote-btn" onclick="promoteToWiki('${m.id}')">+ Promote to Wiki</button>`
    : '';
  return `<div class="chat-msg chat-msg-${m.role}">
    <div class="chat-bubble">${renderMarkdown(m.content)}</div>
    ${citHtml}${promoteBtn}
  </div>`;
}

async function sendChatMessage() {
  const input = document.getElementById('chatInput');
  const msg = input.value.trim();
  if (!msg) return;
  input.value = '';

  // Optimistically render user message
  const container = document.getElementById('chatMessages');
  container.innerHTML += `<div class="chat-msg chat-msg-user"><div class="chat-bubble">${escHtml(msg)}</div></div>`;
  container.innerHTML += `<div class="chat-msg chat-msg-assistant" id="chatTyping"><div class="chat-bubble"><span class="spinner"></span> Thinking...</div></div>`;
  container.scrollTop = container.scrollHeight;

  const resp = await fetch(`/audits/${AUDIT_ID}/chat`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ message: msg, scope: _chatScope }),
  });

  document.getElementById('chatTyping')?.remove();

  if (!resp.ok) { toast('Chat error.', 'error'); return; }
  const data = await resp.json();

  const citations = data.citations || [];
  const citHtml = citations.length
    ? `<div class="chat-citations">${citations.map(c => `<span class="citation-tag">${escHtml(c)}</span>`).join('')}</div>`
    : '';
  const promoteBtn = _chatScope === 'evidence'
    ? `<button class="promote-btn" onclick="promoteToWiki('${data.message_id}')">+ Promote to Wiki</button>`
    : '';

  container.innerHTML += `<div class="chat-msg chat-msg-assistant">
    <div class="chat-bubble">${renderMarkdown(data.content)}</div>
    ${citHtml}${promoteBtn}
  </div>`;
  container.scrollTop = container.scrollHeight;
  refreshTokenCounter();
}

function handleChatKey(e) {
  if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); sendChatMessage(); }
}

async function promoteToWiki(messageId) {
  const resp = await fetch(`/audits/${AUDIT_ID}/chat/${messageId}/promote`, { method: 'POST' });
  if (resp.ok) toast('Promoted to wiki.', 'success');
  else toast('Could not promote message.', 'error');
}

/* ── Findings ────────────────────────────────────────────────────────────── */
async function loadFindings() {
  const resp = await fetch(`/audits/${AUDIT_ID}/findings`);
  const findings = await resp.json();
  const list = document.getElementById('findingsList');

  if (!findings.length) {
    list.innerHTML = '<div class="text-muted">No findings yet. Generate findings from completed work program rows.</div>';
    return;
  }

  list.innerHTML = findings.map((f, i) => `
    <div class="finding-card">
      <div class="finding-card-header" onclick="toggleFinding('finding-body-${f.id}')">
        <h3>Finding ${i + 1}: ${escHtml(f.title)}</h3>
        <span>&#9660;</span>
      </div>
      <div class="finding-card-body" id="finding-body-${f.id}">
        <div class="finding-5c-grid">
          <div class="finding-field"><label>Condition</label><textarea id="fc-${f.id}-condition">${f.condition || ''}</textarea></div>
          <div class="finding-field"><label>Criteria</label><textarea id="fc-${f.id}-criteria">${f.criteria || ''}</textarea></div>
          <div class="finding-field"><label>Cause</label><textarea id="fc-${f.id}-cause">${f.cause || ''}</textarea></div>
          <div class="finding-field"><label>Consequence</label><textarea id="fc-${f.id}-consequence">${f.consequence || ''}</textarea></div>
        </div>
        <div class="finding-field" style="margin-bottom:12px"><label>Corrective Action</label><textarea id="fc-${f.id}-corrective_action">${f.corrective_action || ''}</textarea></div>
        <div class="finding-actions">
          <button class="btn btn-sm btn-outline" onclick="saveFinding('${f.id}', '${escHtml(f.title)}')">Save</button>
        </div>
      </div>
    </div>`).join('');
}

function toggleFinding(bodyId) {
  const el = document.getElementById(bodyId);
  el.style.display = el.style.display === 'none' ? 'block' : 'none';
}

async function saveFinding(id, title) {
  const fields = ['condition', 'criteria', 'cause', 'consequence', 'corrective_action'];
  const body = { title };
  for (const f of fields) {
    const el = document.getElementById(`fc-${id}-${f}`);
    if (el) body[f] = el.value;
  }
  await fetch(`/audits/${AUDIT_ID}/findings/${id}`, {
    method: 'PUT',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  });
  toast('Finding saved.', 'success');
}

async function generateFindings() {
  await fetch(`/audits/${AUDIT_ID}/findings/generate`, { method: 'POST' });
  toast('Generating findings...', 'info');
  setTimeout(() => { loadFindings(); refreshTokenCounter(); }, 8000);
}

function exportFindings() {
  window.location.href = `/audits/${AUDIT_ID}/findings/export`;
}

/* ── Markdown renderer (minimal) ─────────────────────────────────────────── */
function renderMarkdown(text) {
  if (!text) return '';
  return text
    .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
    .replace(/^#### (.+)$/gm, '<h4>$1</h4>')
    .replace(/^### (.+)$/gm, '<h3>$1</h3>')
    .replace(/^## (.+)$/gm, '<h2>$1</h2>')
    .replace(/^# (.+)$/gm, '<h1>$1</h1>')
    .replace(/\*\*(.+?)\*\*/g, '<strong>$1</strong>')
    .replace(/\*(.+?)\*/g, '<em>$1</em>')
    .replace(/`(.+?)`/g, '<code>$1</code>')
    .replace(/^- (.+)$/gm, '<li>$1</li>')
    .replace(/(<li>.*<\/li>)/gs, '<ul>$1</ul>')
    .replace(/\n\n/g, '</p><p>')
    .replace(/^(?!<[hup])/gm, '')
    .replace(/\n/g, '<br>');
}

function escHtml(str) {
  if (!str) return '';
  return String(str).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;');
}
