(function () {
  "use strict";

  var shell = document.querySelector(".chatter-shell");
  var feed = document.getElementById("chatter-feed");
  var state = document.getElementById("chatter-state");
  var hours = document.getElementById("chatter-hours");
  var network = document.getElementById("chatter-network");
  var channel = document.getElementById("chatter-channel");
  var search = document.getElementById("chatter-search");
  var colourToggle = document.getElementById("chatter-colour");
  var legend = document.getElementById("chatter-legend");
  var legendChannels = document.getElementById("legend-channels");
  var legendSenders = document.getElementById("legend-senders");
  var searchTimer = null;

  var PALETTE_SIZE = 10;
  var COLOUR_KEY = "bbs_chatter_colour";

  function appendText(parent, tag, text, className) {
    var element = document.createElement(tag);
    if (className) element.className = className;
    element.textContent = text;
    parent.appendChild(element);
  }

  // djb2. Stable across reloads and across nodes, so a station keeps the
  // same colour every time you open the page -- which is the whole point of
  // colouring by identity rather than by arrival order.
  function paletteIndex(text) {
    var hash = 5381;
    for (var i = 0; i < text.length; i++) {
      hash = ((hash << 5) + hash + text.charCodeAt(i)) >>> 0;
    }
    return hash % PALETTE_SIZE;
  }

  // Meshtastic carries a node id. MeshCore channel messages carry no sender
  // identity at all, so the name parsed from the body prefix is all there
  // is. A message with neither returns null and gets NO colour: painting
  // every anonymous sender the same would imply they are one station, which
  // is worse than saying nothing.
  function senderKey(entry) {
    return entry.sender_node_id || entry.sender_name || null;
  }

  function senderLabel(entry) {
    return entry.sender_long_name || entry.sender_name || entry.sender_node_id
      || "Unknown sender";
  }

  function channelKey(entry) {
    // Network-qualified: meshcore channel 2 and meshtastic channel 2 are
    // unrelated and must never share a colour.
    return (entry.network || "unknown") + "/" + entry.channel_index;
  }

  function channelLabel(entry) {
    return (entry.network || "Unknown") + " / "
      + (entry.channel_name || "Channel " + entry.channel_index);
  }

  function renderEntry(entry) {
    var article = document.createElement("article");
    // The entry carries the CHANNEL colour; the sender elements below carry
    // their own cc-N, which shadows it for their subtree.
    article.className = "chatter-entry cc-" + paletteIndex(channelKey(entry));

    var meta = document.createElement("div");
    meta.className = "chatter-meta";

    var key = senderKey(entry);
    var senderClass = key === null ? "cc-none" : "cc-" + paletteIndex(key);

    var dot = document.createElement("span");
    dot.className = "chatter-dot " + senderClass;
    dot.setAttribute("aria-hidden", "true");
    meta.appendChild(dot);

    // The long name is what a person recognises; sender_name holds only the
    // short name, which for most nodes is just the hex tail of the id.
    var name = senderLabel(entry);
    var strong = document.createElement("strong");
    strong.className = "chatter-sender " + senderClass;
    strong.textContent = name;
    meta.appendChild(strong);

    // Keep the short name and id visible when they add something the long
    // name does not, so a station stays identifiable across renames.
    if (entry.sender_name && entry.sender_name !== name) {
      appendText(meta, "span", entry.sender_name);
    }
    if (entry.sender_node_id && entry.sender_node_id !== name) {
      appendText(meta, "span", entry.sender_node_id);
    }
    appendText(meta, "span", channelLabel(entry));
    // 0 is a real answer ("heard direct"), null means the packet carried no
    // usable hop data -- so test for null rather than falsiness.
    if (entry.hops !== null && entry.hops !== undefined) {
      appendText(meta, "span", entry.hops === 0
        ? "direct" : entry.hops + (entry.hops === 1 ? " hop" : " hops"));
    }
    appendText(meta, "time", new Date(entry.message_timestamp).toLocaleString());
    if (entry.capture_node_id) appendText(meta, "span", "Heard by " + entry.capture_node_id);
    article.appendChild(meta);
    appendText(article, "div", entry.content, "chatter-message");
    feed.appendChild(article);
  }

  function addLegendItem(container, label, index, round, count) {
    var item = document.createElement("span");
    item.className = "legend-item";
    var swatch = document.createElement("span");
    swatch.className = "legend-swatch" + (round ? " is-round" : "")
      + " " + (index === null ? "cc-none" : "cc-" + index);
    item.appendChild(swatch);
    appendText(item, "span", label);
    appendText(item, "span", "(" + count + ")", "legend-count");
    container.appendChild(item);
  }

  // Built from what is actually on screen. A legend of every channel that
  // ever existed would be noise; this one answers "what am I looking at".
  function renderLegend(entries) {
    legendChannels.replaceChildren();
    legendSenders.replaceChildren();

    // Null-prototype: keys here are sender names and node ids straight off
    // the air, and a station calling itself "__proto__" or "constructor"
    // would otherwise collide with Object.prototype instead of getting its
    // own row.
    var channels = Object.create(null);
    var senders = Object.create(null);
    // Counted on its own rather than under a sentinel key, so no string can
    // ever collide with it.
    var unknown = 0;

    entries.forEach(function (entry) {
      var ck = channelKey(entry);
      if (!channels[ck]) channels[ck] = { label: channelLabel(entry), count: 0 };
      channels[ck].count++;

      var sk = senderKey(entry);
      if (sk === null) {
        unknown++;
        return;
      }
      if (!senders[sk]) {
        senders[sk] = {
          label: senderLabel(entry),
          index: paletteIndex(sk),
          count: 0
        };
      }
      senders[sk].count++;
    });

    Object.keys(channels).sort().forEach(function (key) {
      addLegendItem(legendChannels, channels[key].label,
        paletteIndex(key), false, channels[key].count);
    });

    // Busiest first: with many stations the tail is a long list of one-offs.
    var ranked = Object.keys(senders).map(function (key) {
      return senders[key];
    }).sort(function (a, b) {
      return b.count - a.count || a.label.localeCompare(b.label);
    });
    // Unaddressable senders go last, under one neutral swatch. They are not
    // one station and must not look like one, so the row says how many
    // messages rather than naming anybody.
    if (unknown) {
      ranked.push({ label: "Unknown sender", index: null, count: unknown });
    }
    ranked.forEach(function (sender) {
      addLegendItem(legendSenders, sender.label, sender.index, true,
        sender.count);
    });

    if (!Object.keys(channels).length) {
      appendText(legendChannels, "span", "None in view", "legend-empty");
    }
    if (!ranked.length) {
      appendText(legendSenders, "span", "None in view", "legend-empty");
    }
    legend.hidden = entries.length === 0;
  }

  function applyColourPreference() {
    var on = colourToggle.checked;
    shell.classList.toggle("no-colour", !on);
    legend.hidden = !on || !feed.children.length;
    try { localStorage.setItem(COLOUR_KEY, on ? "1" : "0"); } catch (e) {}
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
      renderLegend(data.entries);
      if (!colourToggle.checked) legend.hidden = true;
      state.hidden = feed.children.length > 0;
      state.textContent = "No public messages in this time window.";
    } catch (error) {
      state.hidden = false;
      state.textContent = "Public chatter is temporarily unavailable.";
      legend.hidden = true;
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
  colourToggle.addEventListener("change", applyColourPreference);

  var saved = null;
  try { saved = localStorage.getItem(COLOUR_KEY); } catch (e) {}
  if (saved === "0") colourToggle.checked = false;
  applyColourPreference();

  load();
  window.setInterval(function () { load(); }, 30000);
}());
