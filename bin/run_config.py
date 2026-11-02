#!/usr/bin/env python3
"""Pick the model (and reasoning effort) for a worker session from issue labels.

The orchestrator runs this at dispatch time, on the labels the issue carries
right then, and passes the result to create_session. Labels (set by the factory
app's new-issue form, or by hand):

  model:<alias>   alias one of the keys of MODELS below
  effort:<level>  level one of EFFORTS below

No label of a kind means the default (create_session's own default, i.e. what
dispatch did before these labels existed). More than one label of a kind, or a
value not in the list, means that kind is ignored (default used) and a note
says why.

MODELS is the single alias -> model ID table: when a new model version ships,
change its line here. Labels hold aliases, never IDs.

Usage:
  bin/run_config.py [LABEL ...]       resolve these labels (e.g. claude model:opus)
  bin/run_config.py --issue o/r#n     fetch the issue's labels with `gh api` first
  bin/run_config.py --test            run the self-test

Prints JSON:
  {"run": {"model": "opus", "effort": null},   -> the status JSON's "run"
   "model_id": "claude-opus-5-5",              -> create_session's `model` (omit if null)
   "effort": null,                             -> create_session's effort (omit if null)
   "ignored": [],                              -> kinds ignored (also in run.ignored if any)
   "notes": ["..."]}                           -> lines for the status comment
"""
import json
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


def resolve(labels):
    """Resolve a list of label names into the run config. Pure: no caching."""
    labels = list(labels)
    model, model_note = pick(labels, "model")
    effort, effort_note = pick(labels, "effort")
    notes = [n for n in (model_note, effort_note) if n]
    ignored = [kind for kind, note in (("model", model_note), ("effort", effort_note)) if note]
    if effort and not EFFORT_SUPPORTED:
        notes.append(f"`effort:{effort}` not applied: the session API has no reasoning-effort setting yet, "
                     "so the default effort is used.")
        effort = None
    run = {"model": model, "effort": effort}
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

    print(f"\n{'all passed' if not failures else str(len(failures)) + ' FAILED'}")
    return 1 if failures else 0


def main(argv):
    if "--test" in argv:
        return self_test()
    if argv[:1] == ["--issue"]:
        labels = issue_labels(argv[1])
    else:
        labels = argv
    print(json.dumps(resolve(labels)))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
