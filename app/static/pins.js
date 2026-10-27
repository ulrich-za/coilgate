// SPDX-License-Identifier: GPL-2.0-or-later
// Pinout page: interface tabs highlight their pins on the header (as on
// Pinout.xyz), and selecting a pin shows everything it can do. live.js keeps
// the pins' lit state current. Without JavaScript every section is shown.
(() => {
  "use strict";
  const header = document.querySelector(".pinout-board [data-pi-header]");
  const tabs = document.querySelector("[data-iface-tabs]");
  if (!header || !tabs) return;
  const buttons = [...tabs.querySelectorAll("[data-iface]")];
  const panels = [...document.querySelectorAll("[data-panel]")];
  const details = [...document.querySelectorAll("[data-pin-detail]")];
  const pins = [...header.querySelectorAll(".hpin")];
  let current = "coilgate";
  let selected = null;

  function showPanel(id) {
    panels.forEach((p) => { p.hidden = p.dataset.panel !== id; });
    details.forEach((d) => { d.hidden = true; });
  }

  function highlight(id) {
    const tab = buttons.find((b) => b.dataset.iface === id);
    const map = tab && tab.dataset.map ? JSON.parse(tab.dataset.map) : null;
    header.dataset.mode = id;
    for (const pin of pins) {
      const label = pin.querySelector(".hpin-label");
      let hit = false, main = false, text = label ? label.dataset.default : "";
      if (id === "coilgate") {
        hit = main = pin.dataset.used === "yes";
      } else if (map && map[pin.dataset.physical]) {
        [text, main] = map[pin.dataset.physical];
        hit = true;
      }
      pin.classList.toggle("is-hit", hit && main);
      pin.classList.toggle("is-alt", hit && !main);
      pin.classList.toggle("is-dim", !hit);
      if (label) label.textContent = text;
    }
  }

  function selectTab(id, remember) {
    if (!buttons.some((b) => b.dataset.iface === id)) id = "coilgate";
    current = id;
    buttons.forEach((b) => b.setAttribute("aria-selected", String(b.dataset.iface === id)));
    const active = buttons.find((b) => b.dataset.iface === id);
    if (active && tabs.scrollWidth > tabs.clientWidth) {
      tabs.scrollTo({ left: active.offsetLeft - tabs.clientWidth / 2 + active.offsetWidth / 2, behavior: "smooth" });
    }
    selectPin(null, false);
    showPanel(id);
    highlight(id);
    if (remember) history.replaceState(null, "", id === "coilgate" ? location.pathname : `#${id}`);
  }

  function selectPin(physical, remember) {
    selected = physical;
    pins.forEach((p) => p.classList.toggle("is-selected", p.dataset.physical === String(physical)));
    if (physical === null) return;
    panels.forEach((p) => { p.hidden = true; });
    details.forEach((d) => { d.hidden = d.dataset.pinDetail !== String(physical); });
    if (remember) history.replaceState(null, "", `#pin-${physical}`);
    // On narrow screens the details are below the header: bring them into view.
    const detail = details.find((d) => !d.hidden);
    if (detail && detail.getBoundingClientRect().top > window.innerHeight) {
      detail.scrollIntoView({ behavior: "smooth", block: "start" });
    }
  }

  tabs.hidden = false;
  buttons.forEach((b) => b.addEventListener("click", () => selectTab(b.dataset.iface, true)));
  pins.forEach((p) => p.addEventListener("click", () => {
    if (selected === p.dataset.physical) {
      selectTab(current, true);               // second click: back to the interface
    } else {
      selectPin(p.dataset.physical, true);
    }
  }));
  document.querySelectorAll("[data-pin-close]").forEach((b) => {
    b.addEventListener("click", () => selectTab(current, true));
  });
  document.querySelectorAll("[data-goto-iface]").forEach((b) => {
    b.addEventListener("click", () => selectTab(b.dataset.gotoIface, true));
  });
  document.addEventListener("keydown", (event) => {
    if (event.key === "Escape" && selected !== null) selectTab(current, true);
  });

  // Follow the address: #i2c, #pin-11, or the pins in use.
  function fromHash() {
    const hash = location.hash.slice(1);
    if (/^pin-\d+$/.test(hash) && details.some((d) => d.dataset.pinDetail === hash.slice(4))) {
      selectTab("coilgate", false);
      selectPin(hash.slice(4), false);
    } else {
      selectTab(hash || "coilgate", false);
    }
  }
  window.addEventListener("hashchange", fromHash);
  fromHash();
})();
