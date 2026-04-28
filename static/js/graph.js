/* graph.js — Knowledge Graph using vis-network */

let _network = null;
let _graphData = null;

async function refreshGraph() {
  const resp = await fetch(`/audits/${AUDIT_ID}/graph`);
  _graphData = await resp.json();
  renderGraph(_graphData);
}

function filterGraph() {
  if (!_graphData) return;
  const typeFilter = document.getElementById('graphTypeFilter').value;
  const issuesOnly = document.getElementById('showIssuesOnly').checked;

  let nodes = _graphData.nodes;
  let edges = _graphData.edges;

  if (typeFilter) nodes = nodes.filter(n => n.type === typeFilter);
  if (issuesOnly) nodes = nodes.filter(n => n.has_issues);

  const nodeIds = new Set(nodes.map(n => n.id));
  edges = edges.filter(e => nodeIds.has(e.source) && nodeIds.has(e.target));

  renderGraph({ nodes, edges, issues: _graphData.issues });
}

function renderGraph(data) {
  const container = document.getElementById('graphCanvas');
  if (!container) return;

  const nodes = new vis.DataSet(data.nodes.map(n => ({
    id: n.id,
    label: n.label.length > 25 ? n.label.slice(0, 22) + '…' : n.label,
    title: n.label,
    color: {
      background: n.has_issues ? '#F39C12' : n.color,
      border:     n.has_issues ? '#E74C3C' : darken(n.color),
      highlight:  { background: n.color, border: '#1A1A2E' },
    },
    borderWidth: n.has_issues ? 3 : 1,
    font: { size: 12, color: '#1A1A2E' },
    shape: shapeFor(n.type),
  })));

  const edges = new vis.DataSet(data.edges.map((e, i) => ({
    id: i,
    from: e.source,
    to: e.target,
    label: e.label,
    arrows: 'to',
    color: { color: '#CBD5E0', highlight: '#D0021B' },
    font: { size: 10, color: '#6B7280', align: 'middle' },
    smooth: { type: 'continuous' },
  })));

  const options = {
    physics: {
      enabled: true,
      barnesHut: { gravitationalConstant: -4000, springLength: 120, damping: 0.5 },
    },
    interaction: { hover: true, tooltipDelay: 200 },
    layout: { improvedLayout: true },
  };

  if (_network) _network.destroy();
  _network = new vis.Network(container, { nodes, edges }, options);

  _network.on('click', (params) => {
    if (params.nodes.length > 0) {
      const nodeId = params.nodes[0];
      openGraphSidebar(nodeId);
    }
  });

  if (!data.nodes.length) {
    container.innerHTML = '<div style="display:flex;align-items:center;justify-content:center;height:100%;color:#6B7280">No wiki pages yet. Upload evidence files to build the graph.</div>';
  }
}

async function openGraphSidebar(pageId) {
  const sidebar = document.getElementById('graphSidebar');
  sidebar.innerHTML = '<div class="text-muted">Loading...</div>';

  const resp = await fetch(`/audits/${AUDIT_ID}/wiki/${pageId}`);
  if (!resp.ok) { sidebar.innerHTML = '<div class="text-muted">Could not load page.</div>'; return; }
  const page = await resp.json();

  const issuesHtml = (page.issues || []).map(i => `
    <div style="padding:6px 8px;background:#FFF3CD;border-radius:4px;font-size:12px;margin-bottom:4px">
      <strong>${i.issue_type.replace('_',' ')}</strong>: ${escHtml(i.description)}
    </div>`).join('');

  sidebar.innerHTML = `
    <h3 style="font-size:14px;font-weight:600;margin-bottom:8px">${escHtml(page.title)}</h3>
    <div class="badge badge-active" style="margin-bottom:12px">${page.page_type.replace('_',' ')}</div>
    ${issuesHtml}
    <div style="font-size:13px;line-height:1.6;margin-top:12px">${renderMarkdown(page.content.slice(0, 600))}${page.content.length > 600 ? '…' : ''}</div>
    <button class="btn btn-sm btn-outline" style="margin-top:12px" onclick="switchTab('wiki');openWikiPage('${page.id}')">Open Full Page</button>`;
}

function exportIssues() {
  window.location.href = `/audits/${AUDIT_ID}/graph/export-issues`;
}

function shapeFor(type) {
  const shapes = { source:'box', person:'ellipse', process:'diamond',
                   control:'hexagon', system:'database', evidence_area:'dot', finding:'star' };
  return shapes[type] || 'dot';
}

function darken(hex) {
  if (!hex || hex.length < 7) return '#000';
  const r = Math.max(0, parseInt(hex.slice(1,3),16) - 30);
  const g = Math.max(0, parseInt(hex.slice(3,5),16) - 30);
  const b = Math.max(0, parseInt(hex.slice(5,7),16) - 30);
  return `#${r.toString(16).padStart(2,'0')}${g.toString(16).padStart(2,'0')}${b.toString(16).padStart(2,'0')}`;
}
