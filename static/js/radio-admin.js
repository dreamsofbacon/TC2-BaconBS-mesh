(function () {
  'use strict';
  const form = document.getElementById('radio-composer');
  if (!form) return;
  const radioSelect = document.getElementById('send-radio');
  const channelSelect = document.getElementById('send-channel');
  const message = document.getElementById('send-text');
  const button = document.getElementById('send-button');
  const result = document.getElementById('send-result');
  let radios = [], pending = false;
  function el(tag, text) { const node = document.createElement(tag); if (text !== undefined) node.textContent = text; return node; }
  function selectedRadio() { return radios.find(r => r.id === radioSelect.value); }
  function updateLimit() {
    const radio = selectedRadio();
    const size = new TextEncoder().encode(message.value).length;
    document.getElementById('send-limit').textContent = radio ? size + ' / ' + radio.max_bytes + ' UTF-8 bytes' : 'Select a local radio and channel.';
    button.disabled = pending || !radio || radio.state !== 'connected' || !channelSelect.value || !message.value.trim() || size > radio.max_bytes;
  }
  async function api(url, options) {
    const response = await fetch(url, Object.assign({headers: {'Content-Type': 'application/json', 'X-CSRF-Token': document.querySelector('meta[name="csrf-token"]').content}}, options));
    if (response.redirected) throw new Error('Your admin session expired. Sign in again.');
    const data = await response.json();
    if (!response.ok) throw new Error(data.error || 'Request failed.');
    return data;
  }
  function newID() {
    const bytes = crypto.getRandomValues(new Uint8Array(16));
    bytes[6] = (bytes[6] & 15) | 64; bytes[8] = (bytes[8] & 63) | 128;
    const hex = Array.from(bytes, n => n.toString(16).padStart(2, '0')).join('');
    return [hex.slice(0, 8), hex.slice(8, 12), hex.slice(12, 16), hex.slice(16, 20), hex.slice(20)].join('-');
  }
  async function run(payload, output) {
    payload.id = payload.id || newID();
    payload.radio_token = (radios.find(r => r.id === payload.radio) || {}).token;
    try { sessionStorage.setItem('bbs-last-radio-operation', payload.id); } catch (_) {}
    output.textContent = 'Submitting…';
    // Keep the operation id visible so a network failure never invites an
    // automatic second transmission with a new id.
    try {
      let status = await api('/api/radios/operations', {method: 'POST', body: JSON.stringify(payload)});
      for (let attempt = 0; attempt < 46; attempt++) {
        output.textContent = status.status + ': ' + status.detail;
        if (!['queued', 'dispatching'].includes(status.status)) return status;
        await new Promise(resolve => setTimeout(resolve, 2000));
        status = await api('/api/radios/operations/' + payload.id);
      }
      output.textContent = 'Result unknown. Do not repeat this operation until you check the radio. Operation: ' + payload.id;
    } catch (error) {
      output.textContent = error.message + ' Operation: ' + payload.id + '. Check its status before repeating.';
      const check = el('button', 'Check operation status'); check.type = 'button';
      check.onclick = async () => {
        try { const status = await api('/api/radios/operations/' + payload.id); output.textContent = status.status + ': ' + status.detail; }
        catch (err) { output.textContent = err.message; }
      };
      output.append(check);
    }
  }
  function renderPanels() {
    const panels = document.getElementById('radio-panels');
    if (!panels) return;
    panels.replaceChildren();
    document.getElementById('radio-state').textContent = radios.length ? 'Status comes from the running BBS. Refresh to see recent changes.' : 'No local radios configured. MQTT-only operation remains available.';
    const refresh = el('button', 'Refresh status'); refresh.className = 'btn'; refresh.onclick = load;
    panels.append(refresh);
    radios.forEach(radio => {
      const card = el('section'); card.className = 'card'; card.style.marginTop = '16px';
      card.append(el('h2', (radio.network === 'meshcore' ? 'MeshCore' : 'Meshtastic') + ' · ' + radio.id));
      card.append(el('p', 'Status: ' + radio.state + (radio.identity ? ' · ' + radio.identity : '')));
      if (radio.state !== 'connected') card.append(el('p', 'Controls are unavailable until the BBS reports a connected radio.'));
      card.append(el('h3', 'Channels'));
      const list = el('ul');
      radio.channels.forEach(channel => list.append(el('li', channel.label + ' · slot ' + channel.index)));
      if (!radio.channels.length) list.append(el('li', 'No channels reported yet.'));
      card.append(list);
      card.append(el('h3', 'Radio identity'));
      if (radio.capabilities.identity) {
        const identity = el('form'); const label = el('label', 'Display name (up to 24 UTF-8 bytes)');
        const input = el('input'); input.id = 'identity-' + radio.id; label.htmlFor = input.id; input.value = radio.identity; input.required = true;
        const save = el('button', 'Save name'); save.className = 'btn'; save.disabled = radio.state !== 'connected';
        const output = el('p'); output.setAttribute('role', 'status'); identity.append(label, input, save, output);
        identity.onsubmit = async event => { event.preventDefault(); save.disabled = true; await run({radio: radio.id, action: 'identity', name: input.value}, output); save.disabled = radio.state !== 'connected'; };
        card.append(identity);
      } else card.append(el('p', 'Identity editing is not supported by this connection.'));
      const contacts = el('details'); contacts.append(el('summary', (radio.network === 'meshcore' ? 'Contacts' : 'Known nodes') + ' (' + radio.contacts.length + ')'));
      if (!radio.contacts.length) contacts.append(el('p', 'No contacts or nodes reported yet.'));
      radio.contacts.forEach(contact => {
        const row = el('p', contact.name + ' · ' + contact.id);
        row.style.overflowWrap = 'anywhere';
        if (radio.capabilities.remove_contact) {
          const remove = el('button', 'Remove contact'); remove.className = 'btn'; remove.disabled = radio.state !== 'connected';
          remove.onclick = async () => { remove.disabled = true; const output = el('span'); row.append(output); await run({radio: radio.id, action: 'remove_contact', contact: contact.id}, output); };
          row.append(remove);
        }
        contacts.append(row);
      });
      if (!radio.capabilities.remove_contact) contacts.append(el('p', 'Manual contact removal is not supported by this connection.'));
      card.append(contacts);
      if (radio.capabilities.refresh) {
        const refreshRadio = el('button', 'Refresh contacts and channels'); refreshRadio.className = 'btn'; refreshRadio.disabled = radio.state !== 'connected';
        const output = el('p'); output.setAttribute('role', 'status');
        refreshRadio.onclick = async () => { refreshRadio.disabled = true; await run({radio: radio.id, action: 'refresh'}, output); refreshRadio.disabled = false; };
        card.append(refreshRadio, output);
      }
      panels.append(card);
    });
  }
  async function load() {
    try {
      radios = (await api('/api/radios')).radios;
      radioSelect.replaceChildren(new Option('Choose a radio…', ''));
      radios.forEach(radio => { const option = new Option(radio.network + ' · ' + radio.id + ' · ' + radio.state, radio.id); option.disabled = radio.state !== 'connected' || !radio.capabilities.send; radioSelect.add(option); });
      channelSelect.replaceChildren(new Option('Choose a channel…', '')); channelSelect.disabled = true;
      if (!radios.some(r => r.state === 'connected')) result.textContent = 'No connected local send destinations. Check the BBS service and connection settings.';
      renderPanels(); updateLimit();
      try {
        const id = sessionStorage.getItem('bbs-last-radio-operation');
        if (id) { const status = await api('/api/radios/operations/' + id); result.textContent = 'Last operation — ' + status.status + ': ' + status.detail; }
      } catch (_) { /* Status history is optional; do not hide destination state. */ }
    } catch (error) { result.textContent = error.message; const state = document.getElementById('radio-state'); if (state) state.textContent = error.message; }
  }
  radioSelect.onchange = () => {
    channelSelect.replaceChildren(new Option('Choose a channel…', ''));
    const radio = selectedRadio();
    if (radio) radio.channels.forEach(channel => channelSelect.add(new Option(channel.label + ' · slot ' + channel.index, String(channel.index))));
    channelSelect.disabled = !radio || !radio.channels.length; updateLimit();
  };
  channelSelect.onchange = updateLimit; message.oninput = updateLimit;
  form.onsubmit = async event => {
    event.preventDefault(); if (button.disabled) return;
    const radio = selectedRadio(), channel = radio.channels.find(c => String(c.index) === channelSelect.value);
    pending = true; updateLimit();
    const status = await run({action: 'send', radio: radio.id, channel: channel.index, channel_token: channel.token, text: message.value}, result);
    if (status && status.status === 'submitted') message.value = '';
    pending = false; updateLimit();
  };
  load();
}());
