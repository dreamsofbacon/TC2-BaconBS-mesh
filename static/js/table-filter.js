/* ================================================================
   table-filter.js — Instant client-side table filtering

   Declarative: a container marked [data-table-filter="<table id>"] holds
   the controls, and each row carries the values as data- attributes. No
   per-page JS, so another table opts in with markup alone.

   Controls:
     [data-filter-search]        free text over the row's data-search
     [data-filter-field="link"]  exact match against the row's data-link
     [data-filter-max-age]       row's data-age-seconds <= the value

   Rows are hidden with a class rather than an inline style: on narrow
   screens app.css restyles table rows to display:block for the card
   stack, and clearing an inline display would fight that.
   ================================================================ */
(function () {
  'use strict';

  function initFilter(panel) {
    var table = document.getElementById(panel.getAttribute('data-table-filter'));
    if (!table) return;

    var rows = Array.prototype.slice.call(
      table.querySelectorAll('tbody tr[data-search]'));
    var emptyRow = table.querySelector('tbody tr[data-filter-empty]');
    var counter = document.querySelector(
      '[data-filter-count="' + panel.getAttribute('data-table-filter') + '"]');
    var controls = Array.prototype.slice.call(panel.querySelectorAll(
      '[data-filter-search],[data-filter-field],[data-filter-max-age]'));
    var clearBtn = panel.querySelector('[data-filter-clear]');
    if (!rows.length && !emptyRow) return;

    function matches(row) {
      for (var i = 0; i < controls.length; i++) {
        var control = controls[i];
        var value = (control.value || '').trim();
        if (!value) continue;

        if (control.hasAttribute('data-filter-search')) {
          var haystack = (row.getAttribute('data-search') || '').toLowerCase();
          // Every term must appear, so "aur solar" narrows rather than widens.
          var terms = value.toLowerCase().split(/\s+/);
          for (var t = 0; t < terms.length; t++) {
            if (haystack.indexOf(terms[t]) === -1) return false;
          }
        } else if (control.hasAttribute('data-filter-max-age')) {
          var age = parseInt(row.getAttribute('data-age-seconds'), 10);
          // A row with no usable timestamp is kept: dropping it would
          // hide a real device because of a missing field.
          if (!isNaN(age) && age > parseInt(value, 10)) return false;
        } else {
          var field = control.getAttribute('data-filter-field');
          if ((row.getAttribute('data-' + field) || '') !== value) return false;
        }
      }
      return true;
    }

    function apply() {
      var shown = 0;
      rows.forEach(function (row) {
        var ok = matches(row);
        row.classList.toggle('is-filtered-out', !ok);
        if (ok) shown++;
      });
      if (emptyRow) emptyRow.classList.toggle('is-filtered-out', shown > 0 || !rows.length);
      if (counter) {
        counter.textContent = shown === rows.length
          ? String(rows.length) + ' device(s).'
          : 'Showing ' + shown + ' of ' + rows.length + ' device(s).';
      }
      if (clearBtn) {
        var active = controls.some(function (c) { return (c.value || '').trim(); });
        clearBtn.hidden = !active;
      }
    }

    controls.forEach(function (control) {
      control.addEventListener('input', apply);
      control.addEventListener('change', apply);
    });
    if (clearBtn) {
      clearBtn.addEventListener('click', function () {
        controls.forEach(function (c) { c.value = ''; });
        apply();
      });
    }
    apply();
  }

  function init() {
    Array.prototype.forEach.call(
      document.querySelectorAll('[data-table-filter]'), initFilter);
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', init);
  } else {
    init();
  }
}());
