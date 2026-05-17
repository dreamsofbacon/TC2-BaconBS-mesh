/* ================================================================
   toast.js — Toast notification manager
   Reads flash messages rendered by server into #server-flashes,
   then displays them as toasts and auto-dismisses.
   ================================================================ */
(function () {
  'use strict';

  var DURATION = 4000;

  function createToast(message, category) {
    var region = document.getElementById('toast-region');
    if (!region) return;

    var toast = document.createElement('div');
    toast.className = 'toast toast-' + (category || 'info');
    toast.setAttribute('role', 'status');
    toast.setAttribute('aria-live', 'polite');

    var body = document.createElement('div');
    body.className = 'toast-body';
    body.textContent = message;

    var close = document.createElement('button');
    close.className = 'toast-close';
    close.setAttribute('aria-label', 'Dismiss');
    close.innerHTML = '<svg viewBox="0 0 16 16" fill="currentColor"><path d="M3.72 3.72a.75.75 0 0 1 1.06 0L8 6.94l3.22-3.22a.75.75 0 1 1 1.06 1.06L9.06 8l3.22 3.22a.75.75 0 1 1-1.06 1.06L8 9.06l-3.22 3.22a.75.75 0 0 1-1.06-1.06L6.94 8 3.72 4.78a.75.75 0 0 1 0-1.06z"/></svg>';

    toast.appendChild(body);
    toast.appendChild(close);
    region.appendChild(toast);

    function dismiss() {
      toast.classList.add('toast-out');
      toast.addEventListener('animationend', function() { toast.remove(); }, { once: true });
      setTimeout(function() { toast.remove(); }, 400);
    }

    close.addEventListener('click', dismiss);
    setTimeout(dismiss, DURATION);
    return toast;
  }

  function liftServerFlashes() {
    var container = document.getElementById('server-flashes');
    if (!container) return;

    container.querySelectorAll('[data-flash]').forEach(function(el) {
      var msg = el.dataset.flash;
      var cat = el.dataset.category || 'info';
      createToast(msg, cat);
    });

    // Keep the original elements hidden in DOM for test assertions
    container.setAttribute('aria-hidden', 'true');
    container.style.display = 'none';
  }

  function init() {
    liftServerFlashes();
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', init);
  } else {
    init();
  }

  // Expose for other scripts
  window.BBS = window.BBS || {};
  window.BBS.toast = createToast;

}());
