/* Reuse the global link poll; avoid a second set of status requests. */
(function () {
  'use strict';
  var region = document.getElementById('overview-links');
  if (!region) return;
  document.addEventListener('bbs:links', function (event) {
    var links = event.detail;
    var connected = links.filter(function (link) { return link.connected && !link.reconnecting; }).length;
    document.getElementById('overview-link-count').textContent = connected + ' / ' + links.length;
    document.getElementById('overview-link-summary').textContent = links.length ? 'Local radio and service links' : 'No configured links';
    region.replaceChildren();
    links.forEach(function (link) {
      var row = document.createElement('div');
      row.className = 'health-row';
      var info = document.createElement('div');
      var name = document.createElement('strong');
      name.textContent = link.name;
      var protocol = document.createElement('small');
      protocol.textContent = link.protocol;
      info.append(name, protocol);
      var badge = document.createElement('span');
      var state = link.reconnecting ? 'reconnecting' : (link.connected ? 'connected' : 'disconnected');
      badge.className = 'health-state ' + state;
      badge.textContent = state.charAt(0).toUpperCase() + state.slice(1);
      row.append(info, badge);
      region.appendChild(row);
    });
    if (!links.length) region.textContent = 'No local links reported. Check connection settings to get started.';
    document.getElementById('overview-updated').textContent = 'Updated ' + new Date().toLocaleTimeString();
  });
  document.addEventListener('bbs:links-unavailable', function () {
    document.getElementById('overview-link-count').textContent = '—';
    document.getElementById('overview-link-summary').textContent = 'Status unavailable';
    document.getElementById('overview-updated').textContent = 'Status unavailable. Retrying in 5 seconds; any displayed details may be out of date.';
  });
}());
