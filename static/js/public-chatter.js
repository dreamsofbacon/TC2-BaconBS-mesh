(function () {
  "use strict";

  var feed = document.getElementById("chatter-feed");
  var state = document.getElementById("chatter-state");
  var more = document.getElementById("chatter-more");
  var hours = document.getElementById("chatter-hours");
  var network = document.getElementById("chatter-network");
  var channel = document.getElementById("chatter-channel");
  var search = document.getElementById("chatter-search");
  var cursor = null;
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

  function params(loadMore) {
    var result = new URLSearchParams({
      hours: String(Math.max(1, Math.min(168, Number(hours.value) || 24))),
      limit: "50"
    });
    if (network.value) result.set("network", network.value);
    if (channel.value) result.set("channel", channel.value);
    if (search.value.trim()) result.set("q", search.value.trim());
    if (loadMore && cursor) {
      result.set("before_time", cursor.before_time);
      result.set("before_id", cursor.before_id);
    }
    return result;
  }

  async function load(loadMore) {
    state.hidden = false;
    state.textContent = "Loading messages...";
    more.hidden = true;
    try {
      var response = await fetch("/api/public/chatter?" + params(loadMore));
      if (!response.ok) throw new Error("Request failed");
      var data = await response.json();
      if (!loadMore) feed.replaceChildren();
      data.entries.forEach(renderEntry);
      cursor = data.next_cursor;
      state.hidden = feed.children.length > 0;
      state.textContent = "No public messages in this time window.";
      more.hidden = !data.has_more;
    } catch (error) {
      state.hidden = false;
      state.textContent = "Public chatter is temporarily unavailable.";
    }
  }

  document.querySelectorAll("[data-hours]").forEach(function (button) {
    button.addEventListener("click", function () {
      hours.value = button.dataset.hours;
      load(false);
    });
  });
  [hours, network, channel].forEach(function (control) {
    control.addEventListener("change", function () { load(false); });
  });
  search.addEventListener("input", function () {
    window.clearTimeout(searchTimer);
    searchTimer = window.setTimeout(function () { load(false); }, 300);
  });
  more.addEventListener("click", function () { load(true); });
  load(false);
  window.setInterval(function () { load(false); }, 30000);
}());