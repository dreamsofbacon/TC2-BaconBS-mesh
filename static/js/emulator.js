(function () {
  "use strict";

  var idle = document.getElementById("emu-idle");
  var live = document.getElementById("emu-live");
  var consoleCard = document.getElementById("emu-console-card");
  var transcript = document.getElementById("emu-transcript");
  var status = document.getElementById("emu-status");
  var input = document.getElementById("emu-input");
  var form = document.getElementById("emu-form");

  var identity = document.getElementById("emu-identity");
  var nameField = document.getElementById("emu-name");
  var bytesField = document.getElementById("emu-bytes");

  var token = null;
  var pollTimer = null;

  function csrf() {
    var meta = document.querySelector('meta[name="csrf-token"]');
    return meta ? meta.getAttribute("content") : "";
  }

  function post(path, body) {
    return fetch(path, {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        "X-CSRF-Token": csrf()
      },
      body: JSON.stringify(body || {})
    }).then(function (response) { return response.json(); });
  }

  function say(message, isError) {
    status.textContent = message || "";
    status.className = isError ? "text-danger" : "text-muted";
  }

  // Each packet gets its own block. Seeing a menu arrive as three of them is
  // the thing this page exists to show, so they are never joined back up.
  function renderChunk(chunk) {
    var block = document.createElement("div");
    block.className = "emu-chunk emu-chunk-in";

    var meta = document.createElement("div");
    meta.className = "emu-chunk-meta";
    meta.textContent = "packet " + chunk.seq + " \u00b7 " + chunk.bytes + " bytes";
    block.appendChild(meta);

    var body = document.createElement("pre");
    body.className = "emu-chunk-body";
    body.textContent = chunk.text;
    block.appendChild(body);

    transcript.appendChild(block);
  }

  function renderSent(text) {
    var block = document.createElement("div");
    block.className = "emu-chunk emu-chunk-out";
    var body = document.createElement("pre");
    body.className = "emu-chunk-body";
    body.textContent = text;
    block.appendChild(body);
    transcript.appendChild(block);
  }

  function renderNote(text, isError) {
    var note = document.createElement("div");
    note.className = "emu-note" + (isError ? " emu-note-error" : "");
    note.textContent = text;
    transcript.appendChild(note);
  }

  function scrollDown() {
    transcript.scrollTop = transcript.scrollHeight;
  }

  function applySession(session) {
    if (!session) return;
    token = session.token;
    document.getElementById("emu-who").textContent =
      session.label + (session.acting_as_real ? " (real node)" : " (test user)");
    document.getElementById("emu-node").textContent = session.node_id;
    document.getElementById("emu-limit").textContent = session.max_text_bytes;
    // Shows why N means north inside a game and a menu item outside one.
    document.getElementById("emu-menu").textContent = session.menu.command
      ? session.menu.command + " step " + session.menu.step
      : "main menu";
    document.getElementById("emu-actas").hidden = !session.acting_as_real;
  }

  function showChunks(data) {
    (data.chunks || []).forEach(renderChunk);
    if (data.error) renderNote(data.error, true);
    applySession(data.session);
    if ((data.chunks || []).length || data.error) scrollDown();
  }

  // Ask Nomad answers from a worker thread well after the question returned,
  // so without this the page would show the slow ack and then nothing.
  function poll() {
    if (!token) return;
    fetch("/api/emulator/poll?token=" + encodeURIComponent(token))
      .then(function (response) { return response.json(); })
      .then(function (data) {
        if (!data.ok) {
          if (data.error) say(data.error, true);
          stopSession(true);
          return;
        }
        showChunks(data);
      })
      .catch(function () { /* transient; the next tick retries */ });
  }

  function startSession() {
    var nodeId = identity.value;
    if (nodeId) {
      var label = identity.options[identity.selectedIndex].textContent.trim();
      if (!window.confirm(
        "Act as " + label + "?\n\n" +
        "Anything you post will be attributed to this node for real, mail " +
        "will genuinely be from them, and starting a game will overwrite " +
        "their saved position. This cannot be undone."
      )) return;
    }
    say("Starting...");
    post("/api/emulator/session", {
      node_id: nodeId,
      confirm_act_as: nodeId ? true : false,
      short_name: nameField.value,
      max_text_bytes: bytesField.value
    }).then(function (data) {
      if (!data.ok) { say(data.error || "Could not start a session.", true); return; }
      applySession(data.session);
      transcript.replaceChildren();
      renderNote("Session started as " + data.session.node_id + ".");
      idle.hidden = true;
      live.hidden = false;
      consoleCard.hidden = false;
      say("");
      input.focus();
      pollTimer = window.setInterval(poll, 2000);
    }).catch(function () { say("Could not reach the server.", true); });
  }

  function stopSession(expired) {
    if (pollTimer) { window.clearInterval(pollTimer); pollTimer = null; }
    var ending = token;
    token = null;
    idle.hidden = false;
    live.hidden = true;
    if (expired) renderNote("Session ended.", true);
    if (ending && !expired) post("/api/emulator/end", { token: ending });
  }

  function send(text) {
    if (!token || !text.trim()) return;
    renderSent(text);
    scrollDown();
    say("Sending...");
    post("/api/emulator/send", { token: token, text: text })
      .then(function (data) {
        if (!data.ok) {
          say(data.error || "Send failed.", true);
          if (data.error) stopSession(true);
          return;
        }
        say("");
        showChunks(data);
      })
      .catch(function () { say("Could not reach the server.", true); });
  }

  document.getElementById("emu-start").addEventListener("click", startSession);
  document.getElementById("emu-end").addEventListener("click", function () {
    stopSession(false);
    renderNote("Session ended.");
  });
  document.getElementById("emu-reset").addEventListener("click", function () {
    if (!token) return;
    post("/api/emulator/reset", { token: token }).then(function (data) {
      if (!data.ok) { say(data.error || "Reset failed.", true); return; }
      applySession(data.session);
      renderNote("Menu state cleared. Still acting as " + data.session.node_id + ".");
      scrollDown();
    });
  });

  form.addEventListener("submit", function (event) {
    event.preventDefault();
    var text = input.value;
    input.value = "";
    send(text);
  });

  Array.prototype.forEach.call(
    document.querySelectorAll(".emu-quick-btn"),
    function (button) {
      button.addEventListener("click", function () {
        send(button.getAttribute("data-send"));
      });
    }
  );
})();
