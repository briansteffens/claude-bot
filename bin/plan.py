#!/usr/bin/env python3
"""Work out a tick's actions from a gather.py snapshot.

The mechanical half of /tick: the reconcile table (finalize, SYNC fan-out,
cancel, nudge, ping) and the build-slot rules (holders, quick lane, small-issue
rule, parallel-verdict staleness, slot_hold, design-ahead) live here in code,
once. The tick skill keeps the same rules in prose as the spec, and the
orchestrator keeps the judgement: the parallelism subagent, anything listed
under `stalled`, retries after `blocked`, briefs, and the conversation with
the owner.

Usage:
  bin/plan.py [snapshot.json] [--apply] [--now 2026-11-02T07:00:00Z]
  bin/plan.py --test

With no snapshot path it runs bin/gather.py. Prints JSON:

  {"projects": [{"repo": ..., "slots": {...},
     "finalize": [{issue, pr, session}],      PR merged: status -> done (--apply), tell the worker MERGED
     "sync": [{issue, session}],              every other building/in-review worker: send SYNC
     "cancel": [{issue, session, pr}],        closed unmerged: status -> cancelled (--apply), send CANCEL
     "ping": [{issue, session, phase}],       claude:ping: RESUME / redispatch / dispatch, then remove the label
     "adopt": [{issue}],                      dispatching without a session: list_sessions by tag
     "retry": [{issue, session, comments}],   blocked + new owner comments: read them, retry if asked
     "nudge": [{issue, session, why}],        ACTIVITY, unless the worker's status_bucket is working
     "build": [{issue, session, slot, handoff?}],  send BUILD (--apply records slot/clears slot_hold
                                              first); with `handoff` ({model, model_id}), start a
                                              build-stage session instead (tick: "Hand-off to a
                                              build session")
     "needs_parallel_check": {...} | null,    run the subagent, record verdicts, run plan.py again
     "needs_estimate": [issue],               ask the subagent for these sizes too
     "hold": [{issue, for}], "clear_hold": [issue],   slot_hold to set / remove (--apply does)
     "dispatch": [issue],                     queued issues to dispatch with DESIGN (bin/dispatch.py)
     "stalled": [{issue, why}]}],             look at these; don't act blindly
   "applied": [...]}                          what --apply changed on GitHub

--apply only makes the judgement-free GitHub edits listed above (status JSON,
phase labels, closing a merged issue). It never messages a worker and never
dispatches: the orchestrator does those from the output.
"""
import copy
import json
import os
import subprocess
import sys
from datetime import datetime, timedelta, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)

import run_config  # noqa: E402

HOLDING = ("building", "in-review")
PRE_BUILD = ("dispatching", "designing", "needs-input", "approved")
STALL_MINUTES = 30
FUTURE_SLACK = timedelta(minutes=5)


def parse_time(stamp):
    if not stamp:
        return None
    try:
        moment = datetime.fromisoformat(str(stamp).replace("Z", "+00:00"))
    except ValueError:
        return None
    return moment if moment.tzinfo else moment.replace(tzinfo=timezone.utc)


def stamp(moment):
    return moment.strftime("%Y-%m-%dT%H:%MZ")


def load_config():
    import yaml
    return yaml.safe_load(open(os.path.join(ROOT, "bot.config.yml")))


def project_settings(config):
    defaults = config.get("defaults") or {}
    return {p["repo"]: {**defaults, **p} for p in config.get("projects") or []}


def status_of(issue):
    return issue.get("status") or {}


def session_of(issue):
    return status_of(issue).get("session")


def lane(issue):
    value = status_of(issue).get("lane")
    if value in ("quick", "normal"):
        return value
    return "quick" if "lane:quick" in (issue.get("labels") or []) else "normal"


def size(issue):
    """S/M/L from the worker's estimate; quick-lane issues count as S; None if unknown."""
    estimate = status_of(issue).get("estimate") or {}
    if estimate.get("size") in ("S", "M", "L"):
        return estimate["size"]
    return "S" if lane(issue) == "quick" else None


def quick_total(settings):
    if settings.get("quick_lane") is False or settings.get("preview") == "deploy":
        return 0
    total = settings.get("quick_slots", 1)
    return total if isinstance(total, int) and total > 0 else 0


def handoff(issue, settings):
    """{model, model_id} when this BUILD should go to a fresh build-stage
    session, else None. Only for sessions recorded as design-stage (dispatched
    with per-stage models; older workers have no `stage`), never for the quick
    lane (built in its design session), and only if the build model differs."""
    run = status_of(issue).get("run") or {}
    if run.get("stage") != "design" or lane(issue) == "quick":
        return None
    models = run_config.stage_models({"defaults": {"models": settings.get("models")}})
    resolved = run_config.resolve(issue.get("labels") or [], "build", models)
    if resolved["run"]["model"] == run.get("model"):
        return None
    return {"model": resolved["run"]["model"], "model_id": resolved["model_id"]}


def build_item(issue, slot, settings):
    item = {"issue": issue["number"], "session": session_of(issue), "slot": slot,
            "comment_id": issue.get("status_comment_id")}
    moved = handoff(issue, settings)
    if moved:
        item["handoff"] = moved
    return item


def verdict(candidate, holders):
    """The cached parallel verdict if still valid against these holders, else None."""
    parallel = status_of(candidate).get("parallel") or {}
    if "ok" not in parallel or sorted(parallel.get("with") or []) != sorted(h["number"] for h in holders):
        return None
    heads = parallel.get("heads") or {}
    for holder in holders:
        head = (holder.get("pr") or {}).get("head_sha")
        recorded = heads.get(str(holder["number"]))
        # A holder that pushed since the check: its real diff beats the old evidence.
        if head and recorded and head != recorded:
            return None
    return bool(parallel["ok"])


def describe(issue):
    status = status_of(issue)
    return {"issue": issue["number"], "title": issue["title"], "phase": issue["phase"],
            "footprint": status.get("footprint"), "branch": status.get("branch"),
            "pr": (issue.get("pr") or {}).get("number"), "head": (issue.get("pr") or {}).get("head_sha"),
            "design": status.get("design"), "estimate": status.get("estimate")}


def pick_slots(free, holders, candidates, small_rule, waiting_designing, decided):
    """Hand out `free` slots in queue order. Returns (picks, needs_check, holds)."""
    picks, needs_check, holds = [], [], {}
    remaining = list(candidates)
    while free > 0 and remaining:
        current = holders + picks
        last = small_rule and free == 1
        chosen, unresolved = None, False
        for candidate in remaining:
            if last and size(candidate) == "L" and not candidate["next"]:
                waiting = [c["number"] for c in remaining if c is not candidate and size(c) != "L"]
                waiting += [d["number"] for d in waiting_designing]
                if waiting:
                    holds[candidate["number"]] = sorted(set(waiting))
                    continue
            if not current:
                chosen = candidate
                break
            result = verdict(candidate, current)
            if result is None:
                needs_check.append(candidate)
                unresolved = True
                continue
            if result and not unresolved:
                chosen = candidate
                break
            if result and unresolved:
                # Cleared, but an earlier candidate's check is pending: it may come first.
                break
        if not chosen:
            break
        picks.append(chosen)
        decided.add(chosen["number"])
        remaining.remove(chosen)
        holds.pop(chosen["number"], None)
        free -= 1
    return picks, needs_check, holds


def plan_project(project, settings, now, regular_pool=None):
    issues = project["issues"]
    out = {key: [] for key in ("finalize", "sync", "cancel", "ping", "adopt", "retry", "nudge", "build",
                               "needs_estimate", "hold", "clear_hold", "dispatch", "stalled")}
    out["repo"] = project["repo"]
    out["needs_parallel_check"] = None
    gone = set()

    for issue in issues:
        status = status_of(issue)
        phase = issue["phase"]
        pr = issue.get("pr") or {}
        number = issue["number"]
        if issue["paused"]:
            continue
        if issue["ping"]:
            out["ping"].append({"issue": number, "session": session_of(issue), "phase": phase})
        if phase in ("done", "cancelled"):
            gone.add(number)
            continue
        if pr.get("merged"):
            out["finalize"].append({"issue": number, "pr": pr.get("number"), "session": session_of(issue),
                                    "comment_id": issue.get("status_comment_id"), "open": issue["state"] == "open"})
            gone.add(number)
            continue
        if issue["state"] == "closed":
            out["cancel"].append({"issue": number, "session": session_of(issue), "pr": pr.get("number"),
                                  "comment_id": issue.get("status_comment_id")})
            gone.add(number)
            continue
        if phase == "dispatching" and not session_of(issue):
            out["adopt"].append({"issue": number})
            continue
        comments = [c["id"] for c in issue.get("unhandled_owner_issue_comments") or []]
        if phase == "blocked":
            if comments:
                out["retry"].append({"issue": number, "session": session_of(issue), "comments": comments})
        elif session_of(issue) and phase != "queued":
            if comments:
                out["nudge"].append({"issue": number, "session": session_of(issue), "why": "owner comment"})
            elif "claude:approved" in issue["labels"] and phase in ("designing", "needs-input"):
                out["nudge"].append({"issue": number, "session": session_of(issue), "why": "claude:approved label"})
        # Stalled or odd timestamps.
        progress = status.get("progress") or {}
        working = phase in ("designing", "building") or (phase == "in-review" and status.get("review_state") == "revising")
        at = parse_time(progress.get("at"))
        if working and at and now - at > timedelta(minutes=STALL_MINUTES):
            out["stalled"].append({"issue": number, "why": f"progress not updated for {int((now - at).total_seconds() // 60)} min"
                                   f" ({progress.get('step') or progress.get('stage')})"})
        for key, value in (("updated", status.get("updated")), ("progress.at", progress.get("at")),
                           ("waiting_since", status.get("waiting_since"))):
            moment = parse_time(value)
            if moment and moment - now > FUTURE_SLACK:
                out["stalled"].append({"issue": number, "why": f"{key} is in the future ({value})"})

    finalized = {f["issue"] for f in out["finalize"]}
    live = [i for i in issues if i["number"] not in gone]
    if finalized:
        out["sync"] = [{"issue": i["number"], "session": session_of(i)} for i in live
                       if i["phase"] in HOLDING and session_of(i)]

    active = [i for i in live if not i["paused"]]
    holders = [i for i in live if i["phase"] in HOLDING]
    quick_holders = [i for i in holders if status_of(i).get("slot") == "quick"]
    regular_holders = [i for i in holders if status_of(i).get("slot") != "quick"]
    approved = [i for i in active if i["phase"] == "approved"]
    decided = set()

    # Quick slot first.
    q_total = quick_total(settings)
    quick_picks, needs_check = [], []
    if q_total:
        free = q_total - len(quick_holders)
        for candidate in [c for c in approved if lane(c) == "quick"]:
            if free <= 0:
                break
            current = holders + quick_picks
            result = True if not current else verdict(candidate, current)
            if result is None:
                needs_check.append(candidate)
                break
            if result:
                quick_picks.append(candidate)
                decided.add(candidate["number"])
                free -= 1

    # Regular slots.
    total = settings.get("build_slots", 2)
    small_rule = isinstance(total, int) and total >= 2 and settings.get("small_slot", True) is not False
    designing = [i for i in active if i["phase"] in ("designing", "dispatching") and size(i) != "L"]
    candidates = [c for c in approved if c["number"] not in decided]
    if regular_pool is not None:
        # build_scope: global: the caller pools regular slots across projects.
        regular_pool["holders"] += regular_holders
        regular_pool["candidates"] += [(project["repo"], c) for c in candidates]
        picks, holds = [], {}
    else:
        free = (total if isinstance(total, int) else 2) - len(regular_holders)
        # Verdicts are against every holder, quick ones included: a quick change
        # can collide as easily as any other.
        picks, more, holds = pick_slots(free, holders + quick_picks, candidates, small_rule, designing, decided)
        needs_check += [c for c in more if c not in needs_check]
        out["slots"] = {"regular": f"{len(regular_holders)}/{total}"}
    if q_total:
        out.setdefault("slots", {})["quick"] = f"{len(quick_holders)}/{q_total}"

    for issue in quick_picks:
        out["build"].append(build_item(issue, "quick", settings))
    for issue in picks:
        out["build"].append(build_item(issue, "regular", settings))
    for number, waiting in sorted(holds.items()):
        out["hold"].append({"issue": number, "for": waiting})
    held = set(holds)
    out["clear_hold"] = [i["number"] for i in live if status_of(i).get("slot_hold") and i["number"] not in held]

    if needs_check:
        out["needs_parallel_check"] = {
            "holders": [describe(h) for h in holders + quick_picks + [p for p in picks]],
            "candidates": [describe(c) for c in needs_check],
            "record_as": "parallel = {with: [holder issue numbers], heads: {\"<n>\": head sha}, ok, reason, checked}",
        }
    # Sizes the small-issue rule needs and nobody has estimated (older workers).
    unknown = [c for c in candidates + quick_picks if size(c) is None]
    if small_rule:
        unknown += [d for d in active if d["phase"] in ("designing", "dispatching") and size(d) is None]
    out["needs_estimate"] = sorted({i["number"] for i in unknown})

    # Design ahead.
    in_design = sum(1 for i in live if i["phase"] in PRE_BUILD)
    room = (settings.get("design_ahead", 3) or 0) - in_design
    for issue in active:
        if issue["phase"] != "queued" or issue["ping"]:
            continue
        if room <= 0:
            break
        out["dispatch"].append(issue["number"])
        room -= 1
    # A ping on a queued issue means "start this one", design_ahead or not.
    out["dispatch"] += [p["issue"] for p in out["ping"] if p["phase"] == "queued" and p["issue"] not in out["dispatch"]]
    return out


def plan(snapshot, config, now=None):
    now = now or datetime.now(timezone.utc)
    settings = project_settings(config)
    pooled = config.get("build_scope") == "global"
    pool = {"holders": [], "candidates": []} if pooled else None
    projects = [plan_project(p, settings.get(p["repo"], config.get("defaults") or {}), now, pool) for p in snapshot["projects"]]
    if pooled:
        total = (config.get("defaults") or {}).get("build_slots", 2)
        order = sorted(pool["candidates"], key=lambda rc: (0 if rc[1]["next"] else 1, rc[1]["number"]))
        small_rule = total >= 2 and (config.get("defaults") or {}).get("small_slot", True) is not False
        designing = [i for p in snapshot["projects"] for i in p["issues"]
                     if i["phase"] in ("designing", "dispatching") and size(i) != "L" and not i["paused"]]
        picks, needs_check, holds = pick_slots(total - len(pool["holders"]), pool["holders"], [c for _, c in order],
                                               small_rule, designing, set())
        repo_of = {id(c): r for r, c in order}
        by_repo = {p["repo"]: p for p in projects}
        for issue in picks:
            repo = repo_of[id(issue)]
            by_repo[repo]["build"].append(build_item(issue, "regular", settings.get(repo, config.get("defaults") or {})))
        for issue in needs_check:
            entry = by_repo[repo_of[id(issue)]]
            entry["needs_parallel_check"] = entry["needs_parallel_check"] or {
                "holders": [describe(h) for h in pool["holders"]], "candidates": [], "note": "build_scope: global"}
            entry["needs_parallel_check"]["candidates"].append(describe(issue))
        for repo, issue in order:
            if issue["number"] in holds:
                by_repo[repo]["hold"].append({"issue": issue["number"], "for": holds[issue["number"]]})
                by_repo[repo]["clear_hold"] = [n for n in by_repo[repo]["clear_hold"] if n != issue["number"]]
        for p in projects:
            p.setdefault("slots", {})["regular"] = f"{len(pool['holders'])}/{total} (global)"
    return {"checked_at": snapshot.get("checked_at"), "projects": projects}


def apply(result, snapshot):
    """Make the judgement-free GitHub edits; returns a list of what changed."""
    import status as st
    applied = []
    issues = {(p["repo"], i["number"]): i for p in snapshot["projects"] for i in p["issues"]}
    for project in result["projects"]:
        repo = project["repo"]

        def fresh(number):
            comment_id, status, body = st.find(repo, number)
            return (comment_id, status, body) if comment_id else None

        for item in project["finalize"]:
            found = fresh(item["issue"])
            if found and found[1].get("phase") != "done":
                st.patch(repo, item["issue"], {}, unset=["slot_hold", "progress"], phase="done", found=found,
                         labels=issues[(repo, item["issue"])]["labels"])
                applied.append(f"{repo}#{item['issue']}: done")
            if item.get("open"):
                st.gh("-X", "PATCH", f"repos/{repo}/issues/{item['issue']}",
                      body={"state": "closed", "state_reason": "completed"})
                applied.append(f"{repo}#{item['issue']}: closed")
        for item in project["cancel"]:
            found = fresh(item["issue"])
            labels = issues[(repo, item["issue"])]["labels"]
            if found and found[1].get("phase") != "cancelled":
                st.patch(repo, item["issue"], {}, unset=["slot_hold"], phase="cancelled", found=found, labels=labels)
                applied.append(f"{repo}#{item['issue']}: cancelled")
            elif not found:
                add, remove = st.phase_label_changes(labels, "cancelled")
                if remove:
                    st.set_labels(repo, item["issue"], add, remove)
                    applied.append(f"{repo}#{item['issue']}: phase labels removed")
        for item in project["build"]:
            found = fresh(item["issue"])
            if not found or found[1].get("phase") != "approved":
                item["skipped"] = f"phase is {found[1].get('phase') if found else 'unknown'} now"
                continue
            changes = {"slot": "quick"} if item["slot"] == "quick" else {}
            unset = ["slot_hold"] + (["slot"] if item["slot"] != "quick" else [])
            if changes or any(k in found[1] for k in unset):
                st.patch(repo, item["issue"], changes, unset=unset, found=found)
                applied.append(f"{repo}#{item['issue']}: {item['slot']} slot recorded")
        held_at = stamp(datetime.now(timezone.utc))
        for item in project["hold"]:
            found = fresh(item["issue"])
            current = (found[1].get("slot_hold") or {}) if found else {}
            if found and sorted(current.get("for") or []) != item["for"]:
                st.patch(repo, item["issue"], {"slot_hold": {"for": item["for"], "at": held_at}}, found=found)
                applied.append(f"{repo}#{item['issue']}: slot_hold for {item['for']}")
        for number in project["clear_hold"]:
            found = fresh(number)
            if found and "slot_hold" in found[1]:
                st.patch(repo, number, {}, unset=["slot_hold"], found=found)
                applied.append(f"{repo}#{number}: slot_hold cleared")
    return applied


# ---------------------------------------------------------------- self-test

def fixture(number, phase="queued", labels=(), state="open", **status):
    pr = status.pop("_pr", None)
    comments = status.pop("_comments", [])
    labels = ["claude"] + list(labels)
    issue = {"number": number, "title": f"issue {number}", "url": "", "state": state, "labels": labels,
             "next": "claude:next" in labels, "paused": "claude:paused" in labels, "ping": "claude:ping" in labels,
             "status_comment_id": 1000 + number, "phase": phase,
             "status": ({"phase": phase, **status} if phase != "queued" else None),
             "unhandled_owner_issue_comments": [{"id": c} for c in comments]}
    if pr:
        issue["pr"] = pr
    return issue


def self_test():
    failures = []
    now = parse_time("2026-11-02T08:00:00Z")

    def check(name, got, want):
        ok = got == want
        print(f"{'ok  ' if ok else 'FAIL'} {name}")
        if not ok:
            print(f"     got  {got!r}\n     want {want!r}")
            failures.append(name)

    def run(issues, **settings):
        base = {"build_slots": 2, "small_slot": True, "quick_lane": True, "quick_slots": 1, "design_ahead": 3,
                "preview": "pages"}
        base.update(settings)
        config = {"defaults": base, "projects": [{"repo": "o/r"}]}
        return plan({"projects": [{"repo": "o/r", "issues": issues}]}, config, now)["projects"][0]

    est = {"S": {"size": "S"}, "M": {"size": "M"}, "L": {"size": "L"}}

    models = {"design": "fable", "build": "opus"}
    design_run = {"model": "fable", "effort": None, "stage": "design"}
    p = run([fixture(1, "approved", session="s1", run=design_run)], models=models)
    check("handoff: design-stage session, build model differs", p["build"][0].get("handoff"),
          {"model": "opus", "model_id": run_config.MODELS["opus"]})
    p = run([fixture(1, "approved", session="s1", run={"model": None, "effort": None})], models=models)
    check("no handoff: worker dispatched before stages (no run.stage)", "handoff" in p["build"][0], False)
    p = run([fixture(1, "approved", session="s1", run=design_run, lane="quick")], models=models)
    check("no handoff: quick lane builds in its design session",
          (p["build"][0]["slot"], "handoff" in p["build"][0]), ("quick", False))
    p = run([fixture(1, "approved", ["model:sonnet"], session="s1",
                     run={"model": "sonnet", "effort": None, "stage": "design"})], models=models)
    check("no handoff: model: label pins both stages", "handoff" in p["build"][0], False)
    p = run([fixture(1, "approved", session="s1", run={"model": None, "effort": None, "stage": "design"})])
    check("no handoff: no models configured", "handoff" in p["build"][0], False)
    p = run([fixture(1, "approved", ["model:haiku"], session="s1", run=design_run)], models=models)
    check("handoff: label added after dispatch is the build model", p["build"][0].get("handoff", {}).get("model"),
          "haiku")
    p = run([fixture(1, "approved", session="s1", run={"model": "opus", "effort": None, "stage": "build"})],
            models=models)
    check("no handoff: already a build-stage session", "handoff" in p["build"][0], False)

    p = run([fixture(1, "approved", session="s1", estimate=est["M"]), fixture(2, "approved", session="s2")])
    check("no holders: queue head builds without a check", [b["issue"] for b in p["build"]], [1])
    check("no holders: second slot needs a check against the new holder",
          [c["issue"] for c in (p["needs_parallel_check"] or {}).get("candidates", [])], [2])

    p = run([fixture(1, "building", session="s1"), fixture(2, "approved", session="s2", estimate=est["M"])])
    check("holder + unchecked candidate: check needed, no build",
          (p["build"], [c["issue"] for c in p["needs_parallel_check"]["candidates"]]), ([], [2]))

    p = run([fixture(1, "building", session="s1"),
             fixture(2, "approved", session="s2", estimate=est["M"], parallel={"with": [1], "ok": True})])
    check("valid ok verdict: build", [b["issue"] for b in p["build"]], [2])

    p = run([fixture(1, "building", session="s1", _pr={"number": 9, "head_sha": "new"}),
             fixture(2, "approved", session="s2", estimate=est["M"],
                     parallel={"with": [1], "ok": True, "heads": {"1": "old"}})])
    check("holder pushed since the check: stale, re-check", (p["build"], bool(p["needs_parallel_check"])), ([], True))

    p = run([fixture(1, "building", session="s1"), fixture(3, "building", session="s3"),
             fixture(2, "approved", session="s2", parallel={"with": [1], "ok": True})], build_slots=3)
    check("holder set changed: stale", bool(p["needs_parallel_check"]), True)

    p = run([fixture(1, "building", session="s1"),
             fixture(2, "approved", session="s2", estimate=est["M"], parallel={"with": [1], "ok": False}),
             fixture(3, "approved", session="s3", estimate=est["M"], parallel={"with": [1], "ok": True})])
    check("not-safe head is skipped for a cleared later candidate", [b["issue"] for b in p["build"]], [3])

    p = run([fixture(1, "building", session="s1"),
             fixture(2, "approved", session="s2", estimate=est["M"]),
             fixture(3, "approved", session="s3", estimate=est["M"], parallel={"with": [1], "ok": True})])
    check("unchecked earlier candidate blocks a later cleared one this tick",
          (p["build"], [c["issue"] for c in p["needs_parallel_check"]["candidates"]]), ([], [2]))

    p = run([fixture(1, "building", session="s1"),
             fixture(2, "approved", session="s2", estimate=est["L"], parallel={"with": [1], "ok": True}),
             fixture(3, "designing", session="s3", estimate=est["S"])])
    check("small-issue rule: last slot held from L while S designs",
          (p["build"], p["hold"]), ([], [{"issue": 2, "for": [3]}]))

    p = run([fixture(1, "building", session="s1"),
             fixture(2, "approved", labels=["claude:next"], session="s2", estimate=est["L"],
                     parallel={"with": [1], "ok": True}),
             fixture(3, "designing", session="s3", estimate=est["S"])])
    check("claude:next overrides the small-issue rule", [b["issue"] for b in p["build"]], [2])

    p = run([fixture(1, "building", session="s1"),
             fixture(2, "approved", session="s2", estimate=est["L"], parallel={"with": [1], "ok": True}),
             fixture(3, "needs-input", session="s3", estimate=est["S"])])
    check("needs-input doesn't count as waiting", [b["issue"] for b in p["build"]], [2])

    p = run([fixture(1, "building", session="s1"),
             fixture(2, "approved", session="s2", estimate=est["L"], parallel={"with": [1], "ok": True}),
             fixture(3, "designing", session="s3", estimate=est["S"])], build_slots=1)
    check("build_slots 1: no reservation, but slot is full", (p["build"], p["hold"]), ([], []))

    p = run([fixture(2, "approved", session="s2", estimate=est["L"], slot_hold={"for": [3]}),
             fixture(3, "designing", session="s3", estimate=est["S"])])
    check("two free slots: L builds, old hold cleared", ([b["issue"] for b in p["build"]], p["clear_hold"]), ([2], [2]))

    p = run([fixture(1, "building", session="s1"), fixture(4, "building", session="s4"),
             fixture(5, "approved", session="s5", lane="quick", parallel={"with": [1, 4], "ok": True})])
    check("quick candidate takes the quick slot while regular slots are full",
          [(b["issue"], b["slot"]) for b in p["build"]], [(5, "quick")])

    p = run([fixture(1, "building", session="s1"), fixture(4, "in-review", session="s4", slot="quick"),
             fixture(5, "approved", session="s5", lane="quick", estimate=est["S"],
                     parallel={"with": [1, 4], "ok": True})])
    check("quick slot taken: quick candidate takes a free regular slot",
          [(b["issue"], b["slot"]) for b in p["build"]], [(5, "regular")])
    check("slots reported", p["slots"], {"regular": "1/2", "quick": "1/1"})

    p = run([fixture(5, "approved", session="s5", labels=["lane:quick"])], preview="deploy", build_slots=1)
    check("preview: deploy has no quick slot", ([(b["issue"], b["slot"]) for b in p["build"]], "quick" in p["slots"]),
          ([(5, "regular")], False))
    p = run([fixture(5, "approved", session="s5", labels=["lane:quick"])], preview="cloudflare",
            cloudflare_project="o-previews", build_slots=1)
    check("preview: cloudflare keeps the quick slot", ([(b["issue"], b["slot"]) for b in p["build"]], "quick" in p["slots"]),
          ([(5, "quick")], True))

    p = run([fixture(1, "in-review", session="s1", _pr={"number": 9, "merged": True}),
             fixture(2, "building", session="s2"), fixture(3, "in-review", session="s3"),
             fixture(4, "needs-input", session="s4"),
             fixture(6, "approved", session="s6", estimate=est["M"], parallel={"with": [2, 3], "ok": True})])
    check("merge: finalize + SYNC to the other holders",
          ([f["issue"] for f in p["finalize"]], [s["issue"] for s in p["sync"]]), ([1], [2, 3]))
    check("merged holder no longer holds a slot", p["slots"]["regular"], "2/2")

    p = run([fixture(7, "designing", state="closed", session="s7")])
    check("closed without merge: cancel", [c["issue"] for c in p["cancel"]], [7])

    p = run([fixture(8, "needs-input", session="s8", _comments=[55]), fixture(9, "designing", session="s9",
             labels=["claude:approved"]), fixture(10, "blocked", session="s10", _comments=[56])])
    check("nudge on owner comment and approved label",
          [(n["issue"], n["why"]) for n in p["nudge"]], [(8, "owner comment"), (9, "claude:approved label")])
    check("blocked + owner comment: retry candidate", [r["issue"] for r in p["retry"]], [10])

    p = run([fixture(11, "designing", labels=["claude:paused"], session="s11", _comments=[57]),
             fixture(12), fixture(13), fixture(14, labels=["claude:ping"]), fixture(15)], design_ahead=2)
    check("paused skipped but counts toward design_ahead; ping on queued dispatches anyway",
          (p["nudge"], p["dispatch"]), ([], [12, 14]))

    p = run([fixture(16, "dispatching")])
    check("dispatching with no session: adopt", p["adopt"], [{"issue": 16}])

    p = run([fixture(17, "building", session="s17", progress={"stage": "building", "at": "2026-11-02T07:00Z"}),
             fixture(18, "building", session="s18", updated="2026-11-02T09:00Z")])
    check("stalled and future timestamps flagged", [s["issue"] for s in p["stalled"]], [17, 18])

    snapshot = {"projects": [{"repo": "o/a", "issues": [fixture(1, "building", session="s1")]},
                             {"repo": "o/b", "issues": [fixture(2, "approved", session="s2", estimate=est["M"],
                                                                parallel={"with": [1], "ok": True})]}]}
    config = {"build_scope": "global", "defaults": {"build_slots": 2},
              "projects": [{"repo": "o/a"}, {"repo": "o/b"}]}
    result = plan(snapshot, config, now)
    check("build_scope global pools slots across projects",
          [(p["repo"], [b["issue"] for b in p["build"]]) for p in result["projects"]], [("o/a", []), ("o/b", [2])])

    before = copy.deepcopy(snapshot)
    plan(snapshot, config, now)
    check("plan doesn't modify the snapshot", snapshot, before)

    print(f"\n{'all passed' if not failures else str(len(failures)) + ' FAILED'}")
    return 1 if failures else 0


def main(argv):
    if "--test" in argv:
        return self_test()
    now = None
    if "--now" in argv:
        now = parse_time(argv[argv.index("--now") + 1])
    paths = [a for i, a in enumerate(argv) if not a.startswith("--") and (i == 0 or argv[i - 1] != "--now")]
    if paths:
        snapshot = json.load(open(paths[0]))
    else:
        out = subprocess.run([sys.executable, "-I", os.path.join(HERE, "gather.py"), os.path.join(ROOT, "bot.config.yml")],
                             capture_output=True, text=True, check=True).stdout
        snapshot = json.loads(out)
    result = plan(snapshot, load_config(), now)
    if "--apply" in argv:
        result["applied"] = apply(result, snapshot)
    json.dump(result, sys.stdout, indent=1)
    print()
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main(sys.argv[1:]))
    except RuntimeError as error:
        print(f"error: {error}", file=sys.stderr)
        sys.exit(1)
