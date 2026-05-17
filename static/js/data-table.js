/* ================================================================
   data-table.js — Column picker, density toggle, expand rows,
                   client-side sort hint, page size selector
   ================================================================ */
(function () {
  'use strict';

  /* ── Density ───────────────────────────────────────────────── */
  function initDensity() {
    var btn = document.getElementById('density-btn');
    if (!btn) return;

    var densities = ['default', 'compact', 'comfortable'];
    var saved;
    try { saved = localStorage.getItem('bbs_density'); } catch(e) {}
    var current = saved || 'default';

    function applyDensity(d) {
      document.body.dataset.density = d;
      btn.title = 'Density: ' + d + ' (click to change)';
    }

    applyDensity(current);

    btn.addEventListener('click', function() {
      var idx = densities.indexOf(current);
      current = densities[(idx + 1) % densities.length];
      applyDensity(current);
      try { localStorage.setItem('bbs_density', current); } catch(e) {}
    });
  }

  /* ── Column picker ─────────────────────────────────────────── */
  function initColPicker() {
    var btn    = document.getElementById('col-picker-btn');
    var menu   = document.getElementById('col-picker-menu');
    var table  = document.getElementById('data-table');
    if (!btn || !menu || !table) return;

    var tableId = table.dataset.tableId || 'default';
    var key     = 'bbs_cols_' + tableId;
    var hidden  = [];
    try { hidden = JSON.parse(localStorage.getItem(key)) || []; } catch(e) {}

    function applyVisibility() {
      var ths = table.querySelectorAll('thead th[data-col]');
      ths.forEach(function(th) {
        var col = th.dataset.col;
        var vis = hidden.indexOf(col) === -1;
        th.style.display = vis ? '' : 'none';
        // Matching tds
        var idx = Array.from(th.parentNode.children).indexOf(th);
        table.querySelectorAll('tbody tr:not(.row-detail)').forEach(function(tr) {
          var td = tr.children[idx];
          if (td) td.style.display = vis ? '' : 'none';
        });
      });
    }

    // Build menu
    var ths = table.querySelectorAll('thead th[data-col]');
    ths.forEach(function(th) {
      var col   = th.dataset.col;
      var label = th.dataset.label || th.textContent.trim();
      var item  = document.createElement('label');
      item.className = 'col-picker-item';
      var cb = document.createElement('input');
      cb.type    = 'checkbox';
      cb.checked = hidden.indexOf(col) === -1;
      cb.dataset.col = col;
      cb.addEventListener('change', function() {
        if (cb.checked) {
          hidden = hidden.filter(function(c){ return c !== col; });
        } else {
          hidden.push(col);
        }
        try { localStorage.setItem(key, JSON.stringify(hidden)); } catch(e) {}
        applyVisibility();
      });
      item.appendChild(cb);
      item.appendChild(document.createTextNode(' ' + label));
      menu.appendChild(item);
    });

    applyVisibility();

    btn.addEventListener('click', function(e) {
      e.stopPropagation();
      menu.classList.toggle('open');
    });
    document.addEventListener('click', function(e) {
      if (!menu.contains(e.target) && e.target !== btn) menu.classList.remove('open');
    });
  }

  /* ── Expand row ────────────────────────────────────────────── */
  function initExpandRows() {
    document.querySelectorAll('.expand-btn').forEach(function(btn) {
      btn.addEventListener('click', function() {
        var tr     = btn.closest('tr');
        var detail = tr.nextElementSibling;
        if (detail && detail.classList.contains('row-detail')) {
          detail.style.display = detail.style.display === 'none' ? '' : 'none';
          btn.classList.toggle('expanded', detail.style.display !== 'none');
        }
      });
    });
    // Start collapsed
    document.querySelectorAll('tr.row-detail').forEach(function(tr) {
      tr.style.display = 'none';
    });
  }

  /* ── Sort columns (updates URL params, lets server sort) ───── */
  function initSortColumns() {
    var table = document.getElementById('data-table');
    if (!table) return;

    table.querySelectorAll('thead th.sortable[data-col]').forEach(function(th) {
      th.addEventListener('click', function() {
        var url = new URL(window.location.href);
        var col  = th.dataset.col;
        var curDir  = url.searchParams.get('dir')  || 'desc';
        var curSort = url.searchParams.get('sort') || '';
        var newDir  = (curSort === col && curDir === 'asc') ? 'desc' : 'asc';
        url.searchParams.set('sort', col);
        url.searchParams.set('dir', newDir);
        url.searchParams.set('page', '1');
        window.location = url.toString();
      });
    });
  }

  /* ── Page size selector ────────────────────────────────────── */
  function initPageSize() {
    var sel = document.getElementById('per-page-select');
    if (!sel) return;
    sel.addEventListener('change', function() {
      var url = new URL(window.location.href);
      url.searchParams.set('per_page', sel.value);
      url.searchParams.set('page', '1');
      window.location = url.toString();
    });
  }

  /* ── Table search ──────────────────────────────────────────── */
  function initTableSearch() {
    var form = document.getElementById('table-search-form');
    var inp  = document.getElementById('table-search-input');
    if (!form || !inp) return;
    // Already a real form submit — just ensure page resets to 1
    form.addEventListener('submit', function() {
      var url = new URL(form.action || window.location.href);
      url.searchParams.set('page', '1');
      form.action = url.toString();
    });
  }

  /* ── Init ──────────────────────────────────────────────────── */
  function init() {
    initDensity();
    initColPicker();
    initExpandRows();
    initSortColumns();
    initPageSize();
    initTableSearch();
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', init);
  } else {
    init();
  }

}());
