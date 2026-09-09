/* Plain-text bulletin preview. Nothing is transmitted until the form is saved. */
(function () {
  'use strict';
  var preview = document.getElementById('bulletin-preview');
  if (!preview) return;
  var form = preview.closest('form');
  var encoder = new TextEncoder();
  function update() {
    var subject = form.elements.subject.value;
    var content = form.elements.content.value;
    document.getElementById('preview-board').textContent = form.elements.board.value;
    document.getElementById('preview-sender').textContent = form.elements.sender_short_name.value || 'Anonymous';
    document.getElementById('preview-subject').textContent = subject || 'Untitled bulletin';
    document.getElementById('preview-content').textContent = content || 'Your bulletin will appear here as you type.';
    document.getElementById('preview-size').textContent =
      encoder.encode(subject).length + ' subject bytes · ' + encoder.encode(content).length + ' body bytes';
  }
  form.addEventListener('input', update);
  form.addEventListener('change', update);
  form.addEventListener('reset', function () { setTimeout(update, 0); });
  update();
}());
