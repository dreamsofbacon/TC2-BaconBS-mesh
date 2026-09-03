(function () {
  "use strict";

  var generateButton = document.getElementById("fleet-key-generate");
  var confirmInput = document.getElementById("fleet-key-confirm");
  var status = document.getElementById("fleet-key-status");
  var result = document.getElementById("fleet-key-result");
  var publicEntry = document.getElementById("fleet-generated-public");
  var copyButton = document.getElementById("fleet-key-copy");
  var httpWarning = document.getElementById("fleet-key-http-warning");

  if (!generateButton) return;

  var browserCanGenerate = window.isSecureContext
    && window.crypto && window.crypto.subtle;
  if (!browserCanGenerate) httpWarning.hidden = false;

  function bytesToBase64(bytes) {
    var binary = "";
    var chunkSize = 0x8000;
    for (var offset = 0; offset < bytes.length; offset += chunkSize) {
      binary += String.fromCharCode.apply(
        null, bytes.subarray(offset, offset + chunkSize));
    }
    return window.btoa(binary);
  }

  function bytesToBase64Url(bytes) {
    return bytesToBase64(bytes)
      .replace(/\+/g, "-")
      .replace(/\//g, "_")
      .replace(/=+$/, "");
  }

  function privateKeyPem(privateBytes) {
    var encoded = bytesToBase64(privateBytes);
    var lines = encoded.match(/.{1,64}/g) || [];
    return "-----BEGIN PRIVATE KEY-----\n"
      + lines.join("\n")
      + "\n-----END PRIVATE KEY-----\n";
  }

  function downloadPrivateKey(privatePem) {
    var blob = new Blob([privatePem], {
      type: "application/x-pem-file"
    });
    var link = document.createElement("a");
    link.href = URL.createObjectURL(blob);
    link.download = "fleet-key";
    document.body.appendChild(link);
    link.click();
    link.remove();
    window.setTimeout(function () { URL.revokeObjectURL(link.href); }, 1000);
  }

  async function postKey(path, body) {
    var csrf = document.querySelector('meta[name="csrf-token"]').content;
    var response = await window.fetch(path, {
      method: "POST",
      credentials: "same-origin",
      headers: {
        "Content-Type": "application/json",
        "X-CSRF-Token": csrf
      },
      body: JSON.stringify(body)
    });
    var payload = await response.json();
    if (!response.ok || !payload.ok) {
      throw new Error(payload.error || "The node did not accept the public key.");
    }
    return payload;
  }

  async function generateKey() {
    if (!confirmInput.checked) {
      status.className = "text-danger";
      status.textContent = "Confirm that you understand the backup requirement first.";
      return;
    }
    generateButton.disabled = true;
    status.className = "text-muted";
    status.textContent = "Generating Ed25519 key...";
    try {
      var payload;
      if (browserCanGenerate) {
        var keyPair = await window.crypto.subtle.generateKey(
          { name: "Ed25519" }, true, ["sign", "verify"]);
        var privateBytes = new Uint8Array(
          await window.crypto.subtle.exportKey("pkcs8", keyPair.privateKey));
        var publicBytes = new Uint8Array(
          await window.crypto.subtle.exportKey("raw", keyPair.publicKey));
        downloadPrivateKey(privateKeyPem(privateBytes));
        payload = await postKey("/api/fleet/keys/generated", {
          public_key: bytesToBase64Url(publicBytes),
          confirm: true
        });
      } else {
        payload = await postKey("/api/fleet/keys/create", { confirm: true });
        downloadPrivateKey(payload.private_key);
        delete payload.private_key;
      }

      publicEntry.value = payload.public_entry;
      result.hidden = false;
      status.className = "text-success";
      status.textContent = "Key " + payload.key_id
        + " is trusted on this node. Keep the downloaded private key safe.";
    } catch (error) {
      status.className = "text-danger";
      status.textContent = "Key setup failed: " + error.message;
    } finally {
      generateButton.disabled = false;
    }
  }

  async function copyPublicKey() {
    try {
      await navigator.clipboard.writeText(publicEntry.value);
      copyButton.textContent = "Copied";
    } catch (_error) {
      publicEntry.select();
      document.execCommand("copy");
      copyButton.textContent = "Copied";
    }
  }

  generateButton.addEventListener("click", generateKey);
  copyButton.addEventListener("click", copyPublicKey);
}());