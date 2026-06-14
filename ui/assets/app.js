/* AndroZoo Downloader — frontend logic.
   Reads hash files client-side (drop / select / paste), talks to the Python
   bridge via window.pywebview.api, and renders progress + results accessibly. */

(function () {
  "use strict";

  var $ = function (id) { return document.getElementById(id); };

  var dropzone = $("dropzone");
  var fileInput = $("file-input");
  var hashes = $("hashes");
  var hashCount = $("hash-count");
  var apiKey = $("api-key");
  var outDir = $("out-dir");
  var browse = $("browse");
  var verify = $("verify");
  var force = $("force");
  var concurrency = $("concurrency");
  var startBtn = $("start");
  var cancelBtn = $("cancel");
  var errorEl = $("error");
  var progress = $("progress");
  var progressPct = $("progress-pct");
  var liveEl = $("live");
  var resultsBody = $("results-body");

  var apiReady = false;
  var lastAnnounced = 0;

  function api() {
    return (window.pywebview && window.pywebview.api) || null;
  }

  // ---- hash input handling ------------------------------------------- //

  function refreshCount() {
    var text = hashes.value;
    var lines = text.split("\n");
    var valid = 0, invalid = 0;
    var re = /^[0-9a-fA-F]{64}$/;
    for (var i = 0; i < lines.length; i++) {
      var s = lines[i].trim();
      if (!s || s.charAt(0) === "#") continue;
      if (re.test(s)) valid++; else invalid++;
    }
    if (valid === 0 && invalid === 0) {
      hashCount.textContent = "No hashes yet.";
    } else {
      hashCount.textContent =
        valid + " valid hash" + (valid === 1 ? "" : "es") +
        (invalid ? ", " + invalid + " line" + (invalid === 1 ? "" : "s") + " ignored" : "") + ".";
    }
  }

  function loadFile(file) {
    if (!file) return;
    var reader = new FileReader();
    reader.onload = function (e) {
      var existing = hashes.value.trim();
      hashes.value = (existing ? existing + "\n" : "") + e.target.result;
      refreshCount();
      hashes.focus();
    };
    reader.onerror = function () { showError("Could not read that file."); };
    reader.readAsText(file);
  }

  // Drag + drop (progressive enhancement over the file button)
  ["dragenter", "dragover"].forEach(function (evt) {
    dropzone.addEventListener(evt, function (e) {
      e.preventDefault();
      dropzone.classList.add("is-dragover");
    });
  });
  ["dragleave", "drop"].forEach(function (evt) {
    dropzone.addEventListener(evt, function (e) {
      e.preventDefault();
      dropzone.classList.remove("is-dragover");
    });
  });
  dropzone.addEventListener("drop", function (e) {
    if (e.dataTransfer && e.dataTransfer.files && e.dataTransfer.files.length) {
      loadFile(e.dataTransfer.files[0]);
    }
  });

  // Keyboard + click: the accessible single-pointer alternative to dragging
  dropzone.addEventListener("click", function () { fileInput.click(); });
  dropzone.addEventListener("keydown", function (e) {
    if (e.key === "Enter" || e.key === " " || e.key === "Spacebar") {
      e.preventDefault();
      fileInput.click();
    }
  });
  fileInput.addEventListener("change", function (e) {
    if (e.target.files && e.target.files.length) loadFile(e.target.files[0]);
    fileInput.value = ""; // allow re-selecting the same file
  });

  hashes.addEventListener("input", refreshCount);

  // ---- settings ------------------------------------------------------- //

  browse.addEventListener("click", function () {
    var a = api();
    if (!a) { showError("Folder picker needs the desktop app."); return; }
    Promise.resolve(a.pick_output_dir()).then(function (path) {
      if (path) outDir.value = path;
    });
  });

  // ---- run / cancel --------------------------------------------------- //

  function showError(msg) {
    errorEl.textContent = msg;
    errorEl.hidden = false;
  }
  function clearError() {
    errorEl.hidden = true;
    errorEl.textContent = "";
  }

  function setRunning(running) {
    startBtn.disabled = running;
    cancelBtn.disabled = !running;
    [hashes, apiKey, outDir, browse, verify, force, concurrency, dropzone]
      .forEach(function (el) {
        if ("disabled" in el) el.disabled = running;
      });
  }

  function resetResults() {
    resultsBody.innerHTML = "";
    progress.value = 0;
    progressPct.textContent = "0%";
    liveEl.textContent = "";
    lastAnnounced = 0;
  }

  startBtn.addEventListener("click", function () {
    clearError();
    var a = api();
    if (!a) { showError("The downloader backend is not available."); return; }

    resetResults();
    setRunning(true);

    var opts = {
      hashes_text: hashes.value,
      api_key: apiKey.value,
      out_dir: outDir.value,
      concurrency: parseInt(concurrency.value, 10) || 10,
      no_verify: !verify.checked,
      force: force.checked
    };

    Promise.resolve(a.start_download(opts)).then(function (res) {
      if (!res || !res.ok) {
        setRunning(false);
        showError((res && res.error) || "Could not start the download.");
        return;
      }
      progress.max = res.total;
      liveEl.textContent = "Starting " + res.total + " download" + (res.total === 1 ? "" : "s") + "\u2026";
    });
  });

  cancelBtn.addEventListener("click", function () {
    var a = api();
    if (a) a.cancel();
    liveEl.textContent = "Stopping\u2026";
    cancelBtn.disabled = true;
  });

  // ---- events pushed from Python -------------------------------------- //

  function shortSha(sha) { return sha.slice(0, 12) + "\u2026" + sha.slice(-6); }

  function addRow(data) {
    var tr = document.createElement("tr");

    var tdStatus = document.createElement("td");
    var span = document.createElement("span");
    span.className = "status status-" + data.status;
    span.textContent = data.status.charAt(0).toUpperCase() + data.status.slice(1);
    tdStatus.appendChild(span);

    var tdSha = document.createElement("td");
    tdSha.className = "sha";
    tdSha.textContent = data.sha;

    var tdMsg = document.createElement("td");
    tdMsg.textContent = data.message || (data.status === "ok" ? "Saved" : "");

    tr.appendChild(tdStatus);
    tr.appendChild(tdSha);
    tr.appendChild(tdMsg);
    resultsBody.insertBefore(tr, resultsBody.firstChild); // newest first
  }

  window.appEvent = function (type, data) {
    if (type === "progress") {
      progress.max = data.total;
      progress.value = data.done;
      var pct = Math.round((data.done / data.total) * 100);
      progressPct.textContent = pct + "%";
      addRow(data);

      // Throttle live announcements to ~every 10% to avoid flooding AT.
      if (pct >= lastAnnounced + 10 || data.done === data.total) {
        lastAnnounced = pct - (pct % 10);
        liveEl.textContent = "Downloaded " + data.done + " of " + data.total + " (" + pct + "%).";
      }
    } else if (type === "done") {
      setRunning(false);
      var parts = [data.ok + " downloaded"];
      if (data.skipped) parts.push(data.skipped + " skipped");
      if (data.failed) parts.push(data.failed + " failed");
      liveEl.textContent =
        (data.cancelled ? "Cancelled. " : "Finished. ") + parts.join(", ") + ".";
      if (data.failed) {
        showError(data.failed + " download" + (data.failed === 1 ? "" : "s") +
                  " failed \u2014 see the table for details.");
      }
    } else if (type === "error") {
      setRunning(false);
      showError(data.message || "Something went wrong.");
    }
  };

  // Prefill key/output dir from azkey.toml once the bridge is ready.
  window.addEventListener("pywebviewready", function () {
    apiReady = true;
    var a = api();
    if (!a || !a.prefill) return;
    Promise.resolve(a.prefill()).then(function (cfg) {
      if (cfg && cfg.key && !apiKey.value) apiKey.value = cfg.key;
      if (cfg && cfg.out_dir && !outDir.value) outDir.value = cfg.out_dir;
    });
  });

  refreshCount();
})();
