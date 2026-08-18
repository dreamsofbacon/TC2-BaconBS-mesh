/* Settings sidebar: show one section at a time.
 *
 * Progressive enhancement -- the is-tabbed class is added here, so with
 * scripting off every panel renders stacked, exactly as the page did before.
 *
 * Section forms already POST back to <url>#section (see settings.html), so
 * the hash the server redirects to is what picks the panel after a save.
 * That is the whole reason this keys off location.hash rather than its own
 * state: no extra plumbing was needed to survive a round-trip.
 */
(function () {
  var layout = document.getElementById('settings-layout');
  if (!layout) return;

  var panels = Array.prototype.slice.call(layout.querySelectorAll('[data-settings-panel]'));
  var links = Array.prototype.slice.call(layout.querySelectorAll('[data-panel-link]'));
  if (!panels.length) return;

  var STORAGE_KEY = 'baconbbs.settings.panel';

  function idFor(hash) {
    var id = (hash || '').replace(/^#/, '');
    return panels.some(function (p) { return p.id === id; }) ? id : null;
  }

  function show(id, opts) {
    opts = opts || {};
    panels.forEach(function (p) { p.classList.toggle('is-active', p.id === id); });
    links.forEach(function (a) {
      var on = a.getAttribute('data-panel-link') === id;
      a.classList.toggle('active', on);
      // aria-current, not aria-selected: these stay real links to real
      // anchors so they keep working (and keep being copyable) without JS.
      if (on) { a.setAttribute('aria-current', 'true'); }
      else { a.removeAttribute('aria-current'); }
    });
    try { sessionStorage.setItem(STORAGE_KEY, id); } catch (e) { /* private mode */ }
    if (opts.scroll) { window.scrollTo({ top: 0, behavior: 'smooth' }); }
  }

  function remembered() {
    try { return idFor('#' + sessionStorage.getItem(STORAGE_KEY)); } catch (e) { return null; }
  }

  layout.classList.add('is-tabbed');
  show(idFor(location.hash) || remembered() || panels[0].id);

  links.forEach(function (a) {
    a.addEventListener('click', function (ev) {
      var id = a.getAttribute('data-panel-link');
      if (!idFor('#' + id)) return;
      ev.preventDefault();
      // replaceState, not the default jump: setting location.hash would
      // scroll to a panel that is still display:none at that moment.
      history.replaceState(null, '', '#' + id);
      show(id, { scroll: true });
    });
  });

  // Back/forward, and anything else that edits the hash.
  window.addEventListener('hashchange', function () {
    var id = idFor(location.hash);
    if (id) show(id);
  });
})();
