// SPDX-License-Identifier: GPL-2.0-or-later
// Controls page. The page works without this file (plain forms and redirects);
// with it, buttons are sent in the background so the page never reloads or
// jumps, the activity panel and status line update in place, and lamps,
// states and readings update the moment they change while the tab is visible.
(() => {
  "use strict";
  const root = document.querySelector("[data-live-state]");
  if (!root) return;
  const stateUrl = root.dataset.liveState;
  const dock = document.getElementById("activity-dock");
  const logTable = dock ? dock.querySelector("table.log") : null;
  const logBody = logTable ? logTable.tBodies[0] : null;
  const logEmpty = dock ? dock.querySelector("[data-log-empty]") : null;
  const status = dock ? dock.querySelector("[data-status]") : null;
  const MAX_ROWS = 100;
  let lastId = dock ? Number(dock.dataset.lastId || 0) : 0;

  // ------------------------------------------------------------ points

  function setButton(button, primary, value, text) {
    button.value = value;
    button.textContent = text;
    button.classList.toggle("button-primary", primary);
    button.classList.toggle("button-secondary", !primary);
  }

  // Header pins carrying an output or input: glow while active, and show the
  // voltage on the pin (H/L), which is inverted for active-low wiring.
  function applyPins(point) {
    for (const pin of document.querySelectorAll(`[data-pin-point="${point.id}"]`)) {
      const high = point.on === (pin.dataset.activeLevel === "high");
      pin.dataset.on = point.on ? "yes" : "no";
      pin.dataset.level = high ? "H" : "L";
      if (point.available === false) pin.dataset.error = "yes";
      else delete pin.dataset.error;
      const text = pin.querySelector("[data-level-text]");
      if (text) text.textContent = high ? "H" : "L";
    }
  }

  function applyPoints(points) {
    for (const point of points) {
      applyPins(point);
      const row = document.getElementById("io-" + point.id);
      if (!row) continue;
      if (point.kind === "sensor") {
        if (point.available) delete row.dataset.error;
        else row.dataset.error = point.state_text.startsWith("Unavailable") ? "yes" : "wait";
      } else {
        row.dataset.on = point.on ? "yes" : "no";
      }
      const text = row.querySelector("[data-state-text]");
      if (text && text.textContent !== point.state_text) text.textContent = point.state_text;
      const toggle = row.querySelector("button[data-toggle]");
      if (toggle) setButton(toggle, !point.on, point.on ? "off" : "on", point.on ? "Turn off" : "Turn on");
      const pulse = row.querySelector("button[data-pulse]");
      if (pulse) setButton(pulse, !point.pulsing, point.pulsing ? "stop" : "pulse", point.pulsing ? "Stop" : "Pulse");
    }
  }

  // ------------------------------------------------------------ activity panel

  function cell(className, text, extra) {
    const td = document.createElement("td");
    td.className = className;
    td.textContent = text;
    if (extra) {
      const span = document.createElement("span");
      span.className = "m-only";
      span.textContent = " · " + extra;
      td.append(span);
    }
    return td;
  }

  function addEntries(entries) {
    if (!logBody || !entries || !entries.length) return;
    // Entries arrive newest first; insert oldest first so the newest ends on top.
    for (const e of entries.slice().reverse()) {
      if (e.id <= lastId) continue;
      const tr = document.createElement("tr");
      tr.className = "fresh";
      tr.append(
        cell("c-when", e.when),
        cell("c-what", e.what, e.change),
        cell("c-change", e.change),
        cell("c-by", e.by, e.ip),
        cell("c-ip", e.ip),
      );
      logBody.prepend(tr);
      lastId = e.id;
    }
    while (logBody.rows.length > MAX_ROWS) logBody.deleteRow(-1);
    logTable.hidden = false;
    if (logEmpty) logEmpty.hidden = true;
  }

  function showStatus(message, level) {
    if (!status || !message) return;
    status.textContent = message;
    status.dataset.level = level || "info";
    status.classList.remove("flash-in");
    void status.offsetWidth; // restart the highlight
    status.classList.add("flash-in");
  }

  // ------------------------------------------------------------ live updates
  // Each request waits on the Pi until something changes (or about 25 s pass),
  // then the next one starts straight away. Changes show within a fraction of
  // a second, and an idle page sends a request only every 25 seconds or so.

  let version = Number(root.dataset.version);

  // The page was drawn for one setup of inputs, outputs and sensors. When an
  // admin adds, removes, renames or moves one, load the page again.
  function checkSetup(data) {
    if (root.dataset.setup === undefined || typeof data.setup !== "number") return false;
    if (String(data.setup) === root.dataset.setup) return false;
    window.location.reload();
    return true;
  }
  let controller = null;
  let failures = 0;
  let timer = null;

  function schedule(delay) {
    clearTimeout(timer);
    timer = setTimeout(listen, delay);
  }

  async function listen() {
    if (document.hidden) return;              // resumes on visibilitychange
    if (controller) controller.abort();
    controller = new AbortController();
    const url = `${stateUrl}?since=${lastId}&wait=1&v=${version}`
      + (root.dataset.activity === "0" ? "&activity=0" : "");
    let response;
    try {
      response = await fetch(url, {
        headers: { Accept: "application/json" },
        credentials: "same-origin",
        cache: "no-store",
        signal: controller.signal,
      });
    } catch (error) {
      if (error.name === "AbortError") return;
      failures += 1;                          // offline for a moment: back off
      schedule(Math.min(15000, 1000 * 2 ** failures));
      return;
    }
    if (response.status === 401 || response.status === 403) {
      window.location.reload();               // signed out or setup needed
      return;
    }
    if (!response.ok) {
      failures += 1;
      schedule(Math.min(15000, 1000 * 2 ** failures));
      return;
    }
    failures = 0;
    const data = await response.json();
    if (checkSetup(data)) return;
    version = data.version;
    applyPoints(data.points);
    addEntries(data.activity);
    schedule(0);
  }

  document.addEventListener("visibilitychange", () => {
    if (document.hidden) {
      if (controller) controller.abort();
      clearTimeout(timer);
    } else {
      version = -1;                           // fetch the current state at once
      schedule(0);
    }
  });

  // ------------------------------------------------------------ buttons and forms

  root.addEventListener("submit", async (event) => {
    const form = event.target;
    if (!(form instanceof HTMLFormElement) || form.method.toLowerCase() !== "post") return;
    event.preventDefault();
    const body = new FormData(form);
    const submitter = event.submitter;
    if (submitter && submitter.name) body.set(submitter.name, submitter.value);
    body.set("since", String(lastId));

    const buttons = [...form.querySelectorAll("button")];
    const wasDisabled = buttons.map((b) => b.disabled);
    buttons.forEach((b) => { b.disabled = true; });
    form.setAttribute("aria-busy", "true");
    try {
      const response = await fetch(form.action, {
        method: "POST",
        body,
        headers: { Accept: "application/json" },
        credentials: "same-origin",
        cache: "no-store",
      });
      const type = response.headers.get("Content-Type") || "";
      if (response.redirected || !type.includes("application/json")) {
        window.location.reload(); // e.g. signed out: the server sends the sign-in page
        return;
      }
      const data = await response.json();
      if (checkSetup(data)) return;
      if (data.points) applyPoints(data.points);
      addEntries(data.activity);
      showStatus(data.message, data.level);
      if (typeof data.version === "number") version = data.version;
    } catch {
      showStatus("Couldn't reach CoilGate. Check your connection and try again.", "error");
    } finally {
      buttons.forEach((b, i) => { b.disabled = wasDisabled[i]; });
      form.removeAttribute("aria-busy");
    }
  });

  // ------------------------------------------------------------ hide / show the panel

  const toggle = dock ? dock.querySelector("[data-dock-toggle]") : null;
  const KEY = "coilgate.dock";
  function setCollapsed(collapsed, remember) {
    document.body.classList.toggle("dock-collapsed", collapsed);
    toggle.setAttribute("aria-expanded", String(!collapsed));
    toggle.textContent = collapsed ? "Show" : "Hide";
    if (remember) {
      try { localStorage.setItem(KEY, collapsed ? "collapsed" : "open"); } catch { /* private mode */ }
    }
  }
  if (toggle) {
    let saved = null;
    try { saved = localStorage.getItem(KEY); } catch { /* private mode */ }
    toggle.hidden = false;
    setCollapsed(saved === "collapsed", false);
    toggle.addEventListener("click", () => {
      setCollapsed(!document.body.classList.contains("dock-collapsed"), true);
    });
  }

  // In the compact pin map beside the controls, a lit pin jumps to its row.
  document.addEventListener("click", (event) => {
    const pin = event.target.closest(".pi-header.compact .hpin[data-pin-point]");
    if (!pin) return;
    const row = document.getElementById(`io-${pin.dataset.pinPoint}`);
    if (!row) return;
    row.scrollIntoView({ behavior: "smooth", block: "center" });
    row.classList.remove("row-flash");
    void row.offsetWidth;
    row.classList.add("row-flash");
  });

  schedule(0);
})();
