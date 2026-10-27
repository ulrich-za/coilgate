# SPDX-License-Identifier: GPL-2.0-or-later
"""Admin > Groups: what the accounts in each group may do. Admins only."""
from __future__ import annotations

import sqlite3

from flask import abort, flash, g, redirect, render_template, request, url_for

from .. import groups as grp
from ..db import get_db
from .common import audit, bp, requires


@bp.get("/admin/groups")
@requires("groups")
def admin_groups():
    all_groups = grp.load_all(get_db())
    return render_template("admin_groups.html", groups=all_groups,
                           summaries={gid: grp.summary(x, all_groups) for gid, x in all_groups.items()})


def parse_group_form(form, group: grp.Group | None, all_groups: dict) -> tuple[dict, list[str]]:
    errors = []
    if group is not None and group.builtin:
        name = group.name
    else:
        name = " ".join(form.get("name", "").split())
        if not 1 <= len(name) <= grp.NAME_MAX:
            errors.append(f"Give the group a name of 1 to {grp.NAME_MAX} characters.")
        elif any(x.name.lower() == name.lower() and (group is None or x.id != group.id)
                 for x in all_groups.values()):
            errors.append(f"There's already a group called {name}.")
    targets = set()
    for value in form.getlist("manages"):
        try:
            gid = int(value)
        except ValueError:
            continue
        if gid in all_groups and not all_groups[gid].is_admin:
            targets.add(gid)
    if group is None and form.get("manages_self"):
        targets.add("self")
    return {
        "name": name,
        "require_2fa": "require_2fa" in form,
        "privileges": grp.clean_privileges(form.getlist("priv")),
        "targets": targets,
    }, errors


def render_form(group, values, errors=(), status=200):
    db = get_db()
    all_groups = grp.load_all(db)
    members = []
    if group is not None:
        members = db.execute("SELECT id, username, is_active FROM users WHERE group_id = ?"
                             " ORDER BY username", (group.id,)).fetchall()
    return render_template(
        "admin_group_form.html", group=group, values=values, errors=list(errors),
        groups=all_groups, members=members, privileges=grp.PRIVILEGES, sections=grp.SECTIONS,
    ), status


def values_of(group: grp.Group) -> dict:
    return {"name": group.name, "require_2fa": group.require_2fa,
            "privileges": list(group.privileges), "targets": set(group.targets)}


def _save(group_id, values) -> int:
    db = get_db()
    targets = values["targets"]
    new_id = grp.save(db, group_id, name=values["name"], require_2fa=values["require_2fa"],
                      privileges=values["privileges"], targets=targets - {"self"})
    if "self" in targets:            # a new group that manages its own accounts
        db.execute("INSERT OR IGNORE INTO group_manages (group_id, target_id) VALUES (?, ?)",
                   (new_id, new_id))
        db.commit()
    return new_id


def describe_change(values: dict, all_groups: dict) -> str:
    g2 = grp.Group(0, values["name"], None, values["require_2fa"],
                   frozenset(values["privileges"]),
                   frozenset(t for t in values["targets"] if t != "self"))
    parts = grp.summary(g2, all_groups)
    parts.append("two-step required" if values["require_2fa"] else "two-step optional")
    return "; ".join(parts)


@bp.route("/admin/groups/new", methods=["GET", "POST"])
@requires("groups")
def admin_group_new():
    if request.method != "POST":
        return render_form(None, {"name": "", "require_2fa": True, "privileges": ["control"],
                                  "targets": set()})
    all_groups = grp.load_all(get_db())
    values, errors = parse_group_form(request.form, None, all_groups)
    if errors:
        return render_form(None, values, errors, 400)
    try:
        new_id = _save(None, values)
    except sqlite3.IntegrityError:
        return render_form(None, values, [f"There's already a group called {values['name']}."], 400)
    audit("user", "group_created", target=values["name"], detail=describe_change(values, all_groups))
    flash(f"Added the {values['name']} group. Put accounts in it from Accounts.", "success")
    return redirect(url_for("main.admin_group_edit", group_id=new_id))


@bp.route("/admin/groups/<int:group_id>", methods=["GET", "POST"])
@requires("groups")
def admin_group_edit(group_id: int):
    all_groups = grp.load_all(get_db())
    group = all_groups.get(group_id)
    if group is None:
        abort(404)
    if request.method != "POST":
        return render_form(group, values_of(group))
    values, errors = parse_group_form(request.form, group, all_groups)
    if errors:
        return render_form(group, values, errors, 400)
    if group.is_admin and not values["require_2fa"] and group.require_2fa:
        flash("Admins no longer need two-step verification. Anyone who learns an admin's "
              "password can now change everything, so use long, unique passwords.", "info")
    try:
        _save(group_id, values)
    except sqlite3.IntegrityError:
        return render_form(group, values, [f"There's already a group called {values['name']}."], 400)
    audit("user", "group_updated", target=values["name"],
          detail=("two-step required" if values["require_2fa"] else "two-step optional")
          if group.is_admin else describe_change(values, all_groups))
    flash(f"Saved the {values['name']} group. Its accounts have the new privileges from "
          "their next page.", "success")
    return redirect(url_for("main.admin_group_edit", group_id=group_id))


@bp.post("/admin/groups/<int:group_id>/delete")
@requires("groups")
def admin_group_delete(group_id: int):
    db = get_db()
    group = grp.load_all(db).get(group_id)
    if group is None:
        abort(404)
    if group.builtin:
        flash(f"{group.name} is built in, so it can't be removed.", "error")
        return redirect(url_for("main.admin_group_edit", group_id=group_id))
    if group.members:
        flash(f"Move the {group.members} account{'s' if group.members != 1 else ''} in "
              f"{group.name} to another group first.", "error")
        return redirect(url_for("main.admin_group_edit", group_id=group_id))
    db.execute("DELETE FROM user_groups WHERE id = ?", (group_id,))
    db.commit()
    audit("user", "group_deleted", target=group.name)
    flash(f"Removed the {group.name} group.", "success")
    return redirect(url_for("main.admin_groups"))
