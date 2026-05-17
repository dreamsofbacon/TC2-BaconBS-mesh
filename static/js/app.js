/* ================================================================
   app.js — BBS Core: theme, CSRF, sync pill, global init
   ================================================================ */
(function () {
  'use strict';

  /* ── Namespace ─────────────────────────────────────────────── */
  window.BBS = window.BBS || {};

  /* ── Theme engine ──────────────────────────────────────────── */
  var THEMES = [
    { id: 'auto',           label: 'Auto (OS)',        swatch: 'linear-gradient(135deg,#0f141b 50%,#f6f7fb 50%)' },
    { id: 'dark',           label: 'Dark',             swatch: '#0f141b' },
    { id: 'light',          label: 'Light',            swatch: '#f6f7fb' },
    { id: 'high-contrast',  label: 'High Contrast',    swatch: '#000' },
    { id: 'solarized-dark', label: 'Solarized Dark',   swatch: '#002b36' },
    { id: 'solarized-light',label: 'Solarized Light',  swatch: '#fdf6e3' },
    { id: 'nord',           label: 'Nord',             swatch: '#2e3440' },
    { id: 'dracula',        label: 'Dracula',          swatch: '#282a36' },
    { id: 'gruvbox-dark',   label: 'Gruvbox Dark',     swatch: '#282828' },
    { id: 'monokai',        label: 'Monokai',          swatch: '#272822' },
    { id: 'cyberpunk',      label: 'Cyberpunk',        swatch: '#0d0221' },
    { id: 'sepia',          label: 'Sepia',            swatch: '#f4ead8' },
  ];

  var STORAGE_KEY = 'bbs_theme';
  var _mq = window.matchMedia('(prefers-color-scheme: dark)');

  function resolveTheme(id) {
    if (!id || id === 'auto') {
      return _mq.matches ? 'dark' : 'light';
    }
    return id;
  }

  function applyTheme(id) {
    var resolved = resolveTheme(id);
    document.documentElement.setAttribute('data-theme', resolved);
    // Update picker label
    var label = document.getElementById('theme-label');
    var theme = THEMES.find(function(t){ return t.id === (id || 'auto'); });
    if (label && theme) label.textContent = theme.label;
    // Mark selected in menu
    document.querySelectorAll('.theme-option').forEach(function(el) {
      el.classList.toggle('selected', el.dataset.theme === (id || 'auto'));
    });
  }

  function saveTheme(id) {
    try { localStorage.setItem(STORAGE_KEY, id); } catch(e) {}
    applyTheme(id);
  }

  function initTheme() {
    var saved;
    try { saved = localStorage.getItem(STORAGE_KEY); } catch(e) {}
    applyTheme(saved || 'auto');
  }

  function buildThemePicker() {
    var menu = document.getElementById('theme-picker-menu');
    if (!menu) return;
    menu.innerHTML = '';
    THEMES.forEach(function(t) {
      var div = document.createElement('div');
      div.className = 'theme-option';
      div.dataset.theme = t.id;
      var swatch = document.createElement('span');
      swatch.className = 'theme-swatch';
      swatch.style.background = t.swatch;
      div.appendChild(swatch);
      div.appendChild(document.createTextNode(t.label));
      div.addEventListener('click', function() {
        saveTheme(t.id);
        menu.classList.remove('open');
      });
      menu.appendChild(div);
    });
  }

  function initThemePicker() {
    buildThemePicker();
    var toggle = document.getElementById('theme-picker-toggle');
    var menu   = document.getElementById('theme-picker-menu');
    if (!toggle || !menu) return;

    toggle.addEventListener('click', function(e) {
      e.stopPropagation();
      menu.classList.toggle('open');
    });
    document.addEventListener('click', function(e) {
      if (!menu.contains(e.target) && e.target !== toggle) {
        menu.classList.remove('open');
      }
    });
    document.addEventListener('keydown', function(e) {
      if (e.key === 'Escape') menu.classList.remove('open');
    });
  }

  _mq.addEventListener('change', function() {
    var saved;
    try { saved = localStorage.getItem(STORAGE_KEY); } catch(e) {}
    if (!saved || saved === 'auto') applyTheme('auto');
  });

  /* ── CSRF injection ────────────────────────────────────────── */
  function injectCsrf() {
    var meta = document.querySelector('meta[name="csrf-token"]');
    if (!meta) return;
    var token = meta.content;
    document.querySelectorAll('form').forEach(function(form) {
      if (form.method && form.method.toLowerCase() === 'post') {
        if (!form.querySelector('input[name="csrf_token"]')) {
          var inp = document.createElement('input');
          inp.type  = 'hidden';
          inp.name  = 'csrf_token';
          inp.value = token;
          form.appendChild(inp);
        }
      }
    });
    // Intercept all fetch / XHR via headers
    BBS._csrfToken = token;
  }

  /* ── Sync pill ─────────────────────────────────────────────── */
  function initSyncPill() {
    var pill = document.getElementById('sync-pill');
    if (!pill) return;

    var paused = false;
    var interval;

    function fetchStatus() {
      if (paused) return;
      fetch('/api/sync-status', { headers: { 'X-CSRF-Token': BBS._csrfToken || '' } })
        .then(function(r) { return r.json(); })
        .then(function(data) {
          pill.textContent = data.status || 'Sync status unavailable';
          pill.classList.toggle('active', !!data.active);
        })
        .catch(function() {
          pill.textContent = '⬆ Sync status unavailable';
        });
    }

    pill.addEventListener('click', function() {
      paused = !paused;
      pill.title = paused ? 'Click to resume' : 'Click to pause';
      pill.style.opacity = paused ? '0.55' : '1';
      if (!paused) fetchStatus();
    });

    fetchStatus();
    interval = setInterval(fetchStatus, 5000);

    BBS._syncPauseToggle = function() { pill.click(); };
    BBS._syncIntervalId  = interval;
  }

  /* ── Drag-to-reorder rows ──────────────────────────────────── */
  function initDragReorder() {
    document.querySelectorAll('tbody[data-reorderable]').forEach(function(tbody) {
      var dragging = null;

      tbody.addEventListener('dragstart', function(e) {
        dragging = e.target.closest('tr');
        if (!dragging) return;
        dragging.classList.add('dragging');
        e.dataTransfer.effectAllowed = 'move';
      });
      tbody.addEventListener('dragend', function() {
        if (dragging) dragging.classList.remove('dragging');
        dragging = null;
        tbody.querySelectorAll('tr').forEach(function(r){ r.classList.remove('drag-over'); });
      });
      tbody.addEventListener('dragover', function(e) {
        e.preventDefault();
        e.dataTransfer.dropEffect = 'move';
        var target = e.target.closest('tr');
        tbody.querySelectorAll('tr').forEach(function(r){ r.classList.remove('drag-over'); });
        if (target && target !== dragging) target.classList.add('drag-over');
      });
      tbody.addEventListener('drop', function(e) {
        e.preventDefault();
        var target = e.target.closest('tr');
        if (!target || target === dragging) return;
        target.classList.remove('drag-over');
        var rows = Array.from(tbody.querySelectorAll('tr'));
        var fromIdx = rows.indexOf(dragging);
        var toIdx   = rows.indexOf(target);
        if (fromIdx < toIdx) {
          tbody.insertBefore(dragging, target.nextSibling);
        } else {
          tbody.insertBefore(dragging, target);
        }

        // POST new order
        var endpoint = tbody.dataset.reorderable;
        if (!endpoint) return;
        var newOrder = Array.from(tbody.querySelectorAll('tr[data-id]')).map(function(r){ return r.dataset.id; });
        fetch(endpoint, {
          method: 'POST',
          headers: {
            'Content-Type': 'application/json',
            'X-CSRF-Token': BBS._csrfToken || '',
          },
          body: JSON.stringify({ order: newOrder }),
        }).catch(function(err){ console.warn('reorder failed', err); });
      });
    });
  }

  /* ── Board selector (bulletin board list) ──────────────────── */
  function initBoardSelector() {
    var sel = document.getElementById('board-select');
    if (!sel) return;
    sel.addEventListener('change', function() {
      var url = new URL(window.location.href);
      url.searchParams.set('board', sel.value);
      url.searchParams.set('page', '1');
      window.location = url.toString();
    });
  }

  /* ── Flowchart pan/zoom ────────────────────────────────────── */
  function initFlowchart() {
    var viewport = document.getElementById('flowchart-viewport');
    var svg      = document.getElementById('flowchart-svg');
    if (!viewport || !svg) return;

    var scale = 1, tx = 0, ty = 0;
    var isPanning = false, startX, startY, startTx, startTy;

    function applyTransform() {
      svg.style.transform = 'translate(' + tx + 'px, ' + ty + 'px) scale(' + scale + ')';
      svg.style.transformOrigin = '0 0';
    }

    viewport.addEventListener('wheel', function(e) {
      e.preventDefault();
      var factor = e.deltaY < 0 ? 1.1 : 0.9;
      var rect = viewport.getBoundingClientRect();
      var mx = e.clientX - rect.left;
      var my = e.clientY - rect.top;
      tx = mx - factor * (mx - tx);
      ty = my - factor * (my - ty);
      scale *= factor;
      scale = Math.max(0.2, Math.min(4, scale));
      applyTransform();
    }, { passive: false });

    viewport.addEventListener('mousedown', function(e) {
      if (e.button !== 0) return;
      isPanning = true;
      startX = e.clientX; startY = e.clientY;
      startTx = tx; startTy = ty;
      viewport.classList.add('dragging');
    });
    document.addEventListener('mousemove', function(e) {
      if (!isPanning) return;
      tx = startTx + (e.clientX - startX);
      ty = startTy + (e.clientY - startY);
      applyTransform();
    });
    document.addEventListener('mouseup', function() {
      isPanning = false;
      viewport.classList.remove('dragging');
    });

    // Buttons
    document.getElementById('zoom-in')  && document.getElementById('zoom-in').addEventListener('click',  function(){ scale = Math.min(4, scale * 1.2); applyTransform(); });
    document.getElementById('zoom-out') && document.getElementById('zoom-out').addEventListener('click', function(){ scale = Math.max(0.2, scale / 1.2); applyTransform(); });
    document.getElementById('zoom-reset') && document.getElementById('zoom-reset').addEventListener('click', function(){ scale=1; tx=0; ty=0; applyTransform(); });
    document.getElementById('zoom-fit') && document.getElementById('zoom-fit').addEventListener('click', function(){
      var vr = viewport.getBoundingClientRect();
      var sr = svg.getBBox ? svg.getBBox() : { width: 2400, height: 1600 };
      var sx = vr.width  / (sr.width  || 2400);
      var sy = vr.height / (sr.height || 1600);
      scale = Math.min(sx, sy) * 0.92;
      tx = (vr.width  - (sr.width  || 2400) * scale) / 2;
      ty = (vr.height - (sr.height || 1600) * scale) / 2;
      applyTransform();
    });

    applyTransform();
  }

  /* ── Init ──────────────────────────────────────────────────── */
  function init() {
    initTheme();
    initThemePicker();
    injectCsrf();
    initSyncPill();
    initDragReorder();
    initBoardSelector();
    initFlowchart();
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', init);
  } else {
    init();
  }

  // Re-inject CSRF if new forms arrive (e.g. ajax-inserted modals)
  BBS.reinjectCsrf = injectCsrf;

}());
