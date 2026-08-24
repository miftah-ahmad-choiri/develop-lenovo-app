/* monday_data.js — all interactive logic for the Monday Data page.
   Loaded as a static asset so the browser caches it across page loads.
   Data is fetched from /admin/api/monday-data (paginated) on demand.
   Filtering, search, and pagination are handled server-side. */
(function () {
  /* ── State ───────────────────────────────────────────────────── */
  let BOARD_NAMES   = {};
  let _activeBoard  = '';
  let _activeStatus = '';
  let _searchQ      = '';
  let _page         = 1;
  let _totalPages   = 1;
  let _totalRows    = 0;
  const PAGE_SIZE   = 50;

  /* ── Per-page row cache (keyed by monday_item_id) for detail drawer ── */
  let _ROW_MAP = {};

  /* ── Search debounce timer ───────────────────────────────────── */
  let _searchTimer = null;

  /* ── Status grouping — must match backend STATUS_GROUPS ─────── */
  const STATUS_GROUPS = {
    'in progress': ['technical escalation', 'progress', 'qa result'],
  };
  const _RAW_TO_GROUP = {};
  Object.entries(STATUS_GROUPS).forEach(([group, raws]) => {
    raws.forEach(raw => { _RAW_TO_GROUP[raw] = group; });
  });

  /* ── Status filter dropdown ──────────────────────────────────── */
  window.selectStatus = function(status) {
    _activeStatus = status;
    _page = 1;
    const sel = document.getElementById('status-select');
    const btn = document.getElementById('status-clear-btn');
    if (sel) { sel.value = status; sel.classList.toggle('has-filter', !!status); }
    if (btn) btn.classList.toggle('visible', !!status);
    loadData(false);
  };

  /* Populate <select> from meta endpoint counts.
     "In Progress" is always pinned as the first option after "All". */
  const PINNED_STATUS = 'in progress';

  function _buildStatusPills(statusList, totalCount) {
    const rawCounts = {};
    (statusList || []).forEach(s => {
      rawCounts[(s.status || '—').trim()] = s.cnt;
    });

    const displayCounts = {};
    Object.entries(rawCounts).forEach(([raw, n]) => {
      const group = _RAW_TO_GROUP[raw.toLowerCase()];
      const key   = group || raw;
      displayCounts[key] = (displayCounts[key] || 0) + n;
    });

    const sorted = Object.entries(displayCounts).sort((a, b) => {
      if (a[0].toLowerCase() === PINNED_STATUS) return -1;
      if (b[0].toLowerCase() === PINNED_STATUS) return  1;
      return b[1] - a[1];
    });

    const sel = document.getElementById('status-select');
    if (!sel) return;
    sel.innerHTML = `<option value="">All statuses (${totalCount})</option>`;
    sorted.forEach(([label, count]) => {
      const opt     = document.createElement('option');
      opt.value     = label.toLowerCase();
      const display = label.replace(/\b\w/g, c => c.toUpperCase());
      opt.textContent = `${display}  (${count})`;
      sel.appendChild(opt);
    });
  }

  /* ── Render table from server response ──────────────────────── */
  function render(data) {
    const rows  = data.rows   || [];
    const total = data.total  || 0;
    const pages = data.pages  || 1;
    _totalRows  = total;
    _totalPages = pages;

    // update row cache for detail drawer
    _ROW_MAP = {};
    rows.forEach(r => { _ROW_MAP[r.monday_item_id] = r; });

    document.getElementById('row-count-label').textContent =
      total.toLocaleString() + ' item' + (total !== 1 ? 's' : '');

    const start = (_page - 1) * PAGE_SIZE;
    const tbody = document.getElementById('data-tbody');
    if (!rows.length) {
      tbody.innerHTML = '<tr><td colspan="8" style="text-align:center;color:#9ca3af;padding:32px">No records found.</td></tr>';
    } else {
      tbody.innerHTML = rows.map((r, i) => {
        const rowNum      = start + i + 1;
        const statusBadge = _statusBadge(r.status);
        const date        = _fmtDate(r.item_created_at || r.db_synced_at);
        const discClass   = r.disc_count > 0 ? 'btn-disc has-disc' : 'btn-disc';
        const discBadge   = r.disc_count > 0 ? `<span class="disc-count">${r.disc_count}</span>` : '';
        const discSvg     = `<svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M21 15a2 2 0 0 1-2 2H7l-4 4V5a2 2 0 0 1 2-2h14a2 2 0 0 1 2 2z"/></svg>`;
        return `<tr>
          <td class="mono" style="color:#9ca3af">${rowNum}</td>
          <td class="td-task">
            <span class="task-name-link js-detail-btn" data-item-id="${_esc(r.monday_item_id)}" title="${_esc(r.item_name)}">${_esc(r.item_name)}</span>
            <span class="task-sub">${_esc(r.asp_board || '')}</span>
          </td>
          <td>${statusBadge}</td>
          <td style="font-size:12px;color:#57606a;white-space:nowrap">${date}</td>
          <td>${r.serial_number ? `<span class="cell-link js-history-btn" data-serial="${_esc(r.serial_number)}">${_esc(_extractWO(r.wo_case_id) || r.wo_case_id || '—')}</span>` : `<span class="mono" style="color:#9ca3af">—</span>`}</td>
          <td>${r.serial_number ? `<span class="cell-link js-history-btn" data-serial="${_esc(r.serial_number)}">${_esc(r.serial_number)}</span>` : `<span class="mono" style="color:#9ca3af">—</span>`}</td>
          <td style="font-size:12px">${_esc(r.work_order_type || '—')}</td>
          <td><button class="${discClass} js-disc-btn" data-item-id="${_esc(r.monday_item_id)}" data-item-name="${_esc(r.item_name)}">${discSvg}${discBadge}</button></td>
        </tr>`;
      }).join('');
    }
    _renderPagination(total, pages);
  }

  /* ── Helpers ─────────────────────────────────────────────────── */
  function _esc(s) {
    return String(s || '').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;');
  }

  function _extractWO(raw) {
    if (!raw) return '';
    const str   = raw.toString().trim();
    const parts = str.split(/[\/,;\|\s]+/).map(p => p.trim()).filter(Boolean);
    const wo    = parts.find(p => /^40\d{8,}/.test(p));
    if (wo) return wo;
    const head = str.slice(0, 10);
    const tail = str.slice(-10);
    if (/^40\d/.test(head)) return head;
    if (/^40\d/.test(tail)) return tail;
    return str;
  }

  function _statusBadge(s) {
    const v = (s || '').toLowerCase();
    let cls = 'badge-default';
    if (v === 'complete' || v === 'completed')                              cls = 'badge-complete';
    else if (v.includes('approved'))                                        cls = 'badge-approved';
    else if (v === 'reject' || v === 'rejected')                            cls = 'badge-reject';
    else if (v === 'technical escalation')                                  cls = 'badge-tech-esc';
    else if (v.includes('progress') || v.includes('running') || v.includes('pending') || v === 'qa result') cls = 'badge-inprogress';
    return `<span class="badge ${cls}">${_esc(s || '—')}</span>`;
  }

  function _fmtDate(iso) {
    if (!iso) return '—';
    try {
      const d = new Date(iso);
      if (isNaN(d)) return iso.slice(0, 10) || '—';
      return d.toLocaleDateString('en-GB', { day: 'numeric', month: 'short', year: 'numeric' });
    } catch(e) { return iso.slice(0, 10) || '—'; }
  }

  /* ── Pagination ──────────────────────────────────────────────── */
  function _renderPagination(total, pages) {
    const bar = document.getElementById('pagination-bar');
    if (pages <= 1) { bar.innerHTML = ''; return; }
    const from = ((_page - 1) * PAGE_SIZE) + 1;
    const to   = Math.min(_page * PAGE_SIZE, total);
    let html = `<span class="pg-info">${from}–${to} of ${total.toLocaleString()}</span>`;
    html += `<button class="pg-btn" onclick="goPage(1)" ${_page===1?'disabled':''}>«</button>`;
    html += `<button class="pg-btn" onclick="goPage(${_page-1})" ${_page===1?'disabled':''}>‹</button>`;
    const W = 2;
    for (let p = Math.max(1, _page - W); p <= Math.min(pages, _page + W); p++) {
      html += `<button class="pg-btn${p===_page?' active':''}" onclick="goPage(${p})">${p}</button>`;
    }
    html += `<button class="pg-btn" onclick="goPage(${_page+1})" ${_page===pages?'disabled':''}>›</button>`;
    html += `<button class="pg-btn" onclick="goPage(${pages})" ${_page===pages?'disabled':''}>»</button>`;
    bar.innerHTML = html;
  }

  window.goPage = function(p) {
    _page = p;
    loadData(false);
    document.querySelector('.md-main').scrollIntoView({ behavior: 'smooth', block: 'start' });
  };

  /* ── Board filter ────────────────────────────────────────────── */
  window.selectBoard = function(boardId) {
    _activeBoard = boardId;
    _page = 1;
    document.querySelectorAll('.md-filter-item').forEach(el => {
      el.classList.toggle('active', el.dataset.board === boardId);
    });
    const lbl = document.getElementById('active-board-label');
    const sub = document.getElementById('active-board-subtitle');
    if (boardId) {
      const name = BOARD_NAMES[boardId] || boardId;
      lbl.textContent = '— ' + name;
      sub.textContent = 'Filtered by: ' + name;
    } else {
      lbl.textContent = '';
      sub.textContent = 'Showing all boards';
    }
    loadData(false);
  };

  window.filterBoardList = function(q) {
    const lower = q.toLowerCase();
    document.querySelectorAll('#board-filter-list .md-filter-item').forEach(el => {
      if (el.classList.contains('md-filter-all')) return;
      el.style.display = (el.dataset.name || '').includes(lower) ? '' : 'none';
    });
  };

  window.applySearch = function(q) {
    clearTimeout(_searchTimer);
    _searchTimer = setTimeout(function() {
      _searchQ = q.trim();
      _page = 1;
      loadData(false);
    }, 300);
  };

  /* ── Discussion drawer ───────────────────────────────────────── */
  function _openDrawer(itemId, itemName) {
    document.getElementById('drawer-title').textContent    = 'Discussion';
    document.getElementById('drawer-subtitle').textContent = itemName;
    document.getElementById('drawer-body').innerHTML = '<div class="disc-loading">Loading discussions…</div>';
    document.getElementById('disc-drawer').classList.add('open');
    document.getElementById('disc-overlay').classList.add('open');
    document.body.style.overflow = 'hidden';
    fetch('/admin/monday-data/discussion/' + encodeURIComponent(itemId))
      .then(r => r.json())
      .then(data => _renderDrawer(data))
      .catch(() => {
        document.getElementById('drawer-body').innerHTML =
          '<div class="disc-empty">Failed to load discussions.</div>';
      });
  }

  window.closeDrawer = function() {
    document.getElementById('disc-drawer').classList.remove('open');
    document.getElementById('disc-overlay').classList.remove('open');
    document.body.style.overflow = '';
  };

  document.getElementById('data-tbody').addEventListener('click', function(e) {
    const btn = e.target.closest('.js-disc-btn');
    if (btn) _openDrawer(btn.dataset.itemId, btn.dataset.itemName);
  });

  document.getElementById('data-tbody').addEventListener('click', function(e) {
    const el = e.target.closest('.js-detail-btn');
    if (el) _openDetail(el.dataset.itemId);
  });

  /* ── Detail modal ────────────────────────────────────────────── */
  function _openDetail(itemId) {
    // If the row is cached from the current page, render immediately.
    // Otherwise fetch it from the per-item API endpoint.
    const cached = _ROW_MAP[itemId];
    if (cached) {
      document.getElementById('detail-title').textContent    = cached.item_name || 'Escalation Detail';
      document.getElementById('detail-subtitle').textContent = cached.asp_board || '';
      document.getElementById('detail-body').innerHTML = _renderDetail(cached);
      document.getElementById('detail-drawer').classList.add('open');
      document.getElementById('detail-overlay').classList.add('open');
    } else {
      // Show drawer immediately with a loading state while we fetch
      document.getElementById('detail-title').textContent    = 'Escalation Detail';
      document.getElementById('detail-subtitle').textContent = '';
      document.getElementById('detail-body').innerHTML = '<div class="disc-loading">Loading…</div>';
      document.getElementById('detail-drawer').classList.add('open');
      document.getElementById('detail-overlay').classList.add('open');
      fetch('/admin/api/monday-data/item/' + encodeURIComponent(itemId))
        .then(r => r.json())
        .then(row => {
          if (row.error) {
            document.getElementById('detail-body').innerHTML = '<div class="disc-empty">Item not found.</div>';
            return;
          }
          _ROW_MAP[itemId] = row;
          document.getElementById('detail-title').textContent    = row.item_name || 'Escalation Detail';
          document.getElementById('detail-subtitle').textContent = row.asp_board || '';
          document.getElementById('detail-body').innerHTML = _renderDetail(row);
        })
        .catch(() => {
          document.getElementById('detail-body').innerHTML = '<div class="disc-empty">Failed to load detail.</div>';
        });
    }
  }

  window.closeDetail = function() {
    document.getElementById('detail-drawer').classList.remove('open');
    document.getElementById('detail-overlay').classList.remove('open');
  };

  function _renderDetail(r) {
    function clean(v) { return (v || '').toString().replace(/[\uFEFF\u200B\u200C\u200D\u00A0]/g, '').trim(); }
    function tile(label, val, cls) {
      const v = clean(val);
      return `<div class="detail-tile"><div class="dt-label">${label}</div><div class="dt-value ${cls || ''}">${v ? _esc(v) : '<span style="color:#9ca3af;font-weight:400;font-style:italic">—</span>'}</div></div>`;
    }
    function field(label, val, cls) {
      const v = clean(val);
      return `<div class="detail-field"><div class="df-label">${label}</div><div class="df-value ${cls || ''}">${v ? _esc(v) : '<span class="muted">—</span>'}</div></div>`;
    }
    function fieldFull(label, val) {
      const v = clean(val);
      return `<div class="detail-field full"><div class="df-label">${label}</div><div class="df-value ${v ? '' : 'muted'}">${v ? _esc(v) : '—'}</div></div>`;
    }
    const svgId    = `<svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><path d="M20.59 13.41l-7.17 7.17a2 2 0 0 1-2.83 0L2 12V2h10l8.59 8.59a2 2 0 0 1 0 2.82z"/><line x1="7" y1="7" x2="7.01" y2="7"/></svg>`;
    const svgDiag  = `<svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><path d="M2 3h6a4 4 0 0 1 4 4v14a3 3 0 0 0-3-3H2z"/><path d="M22 3h-6a4 4 0 0 0-4 4v14a3 3 0 0 1 3-3h7z"/></svg>`;
    const svgParts = `<svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><path d="M21 16V8a2 2 0 0 0-1-1.73l-7-4a2 2 0 0 0-2 0l-7 4A2 2 0 0 0 3 8v8a2 2 0 0 0 1 1.73l7 4a2 2 0 0 0 2 0l7-4A2 2 0 0 0 21 16z"/></svg>`;
    const svgRepair= `<svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><path d="M14.7 6.3a1 1 0 0 0 0 1.4l1.6 1.6a1 1 0 0 0 1.4 0l3.77-3.77a6 6 0 0 1-7.94 7.94l-6.91 6.91a2.12 2.12 0 0 1-3-3l6.91-6.91a6 6 0 0 1 7.94-7.94l-3.76 3.76z"/></svg>`;
    const svgClock = `<svg width="11" height="11" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="10"/><polyline points="12 6 12 12 16 14"/></svg>`;
    const svgUser  = `<svg width="11" height="11" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><path d="M20 21v-2a4 4 0 0 0-4-4H8a4 4 0 0 0-4 4v2"/><circle cx="12" cy="7" r="4"/></svg>`;
    const bannerBg = (() => {
      const s = (r.status || '').toLowerCase();
      if (s.includes('complete') || s.includes('done'))         return 'background:#f0fdf4;border-color:#bbf7d0';
      if (s.includes('progress') || s.includes('working'))      return 'background:#eff6ff;border-color:#bfdbfe';
      if (s.includes('escalat') || s.includes('tech'))          return 'background:#f5f3ff;border-color:#ddd6fe';
      if (s.includes('reject') || s.includes('cancel'))         return 'background:#fef2f2;border-color:#fecaca';
      return 'background:#f7f8fa;border-color:#e5e7eb';
    })();
    let html = `<div class="detail-banner" style="${bannerBg}">
      <div class="detail-banner-badge">${_statusBadge(r.status)}</div>
      <div class="detail-banner-meta">
        ${r.work_order_type  ? `<span>${svgId}   <strong>${_esc(r.work_order_type)}</strong></span>` : ''}
        ${r.asp_board        ? `<span>${svgUser} <strong>${_esc(r.asp_board)}</strong></span>` : ''}
        ${r.item_created_at  ? `<span>${svgClock} Created <strong>${_fmtDate(r.item_created_at)}</strong></span>` : ''}
        ${r.item_updated_at  ? `<span>${svgClock} Updated <strong>${_fmtDate(r.item_updated_at)}</strong></span>` : ''}
      </div>
    </div>`;
    html += `<div class="detail-tiles">
      ${tile('WO / Case ID',  _extractWO(r.wo_case_id) || r.wo_case_id, 'mono')}
      ${tile('Serial Number', r.serial_number, 'mono blue')}
      ${tile('Creator',       r.creator_name)}
      ${tile('Location',      r.location)}
      ${tile('PPSN Category', r.ppsn_category)}
      ${tile('RRR Category',  r.rrr_category)}
    </div>`;
    html += `<div class="detail-section">
      <div class="detail-section-head">${svgDiag} Diagnosis</div>
      <div class="detail-fields">
        ${field('Date / Time',  r.diag_datetime)}
        ${field('Agent / CE',   r.diag_agent_ce)}
        ${field('Model',        r.diag_model)}
        ${field('ESC Approval', r.diag_esc_approval)}
        ${fieldFull('Warranty Status',     r.diag_warranty)}
        ${fieldFull('Problem Description', r.diag_problem)}
      </div>
    </div>`;
    if (clean(r.diagnose_note)) {
      html += `<div class="detail-note-block"><div class="detail-note-head">${svgDiag} Diagnose Note</div><div class="detail-note-body">${_linkify(clean(r.diagnose_note))}</div></div>`;
    }
    if (clean(r.diag_parts_request)) {
      html += `<div class="detail-note-block"><div class="detail-note-head">${svgParts} Parts Request</div><div class="detail-note-body">${_linkify(clean(r.diag_parts_request))}</div></div>`;
    }
    if (clean(r.repair_note)) {
      html += `<div class="detail-note-block"><div class="detail-note-head">${svgRepair} Repair Note</div><div class="detail-note-body">${_linkify(clean(r.repair_note))}</div></div>`;
    }
    return html;
  }

  /* ── Avatar / discussion helpers ─────────────────────────────── */
  function _initials(name) {
    if (!name) return '?';
    const parts = name.trim().split(/\s+/);
    return parts.length >= 2 ? (parts[0][0] + parts[parts.length-1][0]).toUpperCase() : name.slice(0,2).toUpperCase();
  }
  function _avatarColor(name) {
    const colors = ['#2563eb','#16a34a','#d97706','#7c3aed','#dc2626','#0891b2','#65a30d','#db2777','#ea580c','#0d9488','#7c5cd8','#b45309','#15803d','#1d4ed8','#9333ea','#c2410c','#0369a1','#4f46e5','#be185d','#047857'];
    let h = 5381;
    const s = (name || '').toLowerCase();
    for (let i = 0; i < s.length; i++) h = ((h << 5) + h) ^ s.charCodeAt(i);
    return colors[(h >>> 0) % colors.length];
  }
  function _fmtTs(iso) {
    if (!iso) return '';
    try {
      const d = new Date(iso);
      if (isNaN(d)) return iso.slice(0,16).replace('T',' ');
      return d.toLocaleString('en-GB', { day:'numeric', month:'short', year:'numeric', hour:'2-digit', minute:'2-digit' });
    } catch(e) { return iso.slice(0,16).replace('T',' '); }
  }
  function _cleanBody(txt) {
    return (txt || '').replace(/[\uFEFF\u200B\u200C\u200D\u00A0]/g, '').trim();
  }
  function _linkify(txt) {
    return _esc(txt).replace(/(https?:\/\/[^\s<>"']+)/g, '<a href="$1" target="_blank" rel="noopener noreferrer" class="disc-link">$1</a>');
  }

  function _renderDrawer(data) {
    const body = document.getElementById('drawer-body');
    if (!data || !data.updates || data.updates.length === 0) {
      body.innerHTML = '<div class="disc-empty">No discussion threads found for this item.</div>';
      return;
    }
    let html = '';
    for (const upd of data.updates) {
      const bodyText   = _cleanBody(upd.body_text);
      const authorName = upd.creator_name || upd.creator_id || 'Unknown';
      const initials   = _initials(authorName);
      const avatarBg   = _avatarColor(authorName);
      html += `<div class="disc-update">
        <div class="disc-update-head">
          <div class="disc-avatar" style="background:${avatarBg}">${initials}</div>
          <div class="disc-meta">
            <div class="disc-author">${_esc(authorName)}</div>
            <div class="disc-time">${_fmtTs(upd.created_at)}</div>
          </div>
        </div>
        <div class="disc-update-body">${bodyText ? _linkify(bodyText) : '<em style="color:#9ca3af">— empty —</em>'}</div>`;
      if (upd.replies && upd.replies.length > 0) {
        html += '<div class="disc-replies">';
        for (const rpl of upd.replies) {
          const rBody  = _cleanBody(rpl.body_text);
          const rName  = rpl.creator_name || rpl.creator_id || 'Unknown';
          html += `<div class="disc-reply">
            <div class="disc-reply-avatar" style="background:${_avatarColor(rName)}">${_initials(rName)}</div>
            <div class="disc-reply-content">
              <span class="disc-reply-author">${_esc(rName)}</span>
              <span class="disc-reply-time">${_fmtTs(rpl.created_at)}</span>
              <div class="disc-reply-body">${rBody ? _linkify(rBody) : '<em style="color:#9ca3af">— empty —</em>'}</div>
            </div>
          </div>`;
        }
        html += '</div>';
      }
      html += '</div>';
    }
    body.innerHTML = html;
  }

  /* ── Serial History modal ────────────────────────────────────── */
  document.getElementById('data-tbody').addEventListener('click', function(e) {
    const el = e.target.closest('.js-history-btn');
    if (el) _openHistory(el.dataset.serial);
  });

  function _openHistory(serial) {
    if (!serial) return;
    document.getElementById('hist-serial').textContent = 'All Problem History (SN: ' + serial + ')';
    document.getElementById('hist-body').innerHTML = '<div class="hist-loading">Loading…</div>';
    document.getElementById('hist-overlay').classList.add('open');
    document.body.style.overflow = 'hidden';
    fetch('/admin/api/sn-history/' + encodeURIComponent(serial))
      .then(r => r.json())
      .then(data => { document.getElementById('hist-body').innerHTML = _renderHistory(data.serial_number, data.rows); })
      .catch(() => { document.getElementById('hist-body').innerHTML = '<div class="hist-empty">Failed to load history. Please try again.</div>'; });
  }

  window.closeHistory = function() {
    document.getElementById('hist-overlay').classList.remove('open');
    document.body.style.overflow = '';
  };

  function _renderHistory(sn, rows) {
    const snLabel = `<div class="hist-sn-label">Laptop Device SN: <strong>${_esc(sn || '—')}</strong></div>`;
    if (!rows || !rows.length) return snLabel + `<div class="hist-empty">No work orders found for this serial number in the WO database.</div>`;
    const woCount = `<div class="hist-wo-count">${rows.length} WO${rows.length !== 1 ? 's' : ''} found for this serial number</div>`;
    const groups = {}, order = [];
    rows.forEach(r => {
      const key = r.case_number ? String(r.case_number) : '__none__';
      if (!groups[key]) { groups[key] = []; order.push(key); }
      groups[key].push(r);
    });
    const thS = 'padding:8px 10px;text-align:left;font-size:10px;font-weight:700;color:#57606a;text-transform:uppercase;letter-spacing:.04em;border-bottom:1px solid #e5e7eb;white-space:nowrap';
    let globalIdx = 0, tbody = '';
    order.forEach((key, gIdx) => {
      const wos = groups[key]; const ticket = key === '__none__' ? '—' : key;
      wos.forEach((r, i) => {
        globalIdx++;
        const groupTop   = i === 0 && gIdx > 0 ? 'hist-group-top' : '';
        const ticketCell = i === 0 ? `<td class="hist-ticket-cell" rowspan="${wos.length}" style="padding:9px 10px;vertical-align:top${i===0&&gIdx>0?';border-top:2px solid #e5e7eb':''}">${_esc(ticket)}</td>` : '';
        const woCell     = `<button class="hist-wo-btn" onclick="openWoDetail(${_esc(r.work_order_id)})">${_esc(r.work_order_id)}</button>`;
        tbody += `<tr class="${groupTop}">
          <td style="text-align:center;color:#8b95a1;font-size:12px;width:32px">${globalIdx}</td>
          ${ticketCell}
          <td>${woCell}</td>
          <td>${_statusBadge(r.work_order_status)}</td>
          <td style="font-size:12px">${_esc(r.work_order_type || '—')}</td>
          <td style="font-size:12px;white-space:nowrap">${_esc(r.created_on ? r.created_on.slice(0,10) : '—')}</td>
          <td style="font-size:12px;white-space:nowrap">${_esc(r.completion_date ? r.completion_date.slice(0,10) : '—')}</td>
          <td style="font-size:12px;max-width:220px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap" title="${_esc(r.case_desc||'')}">${_esc(r.case_desc||'—')}</td>
        </tr>`;
      });
    });
    const table = `<div class="hist-table-wrap"><table class="hist-table">
      <thead><tr>
        <th style="${thS};text-align:center;width:32px">#</th>
        <th style="${thS}">Ticket (Case #)</th>
        <th style="${thS}">WO Number</th>
        <th style="${thS}">WO Status</th>
        <th style="${thS}">Type</th>
        <th style="${thS}">Created</th>
        <th style="${thS}">Completed</th>
        <th style="${thS};white-space:normal">Case Description</th>
      </tr></thead>
      <tbody>${tbody}</tbody>
    </table></div>`;
    return snLabel + woCount + table;
  }

  /* ── WO Detail sub-modal ─────────────────────────────────────── */
  window.openWoDetail = function(woId) {
    const body = document.getElementById('wo-det-body');
    body.innerHTML = '<div class="modal-loading">Loading detail…</div>';
    document.getElementById('wo-det-overlay').classList.add('open');
    Promise.all([
      fetch(`/admin/api/wo-detail/${woId}`).then(r => r.json()),
      fetch(`/admin/api/wo-parts/${woId}`).then(r => r.json()),
    ])
    .then(([d, parts]) => {
      if (d.error) { body.innerHTML = `<div style="padding:24px;color:#dc2626">${_woEsc(d.error)}</div>`; return; }
      const active = (parts || []).filter(p => !(p.wo_product_status||'').toLowerCase().includes('cancel') &&
        ((p.order_date && String(p.order_date).trim()) || (p.acceptance_date && String(p.acceptance_date).trim())));
      const latest = active.reduce((b, p) => (!b || p.soid > b.soid ? p : b), null);
      d.part_shipped   = latest && ((latest.ship_pickup_time && String(latest.ship_pickup_time).trim()) || (latest.shipment_date && String(latest.shipment_date).trim())) ? 1 : 0;
      const _pod = latest ? (String(latest.ship_pou_pod_time||'').trim() || String(latest.delivery_date||'').trim()) : '';
      d.part_pod       = _pod ? 1 : 0;
      d.part_pod_raw   = _pod;
      d.part_has_order = latest ? 1 : 0;
      const _dc = latest && latest.dc_number != null ? String(latest.dc_number).trim() : '';
      d.part_dc_filled  = (_dc && _dc !== '0') ? 1 : 0;
      d.part_eta        = latest ? String(latest.target||'').trim() : '';
      d.part_awb        = latest ? String(latest.awb||'').trim() : '';
      d.part_pickup_raw = latest ? (String(latest.ship_pickup_time||'').trim() || String(latest.shipment_date||'').trim()) : '';
      d.part_return_flag_y = active.some(p => (p.return_flag||'').toUpperCase() === 'Y') ? 1 : 0;
      const renderBase = () => {
        body.innerHTML =
          _woRenderHero(d) +
          `<div class="detail-body-inner">` +
            _woRenderTimeline(d, parts) +
            _woRenderParts(parts, d.work_order_id) +
            (d.serial_number ? `<div id="wo-sn-hist-section"><div style="padding:20px 0 4px;font-size:12px;color:#8b95a1">Loading problem history…</div></div>` : '') +
          `</div>`;
        _woInitCollapsibles();
        body.querySelectorAll('.related-wo-id').forEach(b => b.addEventListener('click', () => openWoDetail(b.dataset.wo)));
      };
      renderBase();
      if (d.serial_number) {
        fetch('/admin/api/sn-history/' + encodeURIComponent(d.serial_number))
          .then(r => r.json())
          .then(snData => {
            const sec = body.querySelector('#wo-sn-hist-section');
            if (sec) {
              sec.outerHTML = _woRenderSnHistory(snData.serial_number, snData.rows, d.work_order_id);
              _woInitCollapsibles();
              body.querySelectorAll('.related-wo-id').forEach(b => b.addEventListener('click', () => openWoDetail(b.dataset.wo)));
            }
          })
          .catch(() => { const sec = body.querySelector('#wo-sn-hist-section'); if (sec) sec.outerHTML = ''; });
      }
    })
    .catch(() => { body.innerHTML = '<div style="padding:24px;color:#dc2626">Failed to load WO detail.</div>'; });
  };

  window.closeWoDetail = function() { document.getElementById('wo-det-overlay').classList.remove('open'); };

  /* ── WO rendering helpers ────────────────────────────────────── */
  function _woEsc(s) { return String(s==null?'':s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;'); }
  function _woIsSentinel(s) { return s && String(s).slice(0,4) === '2099'; }
  function _woFmt(s) { return (s && !_woIsSentinel(s)) ? String(s).slice(0,16).replace('T',' ') : '—'; }
  function _woFmtShort(s) {
    if (!s || _woIsSentinel(s)) return null;
    const d = new Date(String(s).replace(' ','T'));
    if (isNaN(d)) return s.slice(0,16).replace('T',' ');
    const day = d.getDate().toString().padStart(2,'0');
    const mon = ['Jan','Feb','Mar','Apr','May','Jun','Jul','Aug','Sep','Oct','Nov','Dec'][d.getMonth()];
    return `${day} ${mon}\n${d.getHours().toString().padStart(2,'0')}:${d.getMinutes().toString().padStart(2,'0')}`;
  }
  function _woStatusBadge(s) {
    if (!s) return '<span class="badge badge-s-default">—</span>';
    const sl = s.toLowerCase();
    let cls;
    if (sl==='closed'||sl==='completed'||sl==='repair completed'||sl==='ready for pickup'||sl==='rma in progress'||sl.includes('unit returned')) cls='badge-s-closed';
    else if (sl.includes('transit'))                        cls='badge-s-transit';
    else if (sl.includes('part')&&sl.includes('hold'))     cls='badge-s-part-hold';
    else if (sl.includes('part')&&sl.includes('request'))  cls='badge-s-part-req';
    else if (sl.includes('part')&&sl.includes('deliver'))  cls='badge-s-part-delivered';
    else if (sl==='customer hold')                         cls='badge-s-cust-hold';
    else if (sl==='in repair')                             cls='badge-s-in-repair';
    else if (sl.includes('technician assigned'))           cls='badge-s-tech-assigned';
    else if (sl==='order accepted')                        cls='badge-s-accepted';
    else if (sl.includes('pick')&&sl.includes('pack'))     cls='badge-s-pick-pack';
    else if (sl.includes('cancel'))                        cls='badge-s-cancelled';
    else cls='badge-s-default';
    return `<span class="badge ${cls}">${_woEsc(s)}</span>`;
  }
  function _woCaseStatusPill(val) {
    if (!val) return '';
    const v = val.toLowerCase();
    let bg, color, border;
    if (v==='closed'||v==='solution provided')           { bg='#f0fdf4';color='#15803d';border='#bbf7d0'; }
    else if (v==='cancelled')                            { bg='#fef2f2';color='#b91c1c';border='#fecaca'; }
    else if (v==='escalated')                            { bg='#fff7ed';color='#c2410c';border='#fed7aa'; }
    else if (v==='wo on hold'||v==='customer action')    { bg='#fffbeb';color='#b45309';border='#fde68a'; }
    else if (v==='wo follow up'||v==='in progress')      { bg='#f5f3ff';color='#6d28d9';border='#ddd6fe'; }
    else                                                 { bg='#f7f8fa';color='#57606a';border='#e5e7eb'; }
    return `<span style="display:inline-block;padding:2px 8px;border-radius:999px;font-size:11px;font-weight:600;background:${bg};color:${color};border:1px solid ${border}">${_woEsc(val)}</span>`;
  }
  function _woClosingCodePill(val) {
    if (!val) return '';
    const v = val.toLowerCase();
    let bg, color, border;
    if (v.includes('problem fixed'))                                       { bg='#f0fdf4';color='#15803d';border='#bbf7d0'; }
    else if (v.includes('need follow up'))                                 { bg='#fffbeb';color='#b45309';border='#fde68a'; }
    else if (v.includes('cancel'))                                         { bg='#fef2f2';color='#b91c1c';border='#fecaca'; }
    else if (v.includes('no trouble')||v.includes('cannot recreate'))      { bg='#f5f3ff';color='#6d28d9';border='#ddd6fe'; }
    else if (v.includes('customer induced')||v.includes('machine not found')){ bg='#fff7ed';color='#c2410c';border='#fed7aa'; }
    else                                                                   { bg='#f7f8fa';color='#57606a';border='#e5e7eb'; }
    return `<span style="display:inline-block;padding:2px 8px;border-radius:999px;font-size:11px;font-weight:600;background:${bg};color:${color};border:1px solid ${border}">${_woEsc(val)}</span>`;
  }
  const _WO_ICONS = {
    timeline: `<svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"><path d="M12 2v20M2 12h20"/><circle cx="12" cy="12" r="3"/></svg>`,
    parts   : `<svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"><path d="M21 16V8a2 2 0 0 0-1-1.73l-7-4a2 2 0 0 0-2 0l-7 4A2 2 0 0 0 3 8v8a2 2 0 0 0 1 1.73l7 4a2 2 0 0 0 2 0l7-4A2 2 0 0 0 21 16z"/></svg>`,
    closing : `<svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"><polyline points="20 6 9 17 4 12"/></svg>`,
    related : `<svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"><path d="M17 21v-2a4 4 0 0 0-4-4H5a4 4 0 0 0-4 4v2"/><circle cx="9" cy="7" r="4"/><path d="M23 21v-2a4 4 0 0 0-3-3.87M16 3.13a4 4 0 0 1 0 7.75"/></svg>`,
  };
  function _woST(icon, text) { return `<div class="detail-section-title">${icon}<span>${_woEsc(text)}</span></div>`; }
  function _woRenderSnHistory(sn, rows, currentWoId) {
    const uid = 'wo-snhist-' + Date.now();
    const snLabel = `<div style="font-size:12px;color:#57606a;margin-bottom:10px">Laptop Device SN: <strong style="font-family:ui-monospace,monospace;color:#1f2328">${_woEsc(sn||'—')}</strong></div>`;
    const title   = `<div class="detail-section-title collapsible-hdr" data-target="${uid}">${_WO_ICONS.related}<span>All Problem History (SN: ${_woEsc(sn||'—')})</span><svg class="caret" width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><polyline points="6 9 12 15 18 9"/></svg></div>`;
    if (!rows || !rows.length) return `<div class="detail-section">${title}<div id="${uid}" class="collapsible-body" style="max-height:2000px">${snLabel}<div style="font-size:12px;color:#8c959f;font-style:italic;padding:4px 0 8px">No work orders found.</div></div></div>`;
    const woCount = `<div style="font-size:11px;color:#8b95a1;margin-bottom:8px">${rows.length} WO${rows.length!==1?'s':''} found</div>`;
    const groups = {}, order = [];
    rows.forEach(r => { const key = r.case_number ? String(r.case_number) : '__none__'; if (!groups[key]) { groups[key]=[]; order.push(key); } groups[key].push(r); });
    const thS = 'padding:8px 10px;text-align:left;font-size:10px;font-weight:700;color:#57606a;text-transform:uppercase;letter-spacing:.04em;border-bottom:1px solid #e5e7eb;white-space:nowrap';
    let globalIdx = 0, tbody = '';
    order.forEach((key, gIdx) => {
      const wos = groups[key]; const ticket = key==='__none__'?'—':key;
      wos.forEach((r, i) => {
        globalIdx++;
        const isCurrent = String(r.work_order_id) === String(currentWoId);
        const groupTop  = i===0&&gIdx>0?`style="border-top:2px solid #e5e7eb"`:'';
        const rowBg     = isCurrent ? 'background:#fffbeb' : '';
        const currentTag= isCurrent?`<span style="margin-left:6px;font-size:10px;font-weight:700;color:#92400e;background:#fef3c7;border:1px solid #fde68a;border-radius:10px;padding:1px 7px;vertical-align:middle">current</span>`:'';
        const woCell    = isCurrent ? `<span style="font-family:ui-monospace,monospace;font-size:13px;font-weight:700;color:#1f2328">${_woEsc(r.work_order_id)}</span>${currentTag}` : `<button class="related-wo-id" data-wo="${_woEsc(r.work_order_id)}">${_woEsc(r.work_order_id)}</button>`;
        const ticketCell= i===0?`<td rowspan="${wos.length}" style="font-family:ui-monospace,monospace;font-size:12px;font-weight:600;color:#3b82d4;vertical-align:top;padding:9px 10px;white-space:nowrap;border-right:2px solid #bfdbfe;background:#eff6ff${gIdx>0?';border-top:2px solid #e5e7eb':''}">${_woEsc(ticket)}</td>`:'';
        tbody += `<tr style="${rowBg}" ${groupTop}><td style="text-align:center;color:#8c959f;font-size:12px;width:32px;padding:9px 10px;border-bottom:1px solid #f0f2f5">${globalIdx}</td>${ticketCell}<td style="padding:9px 10px;border-bottom:1px solid #f0f2f5;vertical-align:top">${woCell}</td><td style="padding:8px 10px;border-bottom:1px solid #f0f2f5;vertical-align:top">${_woStatusBadge(r.work_order_status)}</td><td style="font-size:12px;padding:9px 10px;border-bottom:1px solid #f0f2f5;vertical-align:top">${_woEsc(r.work_order_type||'—')}</td><td style="font-size:12px;white-space:nowrap;padding:9px 10px;border-bottom:1px solid #f0f2f5;vertical-align:top">${_woEsc(r.created_on?r.created_on.slice(0,10):'—')}</td><td style="font-size:12px;white-space:nowrap;padding:9px 10px;border-bottom:1px solid #f0f2f5;vertical-align:top">${_woEsc(r.completion_date?r.completion_date.slice(0,10):'—')}</td><td style="font-size:12px;max-width:200px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;padding:9px 10px;border-bottom:1px solid #f0f2f5;vertical-align:top" title="${_woEsc(r.case_desc||'')}">${_woEsc(r.case_desc||'—')}</td></tr>`;
      });
    });
    const table = `<div style="overflow-x:auto;background:#fff;border:1px solid #e5e7eb;border-radius:8px;overflow:clip"><table style="width:100%;border-collapse:collapse;font-size:13px"><thead><tr style="background:#f7f8fa"><th style="${thS};text-align:center;width:32px">#</th><th style="${thS}">Ticket (Case #)</th><th style="${thS}">WO Number</th><th style="${thS}">WO Status</th><th style="${thS}">Type</th><th style="${thS}">Created</th><th style="${thS}">Completed</th><th style="${thS};white-space:normal">Case Description</th></tr></thead><tbody>${tbody}</tbody></table></div>`;
    return `<div class="detail-section">${title}<div id="${uid}" class="collapsible-body" style="max-height:4000px">${snLabel}${woCount}${table}</div></div>`;
  }
  function _woRenderHero(d) {
    const bar1 = [
      d.work_order_type ? {l:'Type:', v:d.work_order_type, cls:'gray'} : null,
      d.work_order_priority && d.work_order_priority.toLowerCase()!=='normal' ? {l:'Priority:', v:d.work_order_priority, cls:'red'} : null,
      d.premier_service  ? {l:'Premier:', v:d.premier_service,  cls:'purple'} : null,
      d.order_type       ? {l:'Order Type:', v:d.order_type,    cls:'gray'}   : null,
    ].filter(Boolean);
    const hasContact = d.contact_name||d.mobile_phone||d.primary_email||d.address||d.company_name;
    const hasAsp     = d.customer||d.labor_vendor_related||d.technician_id;
    const customerCard = (hasContact||hasAsp)?`<div class="wo-customer-wrap">${hasContact?`<div class="wo-customer-box"><div class="wo-customer-col">${d.contact_name?`<div class="wo-customer-name">${_woEsc(d.contact_name)}</div>`:''}<${d.mobile_phone?`<div class="wo-customer-row"><span class="wo-customer-lbl">Mobile:</span><span class="wo-customer-val">${_woEsc(d.mobile_phone)}</span></div>`:''}</div><div class="wo-customer-col">${d.company_name?`<div class="wo-customer-row"><span class="wo-customer-lbl">Company:</span><span class="wo-customer-val">${_woEsc(d.company_name)}</span></div>`:''}</div></div>`:''} ${hasAsp?`<div class="wo-customer-box asp"><div class="wo-customer-col">${d.customer?`<div class="wo-customer-asp">${_woEsc(d.customer)}</div>`:''}</div></div>`:''}</div>`:'';
    const infoBar = bar1.length?`<div class="wo-info-bar">${bar1.map(i=>`<div class="wo-info-item"><span class="wo-info-lbl">${_woEsc(i.l)}</span><span class="wo-info-val ${i.cls}">${_woEsc(i.v)}</span></div>`).join('')}</div>`:'';
    return `<div class="wo-hero"><div style="display:flex;align-items:center;flex-wrap:wrap;gap:8px;"><div class="wo-hero-id"><span style="color:#8c959f">WO#</span> ${_woEsc(String(d.work_order_id))}</div>${_woStatusBadge(d.work_order_status)}${d.closing_code?_woClosingCodePill(d.closing_code):''}</div><div style="margin-top:6px;display:grid;grid-template-columns:200px 1fr;row-gap:4px;align-items:center;">${d.serial_number?`<div style="display:flex;align-items:center;gap:4px"><span style="font-size:10px;font-weight:700;text-transform:uppercase;letter-spacing:.05em;color:#8c959f">SN:</span><strong style="color:#1f2328;font-family:ui-monospace,monospace">${_woEsc(d.serial_number)}</strong></div>`:'<div></div>'}<div style="display:flex;align-items:center;gap:4px;flex-wrap:wrap">${d.case_number?`<span style="font-size:10px;font-weight:700;text-transform:uppercase;letter-spacing:.05em;color:#8c959f">CASE#</span><span style="font-family:ui-monospace,monospace;color:#1f2328;font-weight:700">${_woEsc(d.case_number)}</span>`:''} ${d.case_status?_woCaseStatusPill(d.case_status):''}</div></div>${d.case_desc?`<div style="margin-top:10px;font-size:12px;color:#57606a;background:#fff;border:1px solid #e5e7eb;border-radius:6px;padding:8px 12px">${_woEsc(d.case_desc)}</div>`:''}${customerCard}${infoBar}</div>`;
  }
  function _woRenderTimeline(d, parts) {
    const now=Date.now();
    const active=(parts&&parts.filter(p=>!(p.wo_product_status||'').toLowerCase().includes('cancel')).length>0)?parts.filter(p=>!(p.wo_product_status||'').toLowerCase().includes('cancel')):(parts||[]);
    let lp=active[0]||null; for(const p of active){if(!lp||(p.soid>lp.soid))lp=p;}
    const ord=lp?(lp.order_date||lp.acceptance_date||null):null;
    const pick=lp?(lp.ship_pickup_time||lp.shipment_date||null):null;
    const pod=lp?(lp.ship_pou_pod_time||lp.delivery_date||null):null;
    const closed=!!(d.completion_date||d.closing_date);
    const nd=v=>(_woIsSentinel(v)?null:v);
    const nodes=[{label:'Case\nCreated',date:nd(d.created_on)},{label:'Released',date:nd(d.release_date)},{label:'Part Order',date:nd(ord)},{label:'Part Pickup',date:nd(pick)},{label:'Part Received\n(POD)',date:nd(pod)},{label:'Orig. Onsite',date:nd(d.original_committed_onsite_date)},{label:'Actual\nOnsite',date:nd(d.actual_committed_onsite_date)},{label:'Defer Date',date:nd(d.customer_defer_date),optional:true},{label:'Completion',date:nd(d.completion_date)},{label:'Closed',date:nd(d.closing_date)}];
    let lastDone=-1; nodes.forEach((n,i)=>{if(n.date)lastDone=i;});
    const nodeHtml=nodes.map((n,i)=>{
      const hasDate=!!n.date,ts=hasDate?new Date(String(n.date).replace(' ','T')).getTime():null,isPast=ts&&ts<=now,isOverdue=!hasDate&&i>0&&i<=lastDone+1&&!n.optional,forceDone=closed&&((hasDate&&(i===5||i===6))||(i===7&&hasDate)),forceGrey=closed&&i===7&&!hasDate,forceActive=!closed&&hasDate&&(i===5||i===6),forceSkip=!closed&&hasDate&&i===7;
      let state;
      if(forceDone)state='done';else if(forceGrey)state='future';else if(forceActive)state='active';else if(forceSkip)state='skipped';else if(hasDate&&isPast)state='done';else if(hasDate)state='active';else if(isOverdue)state='future';else if(n.optional&&!hasDate)state='skipped';else state='future';
      const connCls=i<nodes.length-1?`tl-connector ${(state==='done'&&i<lastDone)?'done':''}` :'';
      const ds=_woFmtShort(n.date),dateCls=(i===7&&hasDate)?'active-text':state==='done'?'done-text':state==='active'?'active-text':'future-text';
      let dotSvg='';
      if(state==='done')dotSvg=`<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="#22c55e" stroke-width="3" stroke-linecap="round" stroke-linejoin="round"><polyline points="20 6 9 17 4 12"/></svg>`;
      else if(state==='active')dotSvg=`<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="#3b82f6" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="4"/></svg>`;
      else dotSvg=`<div class="tl-dot-inner"></div>`;
      return `<div class="tl-node"><div class="tl-dot-wrap ${state}">${dotSvg}</div><div class="tl-label">${n.label.replace(/\n/g,'<br>')}</div><div class="tl-date ${dateCls}">${ds?ds.replace(/\n/g,'<br>'):'<span style="color:#d1d5db">—</span>'}</div></div>${i<nodes.length-1?`<div class="${connCls}"></div>`:''}`;
    }).join('');
    return `<div class="detail-section">${_woST(_WO_ICONS.timeline,'Date Timeline')}<div class="tl-wrap"><div class="tl-scroll"><div class="tl-track">${nodeHtml}</div></div></div></div>`;
  }
  function _woPartStatusBadge(s){if(!s)return'<span class="badge badge-s-default">—</span>';const sl=s.toLowerCase().trim();let cls;if(sl==='delivered')cls='badge-s-part-delivered';else if(sl==='shipped')cls='badge-s-transit';else if(sl.includes('pick')&&sl.includes('pack'))cls='badge-s-pick-pack';else if(sl==='accepted')cls='badge-s-accepted';else if(sl==='pending')cls='badge-s-part-req';else if(sl.includes('hold'))cls='badge-s-part-hold';else if(sl.includes('cancel'))cls='badge-s-cancelled';else cls='badge-s-default';return`<span class="badge ${cls}">${_woEsc(s)}</span>`;}
  function _woRenderParts(parts, woId) {
    const uid='wo-parts-'+Date.now();
    if(!parts||!parts.length)return`<div class="detail-section">${_woST(_WO_ICONS.parts,`Part Order History (WO #${_woEsc(String(woId||''))}`)}<div style="font-size:13px;color:#8c959f;font-style:italic;padding:4px 0 8px">No part order lines found for this WO.</div></div>`;
    const active=parts.filter(p=>!(p.wo_product_status||'').toLowerCase().includes('cancel'));
    const cancelled=parts.filter(p=>(p.wo_product_status||'').toLowerCase().includes('cancel'));
    function card(p,i,isCx){
      const _dc=p.dc_number!=null?String(p.dc_number).trim():'',_dcFilled=_dc&&_dc!=='0',_dcDisplay=_dcFilled&&_dc!=='1';
      const retFlag=p.return_flag==='Y'?(_dcDisplay?`<span class="badge" style="background:#dcfce7;color:#15803d;border:1px solid #86efac">Returned (DC# ${_woEsc(_dc)})</span>`:_dcFilled?`<span class="badge" style="background:#dcfce7;color:#15803d;border:1px solid #86efac">Returned</span>`:'<span class="badge" style="background:#fff7ed;color:#c2410c;border:1px solid #fed7aa">Return Yes</span>'):(p.return_flag==='N'?'<span class="badge badge-default">No Return</span>':'<span style="color:#8c959f">—</span>');
      const eta=p.target,pod=p.ship_pou_pod_time||p.delivery_date,etaFmt=_woFmt(eta);
      let etaCell;
      if(pod||!eta){etaCell=`<div class="part-card-field"><div class="pcf-label">Target ETA</div><div class="pcf-value">${etaFmt}</div></div>`;}
      else{const ov=eta&&new Date(String(eta).replace(' ','T'))<new Date();etaCell=`<div class="part-card-field ${ov?'eta-overdue':'eta-pending'}"><div class="pcf-label">Target ETA</div><div class="pcf-value">${etaFmt}</div><div><span class="eta-pill ${ov?'overdue':'pending'}">${ov?'Overdue':'Awaiting'}</span></div></div>`;}
      return`<div class="part-card${isCx?' cancelled':''}"><div class="part-card-hdr"><span class="part-card-num">#${i+1}</span><span class="part-card-pn">${_woEsc(p.product||'—')}</span><span class="part-card-desc" title="${_woEsc(p.description||'')}">${_woEsc(p.description||'—')}</span><div style="margin-left:auto;display:flex;gap:6px;align-items:center;flex-shrink:0;flex-wrap:wrap;justify-content:flex-end">${_woPartStatusBadge(p.wo_product_status)}${retFlag}</div></div><div class="part-card-body"><div class="part-card-field"><div class="pcf-label">SOID</div><div class="pcf-value mono">${_woEsc(p.soid||'—')}</div></div><div class="part-card-field"><div class="pcf-label">AWB</div><div class="pcf-value mono">${_woEsc(p.awb||'—')}</div></div><div class="part-card-field"><div class="pcf-label">SLA</div><div class="pcf-value">${_woEsc(p.sla||'—')}</div></div><div class="part-card-field"><div class="pcf-label">Order Date</div><div class="pcf-value">${_woFmt(p.order_date||p.acceptance_date)}</div></div><div class="part-card-field"><div class="pcf-label">Pickup Date</div><div class="pcf-value">${_woFmt(p.ship_pickup_time||p.shipment_date)}</div></div>${etaCell}<div class="part-card-field"><div class="pcf-label">Part Received (POD)</div><div class="pcf-value">${_woFmt(p.ship_pou_pod_time||p.delivery_date)}</div></div></div></div>`;
    }
    const aC=active.map((p,i)=>card(p,i,false)),cC=cancelled.map((p,i)=>card(p,i,true));
    const div=cC.length&&aC.length?`<div class="parts-cancelled-divider"><span>Cancelled (${cC.length})</span></div>`:'';
    return`<div class="detail-section"><div class="detail-section-title collapsible-hdr" data-target="${uid}">${_WO_ICONS.parts}<span>Part Order History (WO #${_woEsc(String(woId||''))})</span><svg class="caret" width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><polyline points="6 9 12 15 18 9"/></svg></div><div class="collapsible-body" id="${uid}" style="max-height:2000px">${[...aC,div?[div]:[],... cC].flat().join('')}</div></div>`;
  }
  function _woInitCollapsibles() {
    document.querySelectorAll('#wo-det-body .collapsible-hdr').forEach(hdr => {
      hdr.addEventListener('click', () => {
        const t = document.getElementById(hdr.dataset.target);
        if (!t) return;
        if (t.classList.contains('collapsed')) { t.classList.remove('collapsed'); t.style.maxHeight = t.scrollHeight+'px'; hdr.classList.remove('collapsed'); }
        else { t.style.maxHeight = t.scrollHeight+'px'; requestAnimationFrame(() => { t.classList.add('collapsed'); hdr.classList.add('collapsed'); }); }
      });
    });
  }

  /* ── ESC key ─────────────────────────────────────────────────── */
  document.addEventListener('keydown', function(e) {
    if (e.key === 'Escape') { closeWoDetail(); closeDrawer(); closeDetail(); closeHistory(); }
  });

  /* ── Sidebar collapse ────────────────────────────────────────── */
  window.toggleSidebar = function() { document.getElementById('filter-sidebar').classList.toggle('collapsed'); };

  /* ── A–Z index builder ───────────────────────────────────────── */
  function buildIndex() {
    const list  = document.getElementById('board-filter-list');
    const index = document.getElementById('board-index');

    // Remove any separators that may have been inserted by a previous call
    list.querySelectorAll('.md-filter-letter-sep').forEach(el => el.remove());
    // Clear the right-rail index so letters aren't appended twice
    index.innerHTML = '';

    const items = Array.from(list.querySelectorAll('.md-filter-item:not(.md-filter-all)'));
    const lettersWithItems = new Set(items.map(el => (el.dataset.initial || '').toUpperCase()).filter(l => /[A-Z]/.test(l)));
    const seen = new Set();
    items.forEach(el => {
      const letter = (el.dataset.initial || '').toUpperCase();
      if (letter && !seen.has(letter)) {
        seen.add(letter);
        const sep = document.createElement('div');
        sep.className = 'md-filter-letter-sep'; sep.textContent = letter; sep.dataset.sep = letter;
        list.insertBefore(sep, el);
      }
    });
    'ABCDEFGHIJKLMNOPQRSTUVWXYZ'.split('').forEach(letter => {
      const btn = document.createElement('div');
      btn.className = 'md-idx-letter' + (lettersWithItems.has(letter) ? ' has-items' : '');
      btn.textContent = letter; btn.dataset.letter = letter;
      if (lettersWithItems.has(letter)) {
        btn.addEventListener('click', function() {
          const sep = list.querySelector(`.md-filter-letter-sep[data-sep="${letter}"]`);
          if (sep) sep.scrollIntoView({ block: 'start', behavior: 'smooth' });
          index.querySelectorAll('.md-idx-letter').forEach(b => b.classList.remove('active'));
          btn.classList.add('active');
          setTimeout(() => btn.classList.remove('active'), 800);
        });
      }
      index.appendChild(btn);
    });
  }

  /* ── Fetch data from API and render ─────────────────────────── */
  window.loadData = function(isRefresh) {
    // When the user clicks the refresh button (isRefresh=true), also
    // reload meta so board/status counts stay current.
    if (isRefresh) { _loadMeta(false); return; }

    const tbody = document.getElementById('data-tbody');
    const btn   = document.getElementById('btn-refresh-data');
    tbody.innerHTML = '<tr><td colspan="8" style="text-align:center;color:#9ca3af;padding:32px">Loading…</td></tr>';
    if (btn) { btn.disabled = true; btn.classList.add('spinning'); }

    // Build query string from current filter state
    const params = new URLSearchParams({ page: _page, per_page: PAGE_SIZE });
    if (_activeBoard)  params.set('board_id', _activeBoard);
    if (_activeStatus) params.set('status',   _activeStatus);
    if (_searchQ)      params.set('q',        _searchQ);

    fetch('/admin/api/monday-data?' + params.toString())
      .then(r => r.json())
      .then(data => {
        render(data);
      })
      .catch(() => {
        tbody.innerHTML = '<tr><td colspan="8" style="text-align:center;color:#dc2626;padding:32px">Failed to load data. Please try again.</td></tr>';
        document.getElementById('row-count-label').textContent = 'Error';
      })
      .finally(() => {
        if (btn) { btn.disabled = false; btn.classList.remove('spinning'); }
      });
  };

  /* ── Load meta (boards + status counts) ─────────────────────── */
  function _loadMeta(isFirstLoad) {
    fetch('/admin/api/monday-data/meta')
      .then(r => r.json())
      .then(meta => {
        BOARD_NAMES = meta.board_names || {};

        // Update sidebar "All Boards" count
        const countAll = document.getElementById('fi-count-all');
        if (countAll) countAll.textContent = meta.total_count || 0;

        _buildStatusPills(meta.statuses || [], meta.total_count || 0);

        if (isFirstLoad) {
          buildIndex();
          // Default to "In Progress" on first load only
          _activeStatus = PINNED_STATUS;
          const sel = document.getElementById('status-select');
          if (sel) { sel.value = PINNED_STATUS; sel.classList.add('has-filter'); }
        } else {
          // Restore current selection after _buildStatusPills rebuilt innerHTML
          const sel = document.getElementById('status-select');
          if (sel) {
            sel.value = _activeStatus;
            sel.classList.toggle('has-filter', !!_activeStatus);
          }
        }

        loadData(false);
      })
      .catch(() => {
        // If meta fails, still try to load data
        loadData(false);
      });
  }

  _loadMeta(true);

})();
