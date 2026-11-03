#!/usr/bin/env python3
"""Pick the model (and reasoning effort) for a worker session from issue labels.

The orchestrator runs this at dispatch time, on the labels the issue carries
right then, and passes the result to create_session. Labels (set by the factory
app's new-issue form, or by hand):

  model:<alias>   alias one of the keys of MODELS below
  effort:<level>  level one of EFFORTS below

No label of a kind means the default. For the model that's the per-stage
default from bot.config.yml (`models: {design: <alias>, build: <alias>}`, in
`defaults:` or a project), and with none configured, create_session's own
default (what dispatch did before these labels existed). More than one label of
a kind, or a value not in the list, means that kind is ignored (default used)
and a note says why.

Stages: a worker session is started for the `design` stage (triage, design
round) and, when the build model differs from the design session's model, a
fresh session is started for the `build` stage at BUILD (bin/plan.py marks it
`handoff`). A `model:` label pins both stages to one model, so no hand-off.
Quick-lane issues are built in their design session, so no hand-off either.

MODELS is the single alias -> model ID table: when a new model version ships,
change its line here. Labels hold aliases, never IDs.

Usage:
  bin/run_config.py [--stage S] [--repo o/r] [LABEL ...]
                                      resolve these labels (e.g. claude model:opus);
                                      --repo applies that project's `models:` from
                                      bot.config.yml (without it, `defaults:`'s)
  bin/run_config.py [--stage S] --issue o/r#n
                                      fetch the issue's labels with `gh api` first
  bin/run_config.py --orchestrator [--current MODEL]
                                      the orchestrator's own models from
                                      bot.config.yml's `orchestrator:` block; with
                                      --current (this session's model, from
                                      get_session), whether it matches
  bin/run_config.py --test            run the self-test

Prints JSON:
  {"run": {"model": "opus", "effort": null, "stage": "design"},
                                              -> the status JSON's "run" (stage only
                                                 when one was given)
   "model_id": "claude-opus-5-5",              -> create_session's `model` (omit if null)
   "effort": null,                             -> create_session's effort (omit if null)
   "ignored": [],                              -> kinds ignored (also in run.ignored if any)
   "notes": ["..."]}                           -> lines for the status comment

--orchestrator prints:
  {"model": "sonnet", "model_id": "claude-sonnet-5-5",   -> what this session should run on
   "subagents": "opus",                                    -> Agent tool `model` for subagents
   "current": "claude-opus-5-5", "matches": false,         -> only with --current
   "notes": ["..."]}                                       -> a mismatch reminder for the report
Missing block or unknown alias: that key is null (no check / Agent default).
"""
import json
import os
import subprocess
import sys

# The one alias -> model ID table. New model version: change one line.
MODELS = {
    "opus": "claude-opus-5-5",
    "sonnet": "claude-sonnet-5-5",
    "haiku": "claude-haiku-5-5",
    "fable": "claude-fable-5-1",
}

EFFORTS = ("low", "medium", "high", "max")

# create_session has no reasoning-effort parameter yet (it takes `model` only),
# and the issue asks not to fake effort through prompt text. Flip this to True
# once the session API accepts an effort, and pass "effort" through in the tick.
EFFORT_SUPPORTED = False

KINDS = {"model": tuple(MODELS), "effort": EFFORTS}

STAGES = ("design", "build")
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def stage_models(config, repo=None):
    """The `models:` mapping that applies to a project ({} if none): the
    project's own, else `defaults:`'s. Unknown stages/aliases are dropped."""
    config = config or {}
    found = (config.get("defaults") or {}).get("models")
    for project in config.get("projects") or []:
        if repo and project.get("repo") == repo and "models" in project:
            found = project["models"]
    if not isinstance(found, dict):
        return {}
    return {stage: alias for stage, alias in found.items() if stage in STAGES and alias in MODELS}


def load_stage_models(repo=None):
    import yaml
    return stage_models(yaml.safe_load(open(os.path.join(ROOT, "bot.config.yml"))), repo)


ORCHESTRATOR_KEYS = ("model", "subagents")


def orchestrator_models(config):
    """{"model": alias|None, "subagents": alias|None} from the `orchestrator:`
    block. A missing block, key, or unknown alias gives None for that key."""
    block = (config or {}).get("orchestrator")
    if not isinstance(block, dict):
        block = {}
    return {key: block.get(key) if block.get(key) in MODELS else None for key in ORCHESTRATOR_KEYS}


def family(model):
    """The alias a model string belongs to ('claude-opus-5-5[1m]' -> 'opus'),
    or None. Accepts aliases, IDs, and IDs with a context-window suffix."""
    name = (model or "").split("[", 1)[0].strip().lower()
    for alias in MODELS:
        if name == alias or alias in name.split("-"):
            return alias
    return None


def orchestrator_check(config, current=None):
    """The --orchestrator output for this config and (optionally) the model this
    session runs on now."""
    models = orchestrator_models(config)
    want = models["model"]
    out = {"model": want, "model_id": MODELS[want] if want else None,
           "subagents": models["subagents"], "notes": []}
    if current is not None:
        out["current"] = current
        have = family(current)
        out["matches"] = want is None or have == want
        if not out["matches"]:
            out["notes"].append(
                f"⚠️ Orchestrator runs on `{current}`, config says `{want}` ({MODELS[want]}). "
                f"Switch with `/model {want}` in this session, or start a new session on "
                f"{MODELS[want]} and run /bot-setup there (it moves the heartbeat).")
    return out


def pick(labels, kind):
    """Return (value or None, note or None) for one label kind."""
    prefix = kind + ":"
    values = [label[len(prefix):] for label in labels if label.startswith(prefix)]
    if not values:
        return None, None
    shown = ", ".join(f"`{kind}:{v}`" for v in values)
    if len(values) > 1:
        return None, f"Ignored {shown}: more than one `{kind}:` label, so the default {kind} is used."
    if values[0] not in KINDS[kind]:
        allowed = ", ".join(KINDS[kind])
        return None, f"Ignored {shown}: not one of {allowed}, so the default {kind} is used."
    return values[0], None


def resolve(labels, stage=None, models=None):
    """Resolve a list of label names into the run config. Pure: no caching.

    stage: "design" / "build" / None; models: the project's stage -> alias
    defaults (stage_models()), used when no valid model: label is set."""
    labels = list(labels)
    model, model_note = pick(labels, "model")
    if model is None and stage:
        model = (models or {}).get(stage)
    effort, effort_note = pick(labels, "effort")
    notes = [n for n in (model_note, effort_note) if n]
    ignored = [kind for kind, note in (("model", model_note), ("effort", effort_note)) if note]
    if effort and not EFFORT_SUPPORTED:
        notes.append(f"`effort:{effort}` not applied: the session API has no reasoning-effort setting yet, "
                     "so the default effort is used.")
        effort = None
    run = {"model": model, "effort": effort}
    if stage:
        run["stage"] = stage
    if ignored:
        run["ignored"] = ignored
    return {
        "run": run,
        "model_id": MODELS[model] if model else None,
        "effort": effort,
        "ignored": ignored,
        "notes": notes,
    }


def issue_labels(ref):
    repo, number = ref.split("#", 1)
    out = subprocess.run(["gh", "api", f"repos/{repo}/issues/{number}"],
                         capture_output=True, text=True, check=True).stdout
    return [label["name"] for label in json.loads(out)["labels"]]


def self_test():
    global EFFORT_SUPPORTED
    failures = []

    def check(name, got, want):
        ok = got == want
        print(f"{'ok  ' if ok else 'FAIL'} {name}")
        if not ok:
            print(f"     got  {got!r}\n     want {want!r}")
            failures.append(name)

    base = ["claude", "claude:designing", "bug"]

    r = resolve(base)
    check("no labels: default model and effort", r["run"], {"model": None, "effort": None})
    check("no labels: no create_session model, no notes", (r["model_id"], r["notes"]), (None, []))

    for alias, model_id in MODELS.items():
        r = resolve(base + [f"model:{alias}"])
        check(f"model only: model:{alias} -> {model_id}", (r["run"], r["model_id"], r["notes"]),
              ({"model": alias, "effort": None}, model_id, []))

    for level in EFFORTS:
        r = resolve(base + [f"effort:{level}"])
        check(f"effort only (unsupported): effort:{level} validated, default used, noted",
              (r["run"], len(r["notes"]), "not applied" in r["notes"][0]),
              ({"model": None, "effort": None}, 1, True))

    r = resolve(base + ["model:sonnet", "effort:low"])
    check("both (unsupported effort): model applied, effort noted",
          (r["run"], r["model_id"], len(r["notes"])), ({"model": "sonnet", "effort": None}, "claude-sonnet-5-5", 1))

    EFFORT_SUPPORTED = True
    try:
        r = resolve(base + ["effort:max"])
        check("effort only (supported): passed through", (r["run"], r["effort"], r["notes"]),
              ({"model": None, "effort": "max"}, "max", []))
        r = resolve(base + ["model:haiku", "effort:medium"])
        check("both (supported)", (r["run"], r["model_id"], r["effort"], r["notes"]),
              ({"model": "haiku", "effort": "medium"}, "claude-haiku-5-5", "medium", []))
        r = resolve(base + ["model:opus", "effort:high", "effort:low"])
        check("duplicate effort (supported): effort ignored, model kept", (r["run"], r["effort"]),
              ({"model": "opus", "effort": None, "ignored": ["effort"]}, None))
    finally:
        EFFORT_SUPPORTED = False

    r = resolve(base + ["model:opus", "model:haiku", "effort:high"])
    check("duplicate model: model ignored, named in a note",
          (r["run"]["model"], r["model_id"], r["ignored"], "`model:opus`, `model:haiku`" in r["notes"][0]),
          (None, None, ["model"], True))

    r = resolve(base + ["model:opus", "model:opus"])
    check("same model label twice still counts as duplicate", r["ignored"], ["model"])

    r = resolve(base + ["model:gpt-4", "effort:high"])
    check("unknown model: ignored with a note", (r["run"]["model"], r["ignored"], "not one of" in r["notes"][0]),
          (None, ["model"], True))

    r = resolve(base + ["model:claude-opus-5-5"])
    check("model ID instead of alias: ignored", (r["run"]["model"], r["ignored"]), (None, ["model"]))

    r = resolve(base + ["model:Opus"])
    check("aliases are case-sensitive: model:Opus ignored", r["ignored"], ["model"])

    r = resolve(base + ["effort:extreme", "model:fable"])
    check("unknown effort: ignored, model kept", (r["run"], r["model_id"]),
          ({"model": "fable", "effort": None, "ignored": ["effort"]}, "claude-fable-5-1"))

    r = resolve(base + ["model:", "effort:"])
    check("empty values: both ignored", r["ignored"], ["model", "effort"])

    r = resolve(["claude:model:opus", "xmodel:opus", "models:opus"])
    check("only exact model:/effort: prefixes count", (r["run"], r["notes"]), ({"model": None, "effort": None}, []))

    # Labels added after dispatch: the run is resolved from the labels at the
    # moment of dispatch and recorded; a later label change only shows up when
    # a new session is dispatched (the tick never restarts a running worker
    # for a label change).
    at_dispatch = resolve(base)
    later_labels = base + ["model:haiku"]
    check("labels added after dispatch: recorded run unchanged", at_dispatch["run"], {"model": None, "effort": None})
    check("labels added after dispatch: next dispatch uses them", resolve(later_labels)["model_id"], "claude-haiku-5-5")

    models = {"design": "fable", "build": "opus"}
    r = resolve(base, "design", models)
    check("stage default: design -> fable", (r["run"], r["model_id"], r["notes"]),
          ({"model": "fable", "effort": None, "stage": "design"}, "claude-fable-5-1", []))
    r = resolve(base, "build", models)
    check("stage default: build -> opus", (r["run"], r["model_id"]),
          ({"model": "opus", "effort": None, "stage": "build"}, "claude-opus-5-5"))
    r = resolve(base + ["model:sonnet"], "design", models)
    check("label beats stage default (design)", r["model_id"], "claude-sonnet-5-5")
    r = resolve(base + ["model:sonnet"], "build", models)
    check("label beats stage default (build)", r["model_id"], "claude-sonnet-5-5")
    r = resolve(base + ["model:gpt-4"], "build", models)
    check("invalid label: stage default used, noted", (r["run"]["model"], r["ignored"]), ("opus", ["model"]))
    r = resolve(base, "build", {"design": "fable"})
    check("stage without a default: create_session default", (r["run"], r["model_id"]),
          ({"model": None, "effort": None, "stage": "build"}, None))
    r = resolve(base, "design", None)
    check("no models config: as before, stage recorded", (r["run"], r["model_id"]),
          ({"model": None, "effort": None, "stage": "design"}, None))

    cfg = {"defaults": {"models": {"design": "fable", "build": "opus"}},
           "projects": [{"repo": "o/a"}, {"repo": "o/b", "models": {"build": "sonnet"}},
                        {"repo": "o/c", "models": None}]}
    check("stage_models: defaults apply", stage_models(cfg, "o/a"), {"design": "fable", "build": "opus"})
    check("stage_models: project overrides whole mapping", stage_models(cfg, "o/b"), {"build": "sonnet"})
    check("stage_models: project models: null turns it off", stage_models(cfg, "o/c"), {})
    check("stage_models: unknown aliases/stages dropped",
          stage_models({"defaults": {"models": {"design": "gpt", "build": "opus", "x": "opus"}}}), {"build": "opus"})
    check("stage_models: no config", stage_models({}), {})

    check("orchestrator_models: configured", orchestrator_models({"orchestrator": {"model": "sonnet", "subagents": "opus"}}),
          {"model": "sonnet", "subagents": "opus"})
    check("orchestrator_models: no block", orchestrator_models({}), {"model": None, "subagents": None})
    check("orchestrator_models: block null", orchestrator_models({"orchestrator": None}), {"model": None, "subagents": None})
    check("orchestrator_models: unknown alias dropped",
          orchestrator_models({"orchestrator": {"model": "claude-sonnet-5-5", "subagents": "opus"}}),
          {"model": None, "subagents": "opus"})
    for name, want in (("claude-opus-5-5", "opus"), ("claude-opus-5-5[1m]", "opus"), ("opus", "opus"),
                       ("claude-sonnet-5-5", "sonnet"), ("claude-opus-4-8[1m]", "opus"),
                       ("claude-fable-5-1", "fable"), ("Claude-Haiku-5-5", "haiku"), ("gpt-4", None),
                       ("", None), (None, None)):
        check(f"family({name!r}) -> {want}", family(name), want)
    ocfg = {"orchestrator": {"model": "sonnet", "subagents": "opus"}}
    r = orchestrator_check(ocfg)
    check("orchestrator_check: no --current, no verdict",
          r, {"model": "sonnet", "model_id": "claude-sonnet-5-5", "subagents": "opus", "notes": []})
    r = orchestrator_check(ocfg, "claude-sonnet-5-5")
    check("orchestrator_check: match", (r["matches"], r["notes"]), (True, []))
    r = orchestrator_check(ocfg, "claude-opus-5-5[1m]")
    check("orchestrator_check: mismatch noted with the fix",
          (r["matches"], len(r["notes"]), "/model sonnet" in r["notes"][0]), (False, 1, True))
    r = orchestrator_check({}, "claude-opus-5-5")
    check("orchestrator_check: nothing configured always matches",
          (r["model"], r["subagents"], r["matches"], r["notes"]), (None, None, True, []))

    print(f"\n{'all passed' if not failures else str(len(failures)) + ' FAILED'}")
    return 1 if failures else 0


def main(argv):
    if "--test" in argv:
        return self_test()
    if argv[:1] == ["--orchestrator"]:
        import yaml
        current = None
        if argv[1:2] == ["--current"]:
            if len(argv) < 3:
                raise SystemExit("--current needs a model (this session's, from get_session)")
            current = argv[2]
        config = yaml.safe_load(open(os.path.join(ROOT, "bot.config.yml")))
        print(json.dumps(orchestrator_check(config, current)))
        return 0
    stage = repo = None
    argv = list(argv)
    while argv[:1] and argv[0] in ("--stage", "--repo"):
        if argv[0] == "--stage":
            stage = argv[1]
            if stage not in STAGES:
                raise SystemExit(f"--stage must be one of {', '.join(STAGES)}")
        else:
            repo = argv[1]
        argv = argv[2:]
    if argv[:1] == ["--issue"]:
        repo = repo or argv[1].split("#", 1)[0]
        labels = issue_labels(argv[1])
    else:
        labels = argv
    models = load_stage_models(repo) if stage else {}
    print(json.dumps(resolve(labels, stage, models)))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
