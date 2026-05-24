// ─── State ────────────────────────────────────────────────────────────────
let currentVault = null;
let allPages = [];

// ─── Boot ─────────────────────────────────────────────────────────────────
async function boot() {
  await loadVaults();
  setupTabs();
  setupCategoryFilter();
  setupSearch();
  setupIngest();
  setupQuery();
  setupLog();
}

// ─── Vaults ───────────────────────────────────────────────────────────────
async function loadVaults() {
  const data = await api('/api/vaults');
  const sel = document.getElementById('vault-select');
  sel.innerHTML = data.vaults.map(v =>
    `<option value="${v}" ${v === data.default ? 'selected' : ''}>${v}</option>`
  ).join('');
  currentVault = sel.value || data.vaults[0];
  sel.addEventListener('change', () => {
    currentVault = sel.value;
    refreshAll();
  });
  if (currentVault) await refreshAll();
}

async function refreshAll() {
  await Promise.all([loadPages(), loadStats()]);
  window.dispatchEvent(new CustomEvent('vault-changed', { detail: currentVault }));
}

async function loadStats() {
  if (!currentVault) return;
  const s = await api(`/api/vaults/${currentVault}/status`);
  document.getElementById('sidebar-stats').innerHTML = `
    <strong>${s.name}</strong><br>
    Pages: ${s.total_pages}<br>
    Queued: ${s.raw_queued}<br>
    Model: <span style="color:var(--accent)">${s.model}</span>
  `;
}

// ─── Tabs ─────────────────────────────────────────────────────────────────
function setupTabs() {
  document.querySelectorAll('.nav-btn').forEach(btn => {
    btn.addEventListener('click', () => {
      document.querySelectorAll('.nav-btn').forEach(b => b.classList.remove('active'));
      document.querySelectorAll('.tab').forEach(t => { t.classList.add('hidden'); t.classList.remove('active'); });
      btn.classList.add('active');
      const tab = document.getElementById(`tab-${btn.dataset.tab}`);
      tab.classList.remove('hidden');
      tab.classList.add('active');
      if (btn.dataset.tab === 'graph') window.dispatchEvent(new Event('graph-activate'));
      if (btn.dataset.tab === 'log') loadLog();
    });
  });
}

// ─── Explorer ─────────────────────────────────────────────────────────────
async function loadPages(category = '') {
  const url = `/api/vaults/${currentVault}/pages` + (category ? `?category=${category}` : '');
  const data = await api(url);
  allPages = data.pages;
  renderPageList(allPages);
}

function renderPageList(pages) {
  const ul = document.getElementById('page-list');
  ul.innerHTML = pages.map(p => `
    <li data-path="${p.file_path}" title="${p.file_path}">
      ${p.title}<span class="page-cat">${p.category}</span>
    </li>
  `).join('');
  ul.querySelectorAll('li').forEach(li => {
    li.addEventListener('click', () => {
      ul.querySelectorAll('li').forEach(x => x.classList.remove('selected'));
      li.classList.add('selected');
      loadPageContent(li.dataset.path);
    });
  });
}

async function loadPageContent(filePath) {
  const data = await api(`/api/vaults/${currentVault}/pages/content?file_path=${encodeURIComponent(filePath)}`);
  const page = allPages.find(p => p.file_path === filePath) || {};

  document.getElementById('page-viewer-placeholder').classList.add('hidden');
  const pc = document.getElementById('page-content');
  pc.classList.remove('hidden');
  document.getElementById('page-title').textContent = page.title || filePath.split('/').pop().replace('.md','');
  document.getElementById('page-category-badge').textContent = page.category || '';
  document.getElementById('page-body').innerHTML = markdownToHtml(data.content);

  const backlinks = page.backlinks || [];
  document.getElementById('page-backlinks').innerHTML = backlinks.length
    ? `<strong>Backlinks (${backlinks.length}):</strong> ` + backlinks.map(b =>
        `<a href="#" onclick="openPage('${b}');return false">${b.split('/').pop().replace('.md','')}</a>`
      ).join(', ')
    : '';
}

window.openPage = async function(path) {
  await loadPageContent(path);
  document.querySelectorAll('#page-list li').forEach(li => {
    li.classList.toggle('selected', li.dataset.path === path);
  });
};

function setupCategoryFilter() {
  document.querySelectorAll('.category-btn').forEach(btn => {
    btn.addEventListener('click', () => {
      document.querySelectorAll('.category-btn').forEach(b => b.classList.remove('active'));
      btn.classList.add('active');
      loadPages(btn.dataset.cat);
    });
  });
}

// ─── Search ───────────────────────────────────────────────────────────────
function setupSearch() {
  const input = document.getElementById('search-input');
  const btn   = document.getElementById('search-btn');
  const res   = document.getElementById('search-results');

  const doSearch = async () => {
    const q = input.value.trim();
    if (!q) return;
    const data = await api(`/api/vaults/${currentVault}/search?q=${encodeURIComponent(q)}`);
    res.innerHTML = data.results.length
      ? data.results.map(r => `
          <div class="search-hit" onclick="openExplorerPage('${r.file_path}')">
            <div class="search-hit-title">${r.title}</div>
            <div class="search-hit-cat">${r.category}</div>
            <div class="search-hit-summary">${r.summary || ''}</div>
          </div>
        `).join('')
      : '<p style="color:var(--text-dim)">No results found.</p>';
  };
  btn.addEventListener('click', doSearch);
  input.addEventListener('keydown', e => e.key === 'Enter' && doSearch());
}

window.openExplorerPage = function(path) {
  document.querySelector('[data-tab="explorer"]').click();
  setTimeout(() => loadPageContent(path), 50);
};

// ─── Ingest ───────────────────────────────────────────────────────────────
function setupIngest() {
  document.getElementById('ingest-btn').addEventListener('click', async () => {
    const source = document.getElementById('ingest-source').value.trim();
    const dryRun = document.getElementById('ingest-dry-run').checked;
    if (!source) return;

    const btn = document.getElementById('ingest-btn');
    const result = document.getElementById('ingest-result');
    btn.disabled = true;
    btn.textContent = 'Ingesting…';
    result.classList.add('hidden');
    result.classList.remove('error');

    try {
      const data = await api(`/api/vaults/${currentVault}/ingest`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ source, dry_run: dryRun }),
      });
      const written = data.pages_written || [];
      result.textContent = dryRun
        ? `[dry-run] Would create:\n${data.source_page?.file_path || '?'}\n` +
          (data.page_updates || []).map(u => `  ${u.action}: ${u.file_path}`).join('\n')
        : `✓ Wrote ${written.length} page(s):\n` + written.join('\n');
      if (!dryRun) { await refreshAll(); }
    } catch (e) {
      result.classList.add('error');
      result.textContent = e.message;
    } finally {
      result.classList.remove('hidden');
      btn.disabled = false;
      btn.textContent = 'Ingest';
    }
  });
}

// ─── Query ────────────────────────────────────────────────────────────────
function setupQuery() {
  document.getElementById('query-btn').addEventListener('click', async () => {
    const question = document.getElementById('query-input').value.trim();
    const saveAs   = document.getElementById('query-save-as').value.trim() || null;
    if (!question) return;

    const btn = document.getElementById('query-btn');
    const result = document.getElementById('query-result');
    btn.disabled = true;
    btn.textContent = 'Thinking…';
    result.classList.add('hidden');
    result.classList.remove('error');

    try {
      const data = await api(`/api/vaults/${currentVault}/query`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ question, save_as: saveAs }),
      });
      let text = data.answer;
      if (data.sources?.length) text += `\n\nSources: ${data.sources.join(', ')}`;
      if (data.saved_to) text += `\n\n✓ Saved to: ${data.saved_to}`;
      result.textContent = text;
      if (saveAs) await refreshAll();
    } catch (e) {
      result.classList.add('error');
      result.textContent = e.message;
    } finally {
      result.classList.remove('hidden');
      btn.disabled = false;
      btn.textContent = 'Ask';
    }
  });
}

// ─── Log ──────────────────────────────────────────────────────────────────
async function loadLog() {
  const data = await api(`/api/vaults/${currentVault}/log`);
  document.getElementById('log-content').textContent = data.content || '(empty)';
}

function setupLog() {
  document.getElementById('lint-btn').addEventListener('click', async () => {
    const btn = document.getElementById('lint-btn');
    const result = document.getElementById('lint-result');
    btn.disabled = true;
    btn.textContent = 'Linting…';
    result.classList.add('hidden');

    try {
      const data = await api(`/api/vaults/${currentVault}/lint`, { method: 'POST' });
      const s = data.structural;
      result.textContent =
        `Orphans: ${s.orphans.length}\nBroken links: ${Object.keys(s.broken_links).length}\n\n` +
        `Report: ${data.saved_to}\n\n--- LLM Report ---\n${data.llm_report}`;
      await loadLog();
    } catch (e) {
      result.classList.add('error');
      result.textContent = e.message;
    } finally {
      result.classList.remove('hidden');
      btn.disabled = false;
      btn.textContent = 'Run Lint';
    }
  });
}

// ─── Helpers ──────────────────────────────────────────────────────────────
async function api(url, opts = {}) {
  const res = await fetch(url, opts);
  if (!res.ok) {
    const err = await res.json().catch(() => ({ detail: res.statusText }));
    throw new Error(err.detail || res.statusText);
  }
  return res.json();
}

function markdownToHtml(md) {
  // Minimal markdown renderer (headings, bold, italic, code, links, wikilinks, paragraphs)
  let html = md
    .replace(/^#{6}\s(.+)$/gm, '<h6>$1</h6>')
    .replace(/^#{5}\s(.+)$/gm, '<h5>$1</h5>')
    .replace(/^#{4}\s(.+)$/gm, '<h4>$1</h4>')
    .replace(/^#{3}\s(.+)$/gm, '<h3>$1</h3>')
    .replace(/^#{2}\s(.+)$/gm, '<h2>$1</h2>')
    .replace(/^#{1}\s(.+)$/gm, '<h1>$1</h1>')
    .replace(/```[\w]*\n([\s\S]*?)```/g, '<pre><code>$1</code></pre>')
    .replace(/`([^`]+)`/g, '<code>$1</code>')
    .replace(/\*\*(.+?)\*\*/g, '<strong>$1</strong>')
    .replace(/\*(.+?)\*/g, '<em>$1</em>')
    .replace(/\[\[([^\]|]+?)(?:\|([^\]]+))?\]\]/g, (_, target, label) =>
      `<a href="#" onclick="openPage('${target.trim()}.md');return false">${label || target}</a>`)
    .replace(/\[([^\]]+)\]\(([^)]+)\)/g, '<a href="$2" target="_blank">$1</a>')
    .replace(/^---\s*$/gm, '<hr>')
    .replace(/^- (.+)$/gm, '<li>$1</li>')
    .replace(/(<li>[\s\S]+?<\/li>)/g, '<ul>$1</ul>');

  // Wrap bare paragraphs (lines not already wrapped in tags)
  html = html.split('\n').map(line => {
    if (!line.trim()) return '';
    if (/^<(h[1-6]|pre|ul|li|hr)/.test(line.trim())) return line;
    return `<p>${line}</p>`;
  }).join('\n');

  return html;
}

boot();
