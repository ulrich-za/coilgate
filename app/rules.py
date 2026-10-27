# SPDX-License-Identifier: GPL-2.0-or-later
"""Rules: when something is true, do something to an output or another rule.

    When Living Room Temperature is above 26 °C,
    turn on Living Room Aircon,
    and turn it off again when it drops to 24 °C.

A rule has a trigger (an input's state, a sensor reading past a value, a
schedule being active, or another rule being active), optionally a minimum time
that must pass first, an optional schedule it's limited to, and an action: turn
an output on or off, toggle or pulse it, or switch another rule on, off or
toggle it. With "undo" on, the opposite action runs when the condition stops
being true.

A rule is *active* while its condition is met (after its waiting time, within
its schedule). That's what another rule can be triggered by, which lets rules
be chained: "When Front door is Open for 2 minutes, turn on Hall light" can
start "When that rule is active for 5 minutes, turn on Siren".

The engine checks every rule once a second, and at once whenever an input
changes. Actions run when a condition *becomes* true (or stops being true, for
undo), not continuously, so you can still switch an output by hand in between.
When CoilGate starts, any rule whose condition is already true runs straight
away, which restores the right state after a power cut.
"""
from __future__ import annotations

import dataclasses
import logging
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime

from . import activity
from . import db as database
from . import schedules as sched
from .hardware import HardwareConfigError, IOManager, describe_pulse

log = logging.getLogger("coilgate")

# A chattering contact, a faulty sensor or two rules fighting could otherwise
# switch a relay many times a second. A rule that acts more than this many
# times in a minute (Admin > Settings) is paused for PAUSE_SECONDS; when the
# pause ends it catches up, so its output ends up where the rule says.
DEFAULT_MAX_ACTIONS = 60
PAUSE_SECONDS = 60
CATCH_UP = ("on", "off", "rule_on", "rule_off")
OPPOSITE = {"on": "off", "off": "on", "rule_on": "rule_off", "rule_off": "rule_on"}
OUTPUT_ACTIONS = ("on", "off", "toggle", "pulse")
RULE_ACTIONS = ("rule_on", "rule_off", "rule_toggle")


@dataclass(frozen=True)
class Rule:
    id: int
    name: str
    enabled: bool
    trigger_type: str
    source_id: int | None
    source_rule_id: int | None
    input_state: str | None
    comparator: str | None
    threshold: float | None
    reset_threshold: float | None
    trigger_schedule_id: int | None
    hold_seconds: int
    schedule_id: int
    output_id: int | None
    target_rule_id: int | None
    action: str
    undo: bool

    @classmethod
    def from_row(cls, row) -> "Rule":
        return cls(
            id=row["id"], name=row["name"], enabled=bool(row["enabled"]),
            trigger_type=row["trigger_type"], source_id=row["source_id"],
            source_rule_id=_get(row, "source_rule_id"),
            input_state=row["input_state"], comparator=row["comparator"],
            threshold=row["threshold"], reset_threshold=row["reset_threshold"],
            trigger_schedule_id=row["trigger_schedule_id"], hold_seconds=row["hold_seconds"],
            schedule_id=row["schedule_id"], output_id=row["output_id"],
            target_rule_id=_get(row, "target_rule_id"),
            action=row["action"], undo=bool(row["undo"]),
        )

    @property
    def logic(self) -> tuple:
        """Everything except the name: if this changes, the rule starts over."""
        return (self.enabled, self.trigger_type, self.source_id, self.source_rule_id,
                self.input_state, self.comparator, self.threshold, self.reset_threshold,
                self.trigger_schedule_id, self.hold_seconds, self.schedule_id,
                self.output_id, self.target_rule_id, self.action, self.undo)

    @property
    def effective_reset(self) -> float | None:
        if self.trigger_type != "sensor" or self.threshold is None:
            return None
        return self.reset_threshold if self.reset_threshold is not None else self.threshold


def _get(row, key):
    try:
        return row[key]
    except (KeyError, IndexError):
        return None


def chain_order(rules: dict[int, "Rule"]) -> list[int]:
    """Rule ids ordered so a rule comes after the rule it's triggered by, so a
    chain of rules settles in one check."""
    depth: dict[int, int] = {}

    def level(rid: int, seen: frozenset = frozenset()) -> int:
        if rid in depth:
            return depth[rid]
        rule = rules.get(rid)
        if rule is None or rule.trigger_type != "rule" or rule.source_rule_id in seen | {rid}:
            depth[rid] = 0
        else:
            depth[rid] = level(rule.source_rule_id, seen | {rid}) + 1
        return depth[rid]

    return sorted(rules, key=lambda rid: (level(rid), rid))


def trigger_loop(rules: dict[int, "Rule"], rule_id: int | None, source_rule_id: int | None) -> list[str] | None:
    """If rule `rule_id` were triggered by `source_rule_id`, would the chain come
    back to it? Returns the loop's rule names, or None."""
    if rule_id is None or source_rule_id is None:
        return None
    path, current = [], source_rule_id
    while current is not None:
        if current == rule_id:
            names = [rules[rule_id].name] + [rules[r].name for r in path] + [rules[rule_id].name]
            return names
        if current in path or current not in rules:
            return None
        path.append(current)
        nxt = rules[current]
        current = nxt.source_rule_id if nxt.trigger_type == "rule" else None
    return None


@dataclass
class RuleState:
    active: bool = False
    true_since: float | None = None
    last_fired: float | None = None           # wall clock
    last_error: str | None = None
    recent: deque = field(default_factory=deque)
    paused_until: float = 0.0
    missed: str | None = None                 # an action held back by a pause


class RuleEngine:
    def __init__(self, db_path: str, io: IOManager, clock=time.monotonic):
        self.db_path = db_path
        self.io = io
        self.clock = clock
        self._lock = threading.RLock()
        self.rules: dict[int, Rule] = {}
        self.schedules: dict[int, sched.Schedule] = {}
        self.state: dict[int, RuleState] = {}
        self.order: list[int] = []
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._rules_changed = False
        self.max_actions = DEFAULT_MAX_ACTIONS

    # ------------------------------------------------------------ loading

    def reload(self) -> None:
        """Re-read rules and schedules after a change. A rule whose logic is
        unchanged keeps its state (renaming doesn't restart it)."""
        conn = database.connect(self.db_path)
        try:
            rules = {r["id"]: Rule.from_row(r) for r in conn.execute("SELECT * FROM rules")}
            schedules = sched.load_all(conn)
            from . import settings as runtime
            max_actions = runtime.load(conn).get("rule_max_actions_per_minute", DEFAULT_MAX_ACTIONS)
        finally:
            conn.close()
        self.max_actions = max_actions
        with self._lock:
            for rid in list(self.state):
                old = self.rules.get(rid)
                new = rules.get(rid)
                if new is None or old is None or old.logic != new.logic:
                    self.state.pop(rid, None)
            self.rules, self.schedules = rules, schedules
            self.order = chain_order(rules)

    # ------------------------------------------------------------ evaluating

    def _schedule_active(self, schedule_id: int | None, now: datetime) -> bool:
        if schedule_id is None:
            return True
        s = self.schedules.get(schedule_id)
        return True if s is None else s.is_active(now)

    def condition(self, rule: Rule, state: RuleState, now: datetime) -> bool | None:
        """True or False, or None when it can't be known (sensor offline, input unavailable)."""
        if rule.trigger_type == "input":
            if rule.source_id is None or not self.io.available(rule.source_id):
                return None
            met = self.io.is_on(rule.source_id) == (rule.input_state == "active")
        elif rule.trigger_type == "rule":
            source = self.rules.get(rule.source_rule_id) if rule.source_rule_id else None
            if source is None:
                return None
            source_active = source.enabled and self.status(source.id).active
            met = source_active == (rule.input_state == "active")
        elif rule.trigger_type == "sensor":
            value = self.io.sensor_value(rule.source_id) if rule.source_id else None
            if value is None or rule.threshold is None:
                return None
            # Hysteresis: once active, stay active until the reading passes
            # the reset value, so it doesn't flick on and off around one value.
            limit = rule.effective_reset if state.active else rule.threshold
            met = value > limit if rule.comparator == "above" else value < limit
        else:
            met = self._schedule_active(rule.trigger_schedule_id, now)
        return met and self._schedule_active(rule.schedule_id, now)

    def evaluate_once(self, now: datetime | None = None) -> None:
        """Check every rule. If a rule switched another rule on or off, check
        again (a few times at most) so the result settles straight away."""
        now = now or datetime.now()
        with self._lock:
            for _ in range(5):
                self._rules_changed = False
                self._evaluate_pass(now)
                if not self._rules_changed:
                    break

    def _evaluate_pass(self, now: datetime) -> None:
        for rid in list(self.order):
            rule = self.rules.get(rid)
            if rule is None:
                continue
            state = self.state.setdefault(rule.id, RuleState())
            if not rule.enabled:
                state.active, state.true_since = False, None
                continue
            if state.paused_until and self.clock() >= state.paused_until:
                self._resume(rule, state)
            try:
                met = self.condition(rule, state, now)
            except Exception:
                log.exception("Checking rule %s failed", rule.name)
                continue
            if met is None:
                continue                     # unknown: change nothing
            mono = self.clock()
            if met and not state.active:
                if state.true_since is None:
                    state.true_since = mono
                if mono - state.true_since >= rule.hold_seconds:
                    state.active = True
                    self._fire(rule, state, rule.action, "start")
            elif not met:
                state.true_since = None
                if state.active:
                    state.active = False
                    if rule.undo and rule.action in OPPOSITE:
                        self._fire(rule, state, OPPOSITE[rule.action], "end")

    def _fire(self, rule: Rule, state: RuleState, action: str, phase: str) -> None:
        mono = self.clock()
        if mono < state.paused_until:
            state.missed = action                # caught up when the pause ends
            return
        target_name = self._target_name(rule)
        state.recent.append(mono)
        while state.recent and mono - state.recent[0] >= 60:
            state.recent.popleft()
        if len(state.recent) > self.max_actions:
            state.paused_until = mono + PAUSE_SECONDS
            state.missed = action
            self._log("automation", "paused", target_name, rule.output_id, f"Rule: {rule.name}")
            return
        try:
            if action in RULE_ACTIONS:
                logged = self._switch_rule(rule, action)
            else:
                logged = self._switch_output(rule, action)
        except (HardwareConfigError, ValueError, KeyError, LookupError) as exc:
            state.last_error = str(exc) or "Its target no longer exists."
            self._log("automation", "failed", target_name, rule.output_id,
                      f"Rule {rule.name}: {state.last_error}")
            return
        state.last_error = None
        state.last_fired = time.time()
        self._log("automation", logged, target_name, rule.output_id, f"Rule: {rule.name}")
        conn = database.connect(self.db_path)
        try:
            conn.execute("UPDATE rules SET last_fired_at = ? WHERE id = ?",
                         (int(state.last_fired), rule.id))
            conn.commit()
        finally:
            conn.close()

    def _resume(self, rule: Rule, state: RuleState) -> None:
        """End a pause, and put the output (or rule) where the rule now says,
        in case an action was held back. Toggles and pulses aren't repeated."""
        state.paused_until = 0.0
        state.recent.clear()
        missed, state.missed = state.missed, None
        self._log("automation", "resumed", self._target_name(rule), rule.output_id,
                  f"Rule: {rule.name}")
        if missed is None:
            return
        if state.active:
            wanted = rule.action
        elif rule.undo and rule.action in OPPOSITE:
            wanted = OPPOSITE[rule.action]
        else:
            return
        if wanted not in CATCH_UP or self._already(rule, wanted):
            return
        self._fire(rule, state, wanted, "catch-up")

    def _already(self, rule: Rule, action: str) -> bool:
        """Is the target already how `action` would leave it?"""
        if action in ("on", "off"):
            output = self.io.get(rule.output_id) if rule.output_id is not None else None
            return output is not None and self.io.available(output.id) \
                and self.io.is_on(output.id) == (action == "on")
        target = self.rules.get(rule.target_rule_id) if rule.target_rule_id is not None else None
        return target is not None and target.enabled == (action == "rule_on")

    def resume(self, rule_id: int) -> bool:
        """End a rule's pause now (Admin > Rules > Resume now)."""
        with self._lock:
            state = self.state.get(rule_id)
            if state is None or not state.paused_until:
                return False
            state.paused_until = self.clock()
        self.wake()
        return True

    def paused_for(self, rule_id: int) -> float:
        """Seconds left in a rule's pause (0 when it isn't paused)."""
        state = self.state.get(rule_id)
        if state is None or not state.paused_until:
            return 0.0
        return max(0.0, state.paused_until - self.clock())

    def _target_name(self, rule: Rule) -> str | None:
        if rule.target_rule_id is not None:
            target = self.rules.get(rule.target_rule_id)
            return target.name if target else None
        output = self.io.get(rule.output_id) if rule.output_id is not None else None
        return output.name if output else None

    def _switch_output(self, rule: Rule, action: str) -> str:
        output = self.io.get(rule.output_id) if rule.output_id is not None else None
        if output is None:
            raise LookupError("Its output no longer exists.")
        if action == "pulse":
            self.io.pulse(output.id)
            return "pulse"
        on = (not self.io.is_on(output.id)) if action == "toggle" else action == "on"
        self.io.set(output.id, on)
        return "on" if on else "off"

    def _switch_rule(self, rule: Rule, action: str) -> str:
        target = self.rules.get(rule.target_rule_id) if rule.target_rule_id is not None else None
        if target is None:
            raise LookupError("The rule it switches no longer exists.")
        enable = (not target.enabled) if action == "rule_toggle" else action == "rule_on"
        if enable != target.enabled:
            conn = database.connect(self.db_path)
            try:
                conn.execute("UPDATE rules SET enabled = ?, updated_at = ? WHERE id = ?",
                             (int(enable), int(time.time()), target.id))
                conn.commit()
            finally:
                conn.close()
            self.rules[target.id] = dataclasses.replace(target, enabled=enable)
            self._rules_changed = True
        return "rule_on" if enable else "rule_off"

    def _log(self, category, action, target_name, output_id, detail) -> None:
        conn = database.connect(self.db_path)
        try:
            activity.log(conn, category, action, target=target_name, detail=detail,
                         io_id=output_id if self.io.get(output_id) else None)
        finally:
            conn.close()
        self.io.bump()          # show it on open Controls pages straight away

    # ------------------------------------------------------------ status for the UI

    def status(self, rule_id: int) -> RuleState:
        return self.state.get(rule_id) or RuleState()

    # ------------------------------------------------------------ background thread

    def start(self) -> None:
        if self._thread is None or not self._thread.is_alive():
            self._stop.clear()
            self._thread = threading.Thread(target=self._loop, name="coilgate-rules", daemon=True)
            self._thread.start()

    def wake(self) -> None:
        """Check the rules now (called when an input changes)."""
        self._wake.set()

    def _loop(self) -> None:
        while not self._stop.is_set():
            self._wake.wait(1.0)
            self._wake.clear()
            if self._stop.is_set():
                break
            try:
                self.evaluate_once()
            except Exception:
                log.exception("Rule engine tick failed")

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()


# ---------------------------------------------------------------- describing rules

def action_phrase(action: str, target: str) -> str:
    return {
        "on": f"turn on {target}", "off": f"turn off {target}",
        "toggle": f"toggle {target}", "pulse": f"pulse {target}",
        "rule_on": f"switch on rule {target}", "rule_off": f"switch off rule {target}",
        "rule_toggle": f"toggle rule {target}",
    }.get(action, f"{action} {target}")


def describe(rule: Rule, points: dict, schedules: dict,
             rules: dict | None = None) -> list[tuple[str, bool]]:
    """The rule as a sentence: [(text, emphasised)], for display."""
    from .sensors import format_value
    rules = rules or {}

    def name(pid):
        p = points.get(pid)
        return p.name if p else "(removed)"

    def rule_name(rid):
        r = rules.get(rid)
        return r.name if r else "(removed)"

    parts: list[tuple[str, bool]] = []
    source = points.get(rule.source_id) if rule.source_id else None
    if rule.trigger_type == "input":
        state_text = (source.active_text if rule.input_state == "active" else source.inactive_text) \
            if source else rule.input_state
        parts += [("When ", False), (name(rule.source_id), True), (" is ", False), (state_text, True)]
    elif rule.trigger_type == "sensor":
        value = format_value(rule.threshold, source.quantity) if source else f"{rule.threshold:g}"
        parts += [("When ", False), (name(rule.source_id), True),
                  (f" is {rule.comparator} ", False), (value, True)]
    elif rule.trigger_type == "rule":
        parts += [("When rule ", False), (rule_name(rule.source_rule_id), True), (" is ", False),
                  ("active" if rule.input_state == "active" else "not active", True)]
    else:
        trig = schedules.get(rule.trigger_schedule_id)
        parts += [("While ", False), (trig.name if trig else "(removed)", True), (" is active", False)]

    if rule.hold_seconds and rule.trigger_type != "schedule":
        parts.append((f" for at least {describe_pulse(rule.hold_seconds)}", False))
    limit = schedules.get(rule.schedule_id)
    if limit and not limit.builtin:
        parts += [(", during ", False), (limit.name, True)]

    target = rule_name(rule.target_rule_id) if rule.action in RULE_ACTIONS else name(rule.output_id)
    parts += [(", ", False), (action_phrase(rule.action, target), True)]

    if rule.undo and rule.action in OPPOSITE:
        if rule.action in RULE_ACTIONS:
            again = f"switch it {'off' if rule.action == 'rule_on' else 'on'} again"
        else:
            again = f"turn it {OPPOSITE[rule.action]} again"
        if rule.trigger_type == "sensor" and source:
            direction = "drops to" if rule.comparator == "above" else "rises to"
            parts += [(f", and {again} when it {direction} ", False),
                      (format_value(rule.effective_reset, source.quantity), True)]
        elif rule.trigger_type == "schedule":
            trig = schedules.get(rule.trigger_schedule_id)
            parts.append((f", and {again} when {trig.name if trig else 'it'} ends", False))
        else:
            parts.append((f", and {again} when that's no longer the case", False))
    parts.append((".", False))
    return parts


def sentence(rule: Rule, points: dict, schedules: dict, rules: dict | None = None) -> str:
    return "".join(text for text, _ in describe(rule, points, schedules, rules))
