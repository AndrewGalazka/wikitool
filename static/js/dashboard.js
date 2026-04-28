/* dashboard.js — Protiviti Operational Audit Assistant */

let _deleteAuditId = null;

function openNewAuditModal() {
  document.getElementById('newAuditModal').style.display = 'flex';
  document.getElementById('auditName').focus();
}

function closeModal(id) {
  document.getElementById(id).style.display = 'none';
}

document.getElementById('newAuditForm').addEventListener('submit', async (e) => {
  e.preventDefault();
  const name   = document.getElementById('auditName').value.trim();
  const client = document.getElementById('auditClient').value.trim();
  if (!name) return;

  const fd = new FormData();
  fd.append('name', name);
  fd.append('client', client);

  const resp = await fetch('/audits', { method: 'POST', body: fd });
  if (resp.ok) {
    const data = await resp.json();
    window.location.href = `/audits/${data.id}`;
  } else {
    alert('Failed to create audit.');
  }
});

async function closeAudit(id, name) {
  if (!confirm(`Close audit "${name}"? No uploads or agent runs will be allowed while closed.`)) return;
  await fetch(`/audits/${id}/close`, { method: 'POST' });
  location.reload();
}

async function reopenAudit(id) {
  await fetch(`/audits/${id}/reopen`, { method: 'POST' });
  location.reload();
}

function openDeleteModal(id, name) {
  _deleteAuditId = id;
  document.getElementById('deleteAuditName').textContent = name;
  document.getElementById('deleteConfirmInput').value = '';
  document.getElementById('deleteModal').style.display = 'flex';
  document.getElementById('deleteConfirmInput').focus();
}

async function confirmDelete() {
  const typed = document.getElementById('deleteConfirmInput').value.trim();
  const expected = document.getElementById('deleteAuditName').textContent;
  if (typed !== expected) {
    alert('Name does not match. Please type the exact audit name.');
    return;
  }
  const fd = new FormData();
  fd.append('confirm_name', typed);
  const resp = await fetch(`/audits/${_deleteAuditId}`, { method: 'DELETE', body: fd });
  if (resp.ok) {
    location.reload();
  } else {
    alert('Delete failed.');
  }
}

// Close modals on overlay click
document.querySelectorAll('.modal-overlay').forEach(el => {
  el.addEventListener('click', (e) => {
    if (e.target === el) el.style.display = 'none';
  });
});
