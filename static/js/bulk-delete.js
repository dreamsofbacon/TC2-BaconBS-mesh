/* ================================================================
   bulk-delete.js — Tick several rows and delete them together

   The checkboxes live inside the table but belong to a form outside it
   (form="bulk-delete-form"), because each row already holds its own
   single-row Delete form and forms cannot nest.
   ================================================================ */
(function () {
  'use strict';

  var form = document.querySelector('[data-bulk-delete]');
  if (!form) return;

  var noun = form.getAttribute('data-noun') || 'row';
  var submit = form.querySelector('[data-bulk-submit]');
  var count = form.querySelector('[data-bulk-count]');
  var all = document.querySelector('[data-bulk-all]');
  var boxes = Array.prototype.slice.call(document.querySelectorAll('[data-bulk-row]'));
  var idle = count ? count.textContent : '';

  function plural(n) { return n + ' ' + noun + (n === 1 ? '' : 's'); }

  function selected() {
    return boxes.filter(function (box) { return box.checked; });
  }

  function refresh() {
    var n = selected().length;
    submit.disabled = n === 0;
    submit.textContent = n ? 'Delete ' + plural(n) : 'Delete selected';
    if (count) count.textContent = n ? plural(n) + ' selected' : idle;
    if (all) {
      all.checked = n > 0 && n === boxes.length;
      all.indeterminate = n > 0 && n < boxes.length;
    }
  }

  boxes.forEach(function (box) { box.addEventListener('change', refresh); });
  if (all) {
    all.addEventListener('change', function () {
      boxes.forEach(function (box) { box.checked = all.checked; });
      refresh();
    });
  }

  form.addEventListener('submit', function (event) {
    var n = selected().length;
    if (!n || !window.confirm('Delete ' + plural(n) + '? Other nodes delete their copies too. This cannot be undone.')) {
      event.preventDefault();
    }
  });

  refresh();
})();
