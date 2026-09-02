(function () {
  "use strict";

  var feed = document.getElementById("chatter-feed");
  var state = document.getElementById("chatter-state");
  var hours = document.getElementById("chatter-hours");
  var network = document.getElementById("chatter-network");
  var channel = document.getElementById("chatter-channel");
  var search = document.getElementById("chatter-search");
  var legend = document.getElementById("chatter-legend");
  var legendChannels = document.getElementById("legend-channels");
  var legendNodes = document.getElementById("legend-nodes");
  var searchTimer = null;

  var PALETTE_SIZE = 10;

  function appendText(parent, tag, text, className) {
    var element = document.createElement(tag);
    if (className) element.className = className;
    element.textContent = text;
    parent.appendChild(element);
  }

  // djb2. Stable across reloads and across nodes, so a capture node keeps
  // the same colour every time you open the page -- which is the whole point
  // of colouring by identity rather than by arrival order.
  function paletteIndex(text) {
    var hash = 5381;
    for (var i = 0; i < text.length; i++) {
      hash = ((hash << 5) + hash + text.charCodeAt(i)) >>> 0;
    }
    return hash % PALETTE_SIZE;
  }

  // Hash alone gives a stable colour, but with only a handful of nodes in a
  // fleet and ten buckets, two of them collide about half the time -- which
  // defeats the entire point. So hash first, then probe forward over the set
  // that is actually present. Still deterministic: the same fleet produces
  // the same assignment on every reload, on every node, with nothing stored.
  // The set only reshuffles if a node joins or leaves, which is rare and
  // visible.
  function assignColours(keys) {
    var taken = Object.create(null);
    var map = Object.create(null);
    keys.slice().sort().forEach(function (key) {
      var index = paletteIndex(key);
      for (var n = 0; n < PALETTE_SIZE && taken[index]; n++) {
        index = (index + 1) % PALETTE_SIZE;
      }
      taken[index] = true;
      map[key] = index;
    });
    return map;
  }

  // Both dimensions of what is currently on screen, coloured together.
  function buildPalette(entries) {
    var nodes = Object.create(null);
    var channels = Object.create(null);
    entries.forEach(function (entry) {
      var nk = captureKey(entry);
      if (nk !== null) nodes[nk] = true;
      channels[channelKey(entry)] = true;
    });
    return {
      nodes: assignColours(Object.keys(nodes)),
      channels: assignColours(Object.keys(channels))
    };
  }

  // Which BBS node heard this. That is the useful "where did this come
  // from" for a fleet where several nodes feed the same table over MQTT --
  // not who sent the message, which is a different question the sender
  // fields already answer in text.
  function captureKey(entry) {
    return entry.capture_node_id || null;
  }

  // Capture ids are node keys: a MeshCore one is 64 hex characters and
  // swamps the row. Head and tail are what a person actually matches on,
  // and the full value stays in the title attribute.
  function shortNodeId(id) {
    var text = String(id || "");
    return text.length > 20 ? text.slice(0, 10) + "…" + text.slice(-6) : text;
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

  function swatch(className, round, title) {
    var element = document.createElement("span");
    element.className = "chatter-swatch " + (round ? "is-round " : "") + className;
    element.setAttribute("aria-hidden", "true");
    if (title) element.title = title;
    return element;
  }

  function renderEntry(entry, palette) {
    var article = document.createElement("article");
    // The stripe carries the capture node: which of your nodes heard this.
    var capture = captureKey(entry);
    article.className = "chatter-entry "
      + (capture === null ? "cc-none" : "cc-" + palette.nodes[capture]);

    var meta = document.createElement("div");
    meta.className = "chatter-meta";

    // The long name is what a person recognises; sender_name holds only the
    // short name, which for most nodes is just the hex tail of the id.
    var name = senderLabel(entry);
    appendText(meta, "strong", name);

    // Keep the short name and id visible when they add something the long
    // name does not, so a station stays identifiable across renames.
    if (entry.sender_name && entry.sender_name !== name) {
      appendText(meta, "span", entry.sender_name);
    }
    if (entry.sender_node_id && entry.sender_node_id !== name) {
      appendText(meta, "span", entry.sender_node_id);
    }

    var channelWrap = document.createElement("span");
    channelWrap.className = "legend-item";
    channelWrap.appendChild(
      swatch("cc-" + palette.channels[channelKey(entry)], false));
    appendText(channelWrap, "span", channelLabel(entry));
    meta.appendChild(channelWrap);

    // 0 is a real answer ("heard direct"), null means the packet carried no
    // usable hop data -- so test for null rather than falsiness.
    if (entry.hops !== null && entry.hops !== undefined) {
      appendText(meta, "span", entry.hops === 0
        ? "direct" : entry.hops + (entry.hops === 1 ? " hop" : " hops"));
    }
    appendText(meta, "time", new Date(entry.message_timestamp).toLocaleString());

    if (capture !== null) {
      var captureWrap = document.createElement("span");
      captureWrap.className = "legend-item chatter-node";
      captureWrap.appendChild(
        swatch("cc-" + palette.nodes[capture], true, capture));
      var label = document.createElement("span");
      label.textContent = "Heard by " + shortNodeId(capture);
      label.title = capture;
      captureWrap.appendChild(label);
      meta.appendChild(captureWrap);
    }

    article.appendChild(meta);
    appendText(article, "div", entry.content, "chatter-message");
    feed.appendChild(article);
  }

  function addLegendItem(container, label, index, round, count, title) {
    var item = document.createElement("span");
    item.className = "legend-item";
    var mark = document.createElement("span");
    mark.className = "legend-swatch" + (round ? " is-round" : "")
      + " " + (index === null ? "cc-none" : "cc-" + index);
    item.appendChild(mark);
    var text = document.createElement("span");
    text.textContent = label;
    if (title) text.title = title;
    item.appendChild(text);
    appendText(item, "span", "(" + count + ")", "legend-count");
    container.appendChild(item);
  }

  // Built from what is actually on screen. A legend of every channel that
  // ever existed would be noise; this one answers "what am I looking at".
  function renderLegend(entries, palette) {
    legendChannels.replaceChildren();
    legendNodes.replaceChildren();

    // Null-prototype: keys here are node ids and channel names straight off
    // the air, and one calling itself "__proto__" or "constructor" would
    // otherwise collide with Object.prototype instead of getting its own row.
    var channels = Object.create(null);
    var nodes = Object.create(null);
    // Counted on its own rather than under a sentinel key, so no string can
    // ever collide with it.
    var unattributed = 0;

    entries.forEach(function (entry) {
      var ck = channelKey(entry);
      if (!channels[ck]) channels[ck] = { label: channelLabel(entry), count: 0 };
      channels[ck].count++;

      var nk = captureKey(entry);
      if (nk === null) {
        unattributed++;
        return;
      }
      if (!nodes[nk]) {
        nodes[nk] = { label: shortNodeId(nk), full: nk, count: 0 };
      }
      nodes[nk].count++;
    });

    Object.keys(channels).sort().forEach(function (key) {
      addLegendItem(legendChannels, channels[key].label,
        palette.channels[key], false, channels[key].count);
    });

    // Busiest first: the node hearing most of the traffic is the one you
    // usually want to pick out.
    var ranked = Object.keys(nodes).map(function (key) {
      return nodes[key];
    }).sort(function (a, b) {
      return b.count - a.count || a.label.localeCompare(b.label);
    });
    ranked.forEach(function (node) {
      addLegendItem(legendNodes, node.label, palette.nodes[node.full], true,
        node.count, node.full);
    });
    // Rows that recorded no capture node. Neutral, and counted rather than
    // named, because they are not one node.
    if (unattributed) {
      addLegendItem(legendNodes, "Not recorded", null, true, unattributed);
    }

    if (!Object.keys(channels).length) {
      appendText(legendChannels, "span", "None in view", "legend-empty");
    }
    if (!ranked.length && !unattributed) {
      appendText(legendNodes, "span", "None in view", "legend-empty");
    }
    legend.hidden = entries.length === 0;
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
      var palette = buildPalette(data.entries);
      data.entries.forEach(function (entry) { renderEntry(entry, palette); });
      renderLegend(data.entries, palette);
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
  load();
  window.setInterval(function () { load(); }, 30000);
}());
