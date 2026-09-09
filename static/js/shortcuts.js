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

  var _qsVersion = 0;
  var _qsOpener = null;
  var destinations = [];
  var recentKey = 'bbs_recent_pages';

  function recentPages() {
    try {
      var saved = JSON.parse(sessionStorage.getItem(recentKey) || '[]');
      return Array.isArray(saved) ? saved.filter(function(url) {
        return destinations.some(function(item) { return item.url === url; });
      }).slice(0, 5) : [];
    } catch (e) { return []; }
  }

  function localResults(q) {
    var groups = {};
    if (!q) {
      groups.Recent = recentPages().map(function(url) {
        return destinations.find(function(item) { return item.url === url; });
      });
    }
    groups['Go to'] = destinations.filter(function(item) {
      return item.title.toLowerCase().includes(q.toLowerCase());
    });
    return groups;
  }

  function openQS() {
    if (!_qsModal) return;
    if (!_qsModal.classList.contains('open')) _qsOpener = document.activeElement;
    _qsModal.classList.add('open');
    _qsModal.setAttribute('aria-hidden', 'false');
    clearTimeout(_qsTimer);
    _qsVersion++;
    _qsInput.value = '';
    renderResults(localResults(''), '');
    _qsInput.focus();
  }

  function closeQS() {
    clearTimeout(_qsTimer);
    _qsVersion++;
    if (_qsModal && _qsModal.classList.contains('open')) {
      _qsModal.classList.remove('open');
      _qsModal.setAttribute('aria-hidden', 'true');
      if (_qsOpener && _qsOpener.isConnected) _qsOpener.focus();
    }
  }

  function runSearch(q) {
    var version = ++_qsVersion;
    var groups = localResults(q);
    renderResults(groups, q);
    if (q.length < 2) return;
    fetch('/api/quick-search?q=' + encodeURIComponent(q))
      .then(function(r) {
        if (!r.ok) throw new Error('Search unavailable');
        return r.json();
      })
      .then(function(data) {
        if (version !== _qsVersion) return;
        (data.results || []).forEach(function(item) {
          var group = item.type || 'Content';
          if (!groups[group]) groups[group] = [];
          groups[group].push({title: item.label, sub: item.sub, url: item.url});
        });
        renderResults(groups, q);
      })
      .catch(function() {
        if (version !== _qsVersion) return;
        var notice = document.createElement('p');
        notice.className = 'qs-empty';
        notice.textContent = 'Content search unavailable. Page shortcuts still work.';
        _qsResults.appendChild(notice);
      });
  }

  function renderResults(data, q) {
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
        // Only local destinations may be opened by the palette.
        if (!item.url || !item.url.startsWith('/') || item.url.startsWith('//') || item.url.includes('\\')) return;
        var a = document.createElement('a');
        a.className = 'qs-result';
        a.href = item.url;
        a.innerHTML = '<div><div>' + escHtml(item.title || '') + '</div>' +
          (item.sub ? '<div class="qs-result-sub">' + escHtml(item.sub) + '</div>' : '') + '</div>';
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

    /* QS modal navigation also works while the search input has focus. */
    if (_qsModal && _qsModal.classList.contains('open')) {
      if (e.key === 'Tab') {
        var focusable = [_qsInput].concat(Array.from(_qsResults.querySelectorAll('.qs-result')));
        var index = focusable.indexOf(document.activeElement);
        var next = (index + (e.shiftKey ? -1 : 1) + focusable.length) % focusable.length;
        e.preventDefault();
        focusable[next].focus();
        return;
      }
      if (e.key === 'ArrowDown') { e.preventDefault(); qsNavigate(1); return; }
      if (e.key === 'ArrowUp')   { e.preventDefault(); qsNavigate(-1); return; }
      if (e.key === 'Enter') {
        if (_qsCurrent >= 0 && _qsResults) {
          var items = _qsResults.querySelectorAll('.qs-result');
          if (items[_qsCurrent]) { e.preventDefault(); items[_qsCurrent].click(); return; }
        }
      }
      return;
    }

    if (inInput) return;

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

    document.querySelectorAll('.nav-links a, .nav-dropdown-menu a').forEach(function(a) {
      var url = a.getAttribute('href');
      if (!url || !url.startsWith('/') || a.target === '_blank' || destinations.some(function(item) { return item.url === url; })) return;
      destinations.push({title: a.textContent.replace(a.querySelector('.nav-symbol') ? a.querySelector('.nav-symbol').textContent : '', '').trim(), url: url});
    });
    var current = destinations.find(function(item) { return item.url === window.location.pathname; });
    if (current) {
      try { sessionStorage.setItem(recentKey, JSON.stringify([current.url].concat(recentPages().filter(function(url) { return url !== current.url; })).slice(0, 5))); } catch (e) {}
    }
    var searchButton = document.getElementById('quick-search-button');
    if (searchButton) searchButton.addEventListener('click', openQS);

    if (qsInp) {
      qsInp.addEventListener('input', function() {
        clearTimeout(_qsTimer);
        _qsVersion++;
        _qsCurrent = -1;
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
