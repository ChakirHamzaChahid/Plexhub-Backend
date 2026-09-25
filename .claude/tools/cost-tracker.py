#!/usr/bin/env python3
"""cost-tracker — suivi des tokens consommés par session Claude Code.

Adapté de affaan-m/ecc (MIT) — scripts/hooks/cost-tracker.js (dédoublonnage
par message.id, lecture du transcript passé au hook Stop). Réécrit en Python et
étendu : ventilation session principale / sous-agents, par workflow et par
type d'agent. Aucun tarif codé en dur : on mesure des tokens, pas des dollars.

Usage :
  python .claude/tools/cost-tracker.py hook            # hook Stop (stdin JSON)
  python .claude/tools/cost-tracker.py scan <t.jsonl>  # rejoue un transcript, affiche la ligne
  python .claude/tools/cost-tracker.py report [--weeks N] [--session ID]

Sortie : .claude/.cache/costs.jsonl — UNE ligne par session (réécrite à chaque
Stop, valeurs cumulées). Le hook n'échoue jamais (exit 0, silencieux).

Sources de données (format transcript Claude Code, constaté le 2026-09-25) :
  <projets>/<slug>/<session>.jsonl                      session principale
  <projets>/<slug>/<session>/subagents/agent-<id>.jsonl sous-agents
  <projets>/<slug>/<session>/subagents/agent-<id>.meta.json  {agentType, model, toolUseId}
Un même message API (message.id) est écrit sur plusieurs lignes qui répètent le
même usage : on le compte une seule fois.
"""
import datetime as dt
import glob
import json
import os
import sys

FIELDS = ("in", "out", "cw", "cr")
FREE = "(hors workflow)"


def _root():
    return os.environ.get("CLAUDE_PROJECT_DIR") or os.getcwd()


def _out_path():
    return os.path.join(_root(), ".claude", ".cache", "costs.jsonl")


def _num(v):
    try:
        n = int(v)
        return n if n > 0 else 0
    except (TypeError, ValueError):
        return 0


def _empty():
    return {"in": 0, "out": 0, "cw": 0, "cr": 0, "n": 0}


def _add(acc, u):
    acc["in"] += _num(u.get("input_tokens"))
    acc["out"] += _num(u.get("output_tokens"))
    acc["cw"] += _num(u.get("cache_creation_input_tokens"))
    acc["cr"] += _num(u.get("cache_read_input_tokens"))
    acc["n"] += 1


def _merge(dst, src):
    for k in ("in", "out", "cw", "cr", "n"):
        dst[k] = dst.get(k, 0) + src.get(k, 0)


def _read_messages(path):
    """Retourne (messages, meta) : messages = {id: (usage, model, skill, tool_use_ids)}."""
    msgs, order, synth = {}, [], 0
    meta = {"first_ts": None, "branch": None}
    try:
        fh = open(path, encoding="utf-8", errors="replace")
    except OSError:
        return {}, meta
    with fh:
        for line in fh:
            if '"assistant"' not in line:
                continue
            try:
                e = json.loads(line)
            except ValueError:
                continue
            if e.get("type") != "assistant":
                continue
            m = e.get("message") or {}
            u = m.get("usage")
            if not u:
                continue
            meta["first_ts"] = meta["first_ts"] or e.get("timestamp")
            meta["branch"] = e.get("gitBranch") or meta["branch"]
            mid = m.get("id")
            if not isinstance(mid, str) or not mid:
                synth += 1
                mid = "__line_%d" % synth
            tools = [c.get("id") for c in (m.get("content") or [])
                     if isinstance(c, dict) and c.get("type") == "tool_use"]
            prev = msgs.get(mid)
            if prev is None:
                order.append(mid)
                tools_all = tools
            else:
                tools_all = prev[3] + tools
            msgs[mid] = (u, m.get("model") or "unknown",
                         e.get("attributionSkill") or (prev[2] if prev else None), tools_all)
    return {k: msgs[k] for k in order}, meta


def summarize(transcript_path, session_id=None):
    session_id = session_id or os.path.basename(transcript_path)[:-len(".jsonl")]
    msgs, meta = _read_messages(transcript_path)
    main_by_model, by_wf, tool_skill = {}, {}, {}
    for u, model, skill, tools in msgs.values():
        _add(main_by_model.setdefault(model, _empty()), u)
        _add(by_wf.setdefault(skill or FREE, _empty()), u)
        for t in tools:
            if t:
                tool_skill[t] = skill or FREE
    subs = {}
    sub_dir = os.path.join(os.path.dirname(transcript_path), session_id, "subagents")
    for f in sorted(glob.glob(os.path.join(sub_dir, "agent-*.jsonl"))):
        info = {}
        try:
            with open(f[:-len(".jsonl")] + ".meta.json", encoding="utf-8") as mf:
                info = json.load(mf)
        except (OSError, ValueError):
            pass
        smsgs, _ = _read_messages(f)
        agent = info.get("agentType") or "?"
        wf = tool_skill.get(info.get("toolUseId"), FREE)
        for u, model, _skill, _t in smsgs.values():
            _add(subs.setdefault("%s|%s" % (agent, model), _empty()), u)
            _add(by_wf.setdefault(wf, _empty()), u)
    tot_main, tot_sub = _empty(), _empty()
    for v in main_by_model.values():
        _merge(tot_main, v)
    for v in subs.values():
        _merge(tot_sub, v)
    first = meta["first_ts"] or dt.datetime.now(dt.timezone.utc).isoformat()
    try:
        d = dt.datetime.fromisoformat(first.replace("Z", "+00:00"))
    except ValueError:
        d = dt.datetime.now(dt.timezone.utc)
    iso = d.isocalendar()
    return {
        "ts": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
        "session_id": session_id,
        "started": first,
        "week": "%d-W%02d" % (iso[0], iso[1]),
        "branch": meta["branch"],
        "main": main_by_model,
        "subagents": subs,
        "by_workflow": by_wf,
        "totals": {"main": tot_main, "sub": tot_sub},
    }


def _store(row):
    path = _out_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    rows = []
    try:
        with open(path, encoding="utf-8") as fh:
            for line in fh:
                try:
                    r = json.loads(line)
                except ValueError:
                    continue
                if r.get("session_id") != row["session_id"]:
                    rows.append(r)
    except OSError:
        pass
    rows.append(row)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        for r in rows:
            fh.write(json.dumps(r, ensure_ascii=False, sort_keys=True) + "\n")
    os.replace(tmp, path)


def cmd_hook():
    try:
        raw = sys.stdin.read()
        data = json.loads(raw) if raw.strip() else {}
        tp = data.get("transcript_path")
        if tp and os.path.isfile(tp):
            _store(summarize(tp, data.get("session_id")))
    except Exception:  # le hook ne doit jamais bloquer ni bruiter
        pass
    return 0


def _fmt(n):
    return "{:,}".format(n).replace(",", " ")


def _line(label, a, width=34):
    return "  %-*s in %10s  out %9s  cw %11s  cr %13s  msg %5s" % (
        width, label[:width], _fmt(a["in"]), _fmt(a["out"]), _fmt(a["cw"]),
        _fmt(a["cr"]), _fmt(a["n"]))


def cmd_report(argv):
    weeks, only = 4, None
    if "--weeks" in argv:
        weeks = int(argv[argv.index("--weeks") + 1])
    if "--session" in argv:
        only = argv[argv.index("--session") + 1]
    try:
        with open(_out_path(), encoding="utf-8") as fh:
            rows = [json.loads(x) for x in fh if x.strip()]
    except OSError:
        print("Aucune donnée : %s absent (le hook Stop n'a pas encore tourné)." % _out_path())
        return 0
    if only:
        rows = [r for r in rows if r["session_id"].startswith(only)]
    by_week = {}
    for r in rows:
        by_week.setdefault(r["week"], []).append(r)
    for wk in sorted(by_week)[-weeks:]:
        rs = by_week[wk]
        m, s, wf, ag, mo = _empty(), _empty(), {}, {}, {}
        for r in rs:
            _merge(m, r["totals"]["main"])
            _merge(s, r["totals"]["sub"])
            for k, v in r["by_workflow"].items():
                _merge(wf.setdefault(k, _empty()), v)
            for k, v in r["subagents"].items():
                _merge(ag.setdefault(k.split("|")[0], _empty()), v)
            for k, v in r["main"].items():
                _merge(mo.setdefault("principal " + k, _empty()), v)
            for k, v in r["subagents"].items():
                _merge(mo.setdefault("sous-agent " + k.split("|", 1)[1], _empty()), v)
        print("== Semaine %s — %d session(s)" % (wk, len(rs)))
        print(_line("session principale", m))
        print(_line("sous-agents", s))
        for title, d in (("par workflow", wf), ("par type d'agent", ag), ("par modèle", mo)):
            print(" %s :" % title)
            for k, v in sorted(d.items(), key=lambda kv: -(kv[1]["out"] + kv[1]["in"] + kv[1]["cw"])):
                print(_line(k, v))
        print()
    print("in = input non caché, out = output, cw = écriture cache, cr = lecture cache.")
    return 0


def main(argv):
    cmd = argv[0] if argv else "report"
    if cmd == "hook":
        return cmd_hook()
    if cmd == "scan":
        print(json.dumps(summarize(argv[1]), ensure_ascii=False, indent=1))
        return 0
    if cmd == "report":
        return cmd_report(argv[1:])
    print(__doc__)
    return 2


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.exit(main(sys.argv[1:]))
