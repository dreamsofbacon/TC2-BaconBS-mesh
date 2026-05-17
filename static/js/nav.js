/* ================================================================
   nav.js — Hamburger / mobile drawer navigation
   ================================================================ */
(function () {
  'use strict';

  function initNav() {
    var btn      = document.getElementById('hamburger-btn');
    var drawer   = document.getElementById('nav-drawer');
    var backdrop = document.getElementById('drawer-backdrop');
    var closeBtn = document.getElementById('drawer-close');

    if (!btn || !drawer || !backdrop) return;

    var focusableSelector = 'a, button, input, select, textarea, [tabindex]:not([tabindex="-1"])';

    function openDrawer() {
      drawer.classList.add('open');
      backdrop.classList.add('open');
      document.body.style.overflow = 'hidden';
      drawer.setAttribute('aria-hidden', 'false');
      btn.setAttribute('aria-expanded', 'true');
      // Focus first focusable element
      var first = drawer.querySelector(focusableSelector);
      if (first) setTimeout(function() { first.focus(); }, 50);
    }

    function closeDrawer() {
      drawer.classList.remove('open');
      backdrop.classList.remove('open');
      document.body.style.overflow = '';
      drawer.setAttribute('aria-hidden', 'true');
      btn.setAttribute('aria-expanded', 'false');
      btn.focus();
    }

    btn.addEventListener('click', openDrawer);
    backdrop.addEventListener('click', closeDrawer);
    if (closeBtn) closeBtn.addEventListener('click', closeDrawer);

    document.addEventListener('keydown', function(e) {
      if (!drawer.classList.contains('open')) return;
      if (e.key === 'Escape') {
        closeDrawer();
        return;
      }
      // Trap focus
      if (e.key === 'Tab') {
        var focusable = Array.from(drawer.querySelectorAll(focusableSelector))
          .filter(function(el) { return !el.disabled && el.offsetParent !== null; });
        if (!focusable.length) return;
        var first = focusable[0];
        var last  = focusable[focusable.length - 1];
        if (e.shiftKey && document.activeElement === first) {
          e.preventDefault();
          last.focus();
        } else if (!e.shiftKey && document.activeElement === last) {
          e.preventDefault();
          first.focus();
        }
      }
    });

    // Mark active link
    var path = window.location.pathname;
    drawer.querySelectorAll('.drawer-link').forEach(function(link) {
      if (link.getAttribute('href') && path.startsWith(link.getAttribute('href').split('?')[0])) {
        link.classList.add('active');
      }
    });
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', initNav);
  } else {
    initNav();
  }

}());
