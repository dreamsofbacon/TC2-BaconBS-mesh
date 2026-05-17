/* ================================================================
   shortcuts.js — Keyboard shortcuts + quick-search modal
   ================================================================ */
(function () {
  'use strict';

  window.BBS = window.BBS || {};

  var GOTO = {
    b: '/bulletins',
    c: '/channels',
    s: '/settings',
    d: '/dashboard',
    t: '/system/transmissions',
    l: '/clients',
    m: '/mail',
    g: '/system/flowchart',
  };

  /* ── Quick search modal ────────────────────────────────────── */
  var _qsModal    = null;
  var _qsInput    = null;
  var _qsResults  = null;
  var _qsTimer    = null;
  var _qsCurrent  = -1;

  function openQS() {
    _qsModal = _qsModal || document.getElementById('qs-modal');
    if (!_qsModal) return;
    _qsModal.classList.add('open');
    _qsInput = _qsModal.querySelector('#qs-input');
    _qsResults = _qsModal.querySelector('#qs-results');
    if (_qsInput) { _qsInput.value = ''; _qsInput.focus(); }
    if (_qsResults) _qsResults.innerHTML = '<p class="qs-hint">Type to search bulletins, mail, channels, clients&hellip;</p>';
  }

  function closeQS() {
    if (_qsModal) _qsModal.classList.remove('open');
  }

  function runSearch(q) {
    if (!_qsResults) return;
    if (!q || q.length < 2) {
      _qsResults.innerHTML = '<p class="qs-hint">Type to search bulletins, mail, channels, clients&hellip;</p>';
      return;
    }
    _qsResults.innerHTML = '<p class="qs-hint">Searching&hellip;</p>';
    var csrfToken = (window.BBS && window.BBS._csrfToken) || '';
    fetch('/api/quick-search?q=' + encodeURIComponent(q), {
      headers: { 'X-CSRF-Token': csrfToken }
    })
      .then(function(r) { return r.json(); })
      .then(function(data) {
        renderResults(data);
      })
      .catch(function() {
        _qsResults.innerHTML = '<p class="qs-empty">Search unavailable.</p>';
      });
  }

  function renderResults(data) {
    if (!_qsResults) return;
    _qsResults.innerHTML = '';
    var hasAny = false;
    Object.keys(data).forEach(function(group) {
      var items = data[group];
      if (!items || !items.length) return;
      hasAny = true;
      var grp = document.createElement('div');
      grp.className = 'qs-group';
      var lbl = document.createElement('div');
      lbl.className = 'qs-group-label';
      lbl.textContent = group;
      grp.appendChild(lbl);

      items.forEach(function(item) {
        var a = document.createElement('a');
        a.className = 'qs-result';
        a.href = item.url || '#';
        a.innerHTML = '<div><div>' + escHtml(item.title || '') + '</div>' +
          (item.sub ? '<div class="qs-result-sub">' + escHtml(item.sub) + '</div>' : '') +
          '</div>';
        a.addEventListener('click', closeQS);
        grp.appendChild(a);
      });
      _qsResults.appendChild(grp);
    });
    if (!hasAny) {
      _qsResults.innerHTML = '<p class="qs-empty">No results for "' + escHtml(q) + '".</p>';
    }
    _qsCurrent = -1;
  }

  function escHtml(s) {
    return String(s)
      .replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;')
      .replace(/"/g,'&quot;');
  }

  function qsNavigate(dir) {
    if (!_qsResults) return;
    var items = _qsResults.querySelectorAll('.qs-result');
    if (!items.length) return;
    items[_qsCurrent] && items[_qsCurrent].classList.remove('selected');
    _qsCurrent = Math.max(-1, Math.min(items.length - 1, _qsCurrent + dir));
    if (_qsCurrent >= 0) {
      items[_qsCurrent].classList.add('selected');
      items[_qsCurrent].scrollIntoView({ block: 'nearest' });
    }
  }

  /* ── Shortcut help modal ───────────────────────────────────── */
  function openHelp() {
    var m = document.getElementById('help-modal');
    if (m) m.classList.add('open');
  }
  function closeHelp() {
    var m = document.getElementById('help-modal');
    if (m) m.classList.remove('open');
  }

  /* ── Global keydown ────────────────────────────────────────── */
  var _gMode = false;

  document.addEventListener('keydown', function(e) {
    var tag = document.activeElement && document.activeElement.tagName;
    var inInput = tag === 'INPUT' || tag === 'TEXTAREA' || tag === 'SELECT' ||
                  (document.activeElement && document.activeElement.isContentEditable);

    /* Quick search Ctrl+K / Cmd+K */
    if ((e.ctrlKey || e.metaKey) && e.key === 'k') {
      e.preventDefault();
      openQS();
      return;
    }

    /* Escape closes modals */
    if (e.key === 'Escape') {
      closeQS();
      closeHelp();
      _gMode = false;
      return;
    }

    if (inInput) return;

    /* QS modal navigation */
    if (_qsModal && _qsModal.classList.contains('open')) {
      if (e.key === 'ArrowDown') { e.preventDefault(); qsNavigate(1); return; }
      if (e.key === 'ArrowUp')   { e.preventDefault(); qsNavigate(-1); return; }
      if (e.key === 'Enter') {
        if (_qsCurrent >= 0 && _qsResults) {
          var items = _qsResults.querySelectorAll('.qs-result');
          if (items[_qsCurrent]) { items[_qsCurrent].click(); return; }
        }
      }
      return;
    }

    /* ? opens shortcut help */
    if (e.key === '?') { openHelp(); return; }

    /* / focuses search */
    if (e.key === '/') {
      var search = document.getElementById('table-search-input') ||
                   document.querySelector('.search-box input');
      if (search) { e.preventDefault(); search.focus(); }
      return;
    }

    /* n — new item on table pages */
    if (e.key === 'n') {
      var newBtn = document.getElementById('new-record-btn');
      if (newBtn) { newBtn.click(); return; }
    }

    /* g — goto mode */
    if (e.key === 'g' && !_gMode) {
      _gMode = true;
      setTimeout(function() { _gMode = false; }, 1500);
      return;
    }
    if (_gMode) {
      _gMode = false;
      var dest = GOTO[e.key.toLowerCase()];
      if (dest) { window.location = dest; }
      return;
    }
  });

  /* ── Wire up modal close buttons ───────────────────────────── */
  function init() {
    // QS modal — the .modal-backdrop element itself is id="qs-modal"
    var qsModal   = document.getElementById('qs-modal');
    var qsInp     = document.getElementById('qs-input');
    var helpModal = document.getElementById('help-modal');
    var helpClose = document.getElementById('help-close');

    _qsModal   = qsModal;
    _qsInput   = qsInp;
    _qsResults = qsModal && qsModal.querySelector('#qs-results');

    if (qsInp) {
      qsInp.addEventListener('input', function() {
        clearTimeout(_qsTimer);
        _qsTimer = setTimeout(function() { runSearch(qsInp.value.trim()); }, 280);
      });
    }
    // Close when clicking backdrop (not inner modal)
    if (qsModal) {
      qsModal.addEventListener('click', function(e) {
        if (e.target === qsModal) closeQS();
      });
    }
    if (helpModal) {
      helpModal.addEventListener('click', function(e) {
        if (e.target === helpModal) closeHelp();
      });
    }
    if (helpClose) helpClose.addEventListener('click', closeHelp);

    // Expose for nav use
    BBS.openSearch = openQS;
    BBS.openHelp   = openHelp;
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', init);
  } else {
    init();
  }

}());
