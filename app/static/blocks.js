// SPDX-License-Identifier: GPL-2.0-or-later
// Drag-and-drop rule builder. Pieces (triggers, actions, schedules) go into
// the rule's slots by dragging them, or by tapping/clicking them, or with the
// keyboard (each piece is a button). Placing a piece fills the same form
// fields the sentence builder uses, so the server checks and saves the rule
// the same way.
//
// Dragging works with a mouse, a pen, or a finger: on touch screens, press and
// hold a piece for a moment, then drag. A quick tap places it straight away.
(() => {
  "use strict";
  const form = document.querySelector("form[data-blocks]");
  if (!form) return;
  form.hidden = false;
  document.querySelectorAll("[data-needs-js]").forEach((el) => { el.hidden = true; });

  const field = (name) => form.querySelector(`[data-field="${name}"]`);
  const slots = {};
  form.querySelectorAll(".slot[data-slot]").forEach((s) => { slots[s.dataset.slot] = s; });
  const pieces = [...document.querySelectorAll(".piece[data-slot]")];
  const placed = { when: null, then: null, during: null };

  // ------------------------------------------------------------ placing pieces

  const options = (piece) => (piece.dataset.options ? JSON.parse(piece.dataset.options) : []);
  const nameOf = (piece) => piece.querySelector(".piece-label").textContent;

  function chipFor(piece, chosen) {
    const slot = piece.dataset.slot;
    const chip = document.createElement("div");
    chip.className = `chip chip-${slot}`;
    const label = document.createElement("span");
    label.className = "chip-label";
    label.textContent = (piece.dataset.kind === "rule" ? "Rule: " : "") + nameOf(piece);
    chip.append(label);
    const opts = options(piece);
    if (opts.length > 1) {
      const select = document.createElement("select");
      select.className = "chip-select";
      select.setAttribute("aria-label", `What about ${nameOf(piece)}`);
      for (const [value, words] of opts) select.add(new Option(words, value));
      if (chosen && opts.some(([value]) => value === chosen)) select.value = chosen;
      select.addEventListener("change", () => { write(slot); update(); });
      chip.append(select);
    } else if (opts.length === 1) {
      const words = document.createElement("span");
      words.className = "chip-words";
      words.textContent = opts[0][1];
      chip.append(words);
    }
    if (slot !== "during") {
      const remove = document.createElement("button");
      remove.type = "button";
      remove.className = "chip-remove";
      remove.setAttribute("aria-label", `Remove ${nameOf(piece)}`);
      remove.textContent = "×";
      remove.addEventListener("click", () => clear(slot));
      chip.append(remove);
    }
    return chip;
  }

  // The option chosen in a placed piece (its state or action).
  function chosen(slot) {
    const piece = placed[slot];
    if (!piece) return "";
    const select = slots[slot].querySelector(".chip-select");
    if (select) return select.value;
    const opts = options(piece);
    return opts.length ? opts[0][0] : "";
  }

  // Copy what's in a slot to the form fields the server reads.
  function write(slot) {
    const piece = placed[slot];
    if (slot === "when") {
      const trigger = piece ? piece.dataset.trigger : "";
      field("trigger_type").value = trigger;
      field("trigger_schedule").value = trigger === "schedule" ? piece.dataset.id : "";
      field("source").value = !piece || trigger === "schedule" ? ""
        : trigger === "sensor" ? piece.dataset.id : `${piece.dataset.id}:${chosen("when")}`;
    } else if (slot === "then") {
      field("target").value = piece ? `${piece.dataset.id}:${chosen("then")}` : "";
    } else if (piece) {
      field("schedule_id").value = piece.dataset.id;
    }
  }

  function place(piece, choice) {
    const slot = piece.dataset.slot;
    const bar = form.querySelector("[data-bar]");
    if (bar) {
      bar.classList.remove("flash-in");
      void bar.offsetWidth;
      bar.classList.add("flash-in");
    }
    placed[slot] = piece;
    const body = slots[slot].querySelector("[data-slot-body]");
    body.replaceChildren(chipFor(piece, choice));
    slots[slot].classList.add("slot-filled");
    pieces.forEach((p) => p.classList.toggle("piece-used", Object.values(placed).includes(p)));
    write(slot);
    update();
    const select = body.querySelector(".chip-select");
    // Keyboard users carry on in the new chip's menu, without the page jumping.
    if (select && !choice && document.activeElement === piece) select.focus({ preventScroll: true });
  }

  function clear(slot) {
    placed[slot] = null;
    const body = slots[slot].querySelector("[data-slot-body]");
    const empty = document.createElement("p");
    empty.className = "slot-empty";
    empty.textContent = slots[slot].dataset.empty;
    body.replaceChildren(empty);
    slots[slot].classList.remove("slot-filled");
    pieces.forEach((p) => p.classList.toggle("piece-used", Object.values(placed).includes(p)));
    write(slot);
    update();
  }

  // Show the fields that fit the pieces in place, and a preview sentence.
  function update() {
    const trigger = placed.when ? placed.when.dataset.trigger : "";
    form.querySelectorAll("[data-show-for]").forEach((el) => {
      el.hidden = el.dataset.showFor !== trigger;
    });
    form.querySelectorAll("[data-hide-for]").forEach((el) => {
      el.hidden = el.dataset.hideFor === trigger;
    });
    const unit = form.querySelector("[data-unit-text]");
    if (unit) unit.textContent = placed.when && placed.when.dataset.unit ? placed.when.dataset.unit : "";

    const action = placed.then ? options(placed.then).find(([value]) => value === chosen("then")) : null;
    const undoable = !action || action[2] !== false;
    const undoBox = form.querySelector("[data-undo-box]");
    undoBox.disabled = !undoable;
    form.querySelector("[data-undo-row]").classList.toggle("is-disabled", !undoable);
    form.querySelector("[data-undo-hint]").hidden = undoable;
    form.querySelector("[data-undo-text]").textContent =
      trigger === "sensor" ? "and undo it when the reading gets back to"
        : trigger === "schedule" ? "and undo it when the schedule ends"
        : "and undo it when that's no longer the case";

    const sentence = preview(trigger, action, undoable && undoBox.checked);
    form.querySelector("[data-preview]").textContent = sentence;
    // On narrow screens the rule scrolls out of view while picking pieces;
    // a bar along the bottom keeps it (or the next step) in sight.
    const bar = form.querySelector("[data-bar-text]");
    if (bar) {
      bar.textContent = sentence
        || (!placed.when ? "Next: tap what starts the rule." : "Next: tap what the rule does.");
    }
  }

  function words(slot) {
    const piece = placed[slot];
    const found = options(piece).find(([value]) => value === chosen(slot));
    return found ? found[1] : "";
  }

  function preview(trigger, action, undo) {
    if (!placed.when || !placed.then) return "";
    const when = placed.when;
    let text;
    if (trigger === "sensor") {
      const comparator = form.querySelector("#comparator").value;
      const value = form.querySelector("#threshold").value || "…";
      text = `When ${nameOf(when)} is ${comparator} ${value} ${when.dataset.unit || ""}`.trimEnd();
    } else if (trigger === "schedule") {
      text = `While ${nameOf(when)} is active`;
    } else {
      text = `When ${trigger === "rule" ? "rule " : ""}${nameOf(when)} ${words("when")}`;
    }
    const hold = Number(form.querySelector("#hold_value").value || 0);
    if (hold > 0 && trigger !== "schedule") {
      const unit = form.querySelector("[name=hold_unit]").value === "min" ? "minute" : "second";
      text += ` for at least ${hold} ${unit}${hold === 1 ? "" : "s"}`;
    }
    if (placed.during && placed.during.dataset.id !== slots.during.dataset.always) {
      text += `, during ${nameOf(placed.during)}`;
    }
    const target = placed.then.dataset.kind === "rule" ? `rule ${nameOf(placed.then)}` : nameOf(placed.then);
    text += `, ${action ? action[1] : ""} ${target}`;
    if (undo) text += ", and undo it afterwards";
    return text + ".";
  }

  const jump = form.querySelector("[data-bar-jump]");
  if (jump) {
    jump.addEventListener("click", (event) => {
      event.preventDefault();
      form.querySelector(".canvas").scrollIntoView({ behavior: "smooth", block: "start" });
    });
  }

  form.addEventListener("input", update);
  form.addEventListener("change", update);

  // ------------------------------------------------------------ dragging
  // Pointer events cover mouse, pen and touch alike. A mouse or pen drag
  // starts after a few pixels of movement; a finger has to rest on the piece
  // briefly first, so swiping through the list still scrolls the page.

  let drag = null;

  function slotAt(x, y) {
    const el = document.elementFromPoint(x, y);
    return el ? el.closest(".slot[data-slot]") : null;
  }

  function startDrag() {
    const piece = drag.piece;
    const ghost = piece.cloneNode(true);
    ghost.classList.add("piece-ghost");
    ghost.removeAttribute("id");
    ghost.setAttribute("aria-hidden", "true");
    document.body.append(ghost);
    const rect = piece.getBoundingClientRect();
    drag.ghost = ghost;
    drag.dx = drag.x - rect.left;
    drag.dy = drag.y - rect.top;
    drag.active = true;
    document.body.classList.add("is-dragging");
    slots[piece.dataset.slot].classList.add("slot-ready");
    moveGhost(drag.x, drag.y);
    if (drag.touch && navigator.vibrate) navigator.vibrate(10);
  }

  function moveGhost(x, y) {
    drag.ghost.style.transform = `translate(${x - drag.dx}px, ${y - drag.dy}px)`;
    const over = slotAt(x, y);
    Object.values(slots).forEach((s) => s.classList.toggle("slot-over", s === over && s.dataset.slot === drag.piece.dataset.slot));
  }

  function endDrag(placeIt) {
    if (!drag) return;
    clearTimeout(drag.timer);
    if (drag.active) {
      const over = slotAt(drag.x, drag.y);
      if (placeIt && over && over.dataset.slot === drag.piece.dataset.slot) place(drag.piece);
      drag.ghost.remove();
      document.body.classList.remove("is-dragging");
      Object.values(slots).forEach((s) => s.classList.remove("slot-ready", "slot-over"));
      const piece = drag.piece;
      piece.dataset.dragged = "yes";        // the click that follows isn't a tap
      setTimeout(() => { delete piece.dataset.dragged; }, 0);
    }
    drag = null;
  }

  document.addEventListener("pointerdown", (event) => {
    const piece = event.target.closest(".piece[data-slot]");
    if (!piece || (event.pointerType === "mouse" && event.button !== 0)) return;
    drag = { piece, x: event.clientX, y: event.clientY, startX: event.clientX, startY: event.clientY,
             touch: event.pointerType === "touch", active: false, id: event.pointerId };
    if (drag.touch) drag.timer = setTimeout(() => { if (drag && !drag.active) startDrag(); }, 280);
  });

  document.addEventListener("pointermove", (event) => {
    if (!drag || event.pointerId !== drag.id) return;
    drag.x = event.clientX;
    drag.y = event.clientY;
    const moved = Math.hypot(drag.x - drag.startX, drag.y - drag.startY);
    if (!drag.active) {
      if (drag.touch) {
        if (moved > 8) { clearTimeout(drag.timer); drag = null; }   // scrolling, not dragging
        return;
      }
      if (moved < 5) return;
      startDrag();
    }
    event.preventDefault();
    moveGhost(drag.x, drag.y);
  });

  document.addEventListener("pointerup", (event) => {
    if (drag && event.pointerId === drag.id) endDrag(true);
  });
  document.addEventListener("pointercancel", () => endDrag(false));
  document.addEventListener("keydown", (event) => { if (event.key === "Escape") endDrag(false); });
  // While a finger drags a piece, the page mustn't scroll underneath it.
  document.addEventListener("touchmove", (event) => {
    if (drag && drag.active) event.preventDefault();
  }, { passive: false });
  document.addEventListener("contextmenu", (event) => {
    if (drag && drag.touch) event.preventDefault();      // long-press menu on Android
  });

  // A tap, click, Enter or Space places the piece.
  pieces.forEach((piece) => {
    piece.addEventListener("click", () => {
      if (piece.dataset.dragged) return;
      place(piece);
    });
  });

  // ------------------------------------------------------------ finding pieces

  const filter = document.querySelector("[data-filter]");
  if (filter) {
    filter.addEventListener("input", () => {
      const words = filter.value.toLowerCase().split(/\s+/).filter(Boolean);
      pieces.forEach((p) => {
        const text = p.textContent.toLowerCase();
        p.parentElement.hidden = !words.every((w) => text.includes(w));
      });
      document.querySelectorAll(".palette .pieces").forEach((list) => {
        const any = [...list.children].some((li) => !li.hidden);
        list.hidden = !any;
        const sub = list.previousElementSibling;
        if (sub && sub.classList.contains("palette-sub")) sub.hidden = !any;
      });
    });
    filter.addEventListener("keydown", (event) => {
      if (event.key === "Enter") event.preventDefault();   // don't submit the rule
    });
  }

  // ------------------------------------------------------------ saving

  form.addEventListener("submit", (event) => {
    const missing = [];
    if (!placed.when) missing.push("what starts the rule (When)");
    if (!placed.then) missing.push("what it does (Then)");
    const box = form.querySelector("[data-missing]");
    if (missing.length) {
      event.preventDefault();
      box.textContent = `Add ${missing.join(" and ")} first.`;
      box.hidden = false;
      return;
    }
    box.hidden = true;
  });

  // ------------------------------------------------------------ start from the saved rule

  Object.values(slots).forEach((s) => {
    const empty = s.querySelector(".slot-empty");
    s.dataset.empty = empty ? empty.textContent : "";
  });
  const find = (slot, id, trigger) => pieces.find((p) => p.dataset.slot === slot
    && p.dataset.id === id && (!trigger || p.dataset.trigger === trigger));
  const split = (value) => {
    const at = value.lastIndexOf(":");
    return at < 0 ? [value, ""] : [value.slice(0, at), value.slice(at + 1)];
  };
  const trigger = field("trigger_type").value;
  if (trigger === "schedule") {
    const piece = find("when", field("trigger_schedule").value, "schedule");
    if (piece) place(piece);
  } else if (trigger && field("source").value) {
    const [id, state] = trigger === "sensor" ? [field("source").value, ""] : split(field("source").value);
    const piece = find("when", id, trigger);
    if (piece) place(piece, state);
  }
  if (field("target").value) {
    const [id, action] = split(field("target").value);
    const piece = find("then", id);
    if (piece) place(piece, action);
  }
  const during = find("during", field("schedule_id").value);
  if (during) place(during);
  update();
})();
