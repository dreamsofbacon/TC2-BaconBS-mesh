(function () {
  "use strict";

  var feed = document.getElementById("chatter-feed");
  var state = document.getElementById("chatter-state");
  var hours = document.getElementById("chatter-hours");
  var network = document.getElementById("chatter-network");
  var channel = document.getElementById("chatter-channel");
  var search = document.getElementById("chatter-search");
  var searchTimer = null;

  function appendText(parent, tag, text, className) {
    var element = document.createElement(tag);
    if (className) element.className = className;
    element.textContent = text;
    parent.appendChild(element);
  }

  function renderEntry(entry) {
    var article = document.createElement("article");
    article.className = "chatter-entry";
    var meta = document.createElement("div");
    meta.className = "chatter-meta";
    appendText(meta, "strong", entry.sender_name || entry.sender_node_id || "Unknown sender");
    appendText(meta, "span", (entry.network || "Unknown") + " / " + (entry.channel_name || "Channel " + entry.channel_index));
    appendText(meta, "time", new Date(entry.message_timestamp).toLocaleString());
    if (entry.capture_node_id) appendText(meta, "span", "Heard by " + entry.capture_node_id);
    article.appendChild(meta);
    appendText(article, "div", entry.content, "chatter-message");
    feed.appendChild(article);
  }

  function params() {
    var result = new URLSearchParams({
      hours: String(Math.max(1, Math.min(168, Number(hours.value) || 24)))
    });
    if (network.value) result.set("network", network.value);
    if (channel.value) result.set("channel", channel.value);
    if (search.value.trim()) result.set("q", search.value.trim());
    return result;
  }

  async function load() {
    state.hidden = false;
    state.textContent = "Loading messages...";
    try {
      var response = await fetch("/api/public/chatter?" + params());
      if (!response.ok) throw new Error("Request failed");
      var data = await response.json();
      feed.replaceChildren();
      data.entries.forEach(renderEntry);
      state.hidden = feed.children.length > 0;
      state.textContent = "No public messages in this time window.";
    } catch (error) {
      state.hidden = false;
      state.textContent = "Public chatter is temporarily unavailable.";
    }
  }

  document.querySelectorAll("[data-hours]").forEach(function (button) {
    button.addEventListener("click", function () {
      hours.value = button.dataset.hours;
      load();
    });
  });
  [hours, network, channel].forEach(function (control) {
    control.addEventListener("change", function () { load(); });
  });
  search.addEventListener("input", function () {
    window.clearTimeout(searchTimer);
    searchTimer = window.setTimeout(function () { load(); }, 300);
  });
  load();
  window.setInterval(function () { load(); }, 30000);
}());