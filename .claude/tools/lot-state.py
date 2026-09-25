#!/usr/bin/env python3
"""lot-state — survivre à la compaction de contexte.

Adapté de affaan-m/ecc (MIT) — scripts/hooks/pre-compact.js (sauvegarde d'un
état avant compaction, réinjection au démarrage suivant). Réécrit en Python,
sans appel LLM : l'état est lu dans git, dans le dod-gate et dans le
transcript (lignes ROUTAGE), donc déterministe et gratuit.

  python .claude/tools/lot-state.py save     # hook PreCompact (stdin JSON)
  python .claude/tools/lot-state.py inject   # hook SessionStart (stdin JSON)
  python .claude/tools/lot-state.py show     # affiche l'état sauvegardé

Fichier : .claude/.cache/lot-state.md (gitignoré). Réinjection complète après
une compaction ou une reprise (`compact`, `resume`) si l'état a moins de 24 h ;
au démarrage d'une session neuve (`startup`), une seule ligne qui le signale.
Les hooks n'échouent jamais (exit 0).
"""
import datetime as dt
import json
import os
import re
import subprocess
import sys
import time

MAX_AGE_S = 24 * 3600
TAIL_BYTES = 3 * 1024 * 1024
ROUTAGE_RE = re.compile(r"^\s*[`*>-]*\s*(ROUTAGE\b[^\n]{0,300})", re.M)


def _root():
    return os.environ.get("CLAUDE_PROJECT_DIR") or os.getcwd()


def _state_path():
    return os.path.join(_root(), ".claude", ".cache", "lot-state.md")


def _run(cmd, timeout=15):
    try:
        r = subprocess.run(cmd, cwd=_root(), capture_output=True, timeout=timeout)
        return r.stdout.decode("utf-8", "replace").rstrip()
    except Exception:
        return ""


def _stdin_json():
    try:
        raw = sys.stdin.buffer.read().decode("utf-8-sig", "replace")
        return json.loads(raw) if raw.strip() else {}
    except Exception:
        return {}


def _transcript_facts(path):
    """Dernières lignes ROUTAGE, workflow en cours, dernière demande utilisateur."""
    facts = {"routage": [], "workflow": None, "last_user": None}
    if not path or not os.path.isfile(path):
        return facts
    try:
        with open(path, "rb") as fh:
            size = os.path.getsize(path)
            fh.seek(max(0, size - TAIL_BYTES))
            data = fh.read().decode("utf-8", "replace")
    except OSError:
        return facts
    for line in data.splitlines():
        if not line.startswith("{"):
            continue
        try:
            e = json.loads(line)
        except ValueError:
            continue
        msg = e.get("message") or {}
        content = msg.get("content")
        if e.get("type") == "assistant":
            if e.get("attributionSkill"):
                facts["workflow"] = e["attributionSkill"]
            for c in content or []:
                if isinstance(c, dict) and c.get("type") == "text":
                    facts["routage"] += ROUTAGE_RE.findall(c.get("text", ""))
        elif e.get("type") == "user" and not e.get("isMeta"):
            text = content if isinstance(content, str) else " ".join(
                c.get("text", "") for c in (content or [])
                if isinstance(c, dict) and c.get("type") == "text")
            text = text.strip()
            if text and not text.startswith("<"):
                facts["last_user"] = text
    return facts


def _attempts(routage):
    counts = {}
    for r in routage:
        m = re.match(r"ROUTAGE\s+(.+?)\s*:", r)
        if m:
            key = m.group(1).strip()
            counts[key] = counts.get(key, 0) + 1
    return counts


def _board_open_lines():
    path = os.path.join(_root(), "docs", "31-board.md")
    out = []
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            for line in fh:
                if re.search(r"\|\s*\**(in_progress|in progress|review|blocked)\**\s*\|", line, re.I) \
                        and "**done**" not in line:
                    out.append(line.strip()[:220])
    except OSError:
        pass
    return out[-8:]


def cmd_save():
    data = _stdin_json()
    now = dt.datetime.now().astimezone()
    branch = _run(["git", "rev-parse", "--abbrev-ref", "HEAD"]).strip()
    head = _run(["git", "log", "-1", "--format=%h %s"]).strip()
    status = _run(["git", "status", "--short"]).splitlines()
    gate = ""
    if os.path.isfile(os.path.join(_root(), ".claude", "tools", "dod-gate.py")):
        gate = _run([sys.executable, ".claude/tools/dod-gate.py", "status"], timeout=30)
    facts = _transcript_facts(data.get("transcript_path"))
    att = _attempts(facts["routage"])
    lines = [
        "# État de lot sauvegardé avant compaction",
        "",
        "- Sauvegardé : %s (déclencheur : %s, session %s)" % (
            now.strftime("%Y-%m-%d %H:%M"), data.get("trigger") or "?",
            (data.get("session_id") or "?")[:8]),
        "- Branche / HEAD : `%s` · `%s`" % (branch or "?", head or "?"),
        "- Arbre de travail : %d fichier(s) modifié(s)%s" % (
            len(status), (" — " + ", ".join(s[3:] for s in status[:12])) if status else ""),
        "- Workflow en cours (dernière commande) : %s" % (facts["workflow"] or "aucun détecté"),
    ]
    if facts["last_user"]:
        lines.append("- Dernière demande de Chakir : « %s »" % facts["last_user"][:300].replace("\n", " "))
    if gate:
        lines += ["", "## dod-gate status", "", "```", "\n".join(gate.splitlines()[:12]), "```"]
    if facts["routage"]:
        lines += ["", "## Dernières lignes ROUTAGE", ""]
        lines += ["- " + r.strip() for r in facts["routage"][-8:]]
        lines += ["", "Tentatives vues par tâche (compteur unique, 3 max) : " +
                  ", ".join("%s ×%d" % kv for kv in att.items())]
    board = _board_open_lines()
    if board:
        lines += ["", "## Tickets ouverts (docs/31-board.md)", ""] + board
    lines += ["", "*Vérifier avant d'agir : c'est un instantané, git et le board font foi.*", ""]
    path = _state_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines))
    return 0


def cmd_inject():
    data = _stdin_json()
    path = _state_path()
    try:
        age = time.time() - os.path.getmtime(path)
    except OSError:
        return 0
    if age > MAX_AGE_S:
        return 0
    source = data.get("source") or "compact"
    if source == "startup":
        ago = "%d min" % (age // 60) if age < 3600 else "%d h" % (age // 3600)
        print("ℹ️ Un état de lot de la session précédente (il y a %s) est dans "
              "`.claude/.cache/lot-state.md` — à relire seulement si tu reprends ce travail." % ago)
        return 0
    with open(path, encoding="utf-8", errors="replace") as fh:
        print(fh.read())
    return 0


def main(argv):
    try:
        if hasattr(sys.stdout, "reconfigure"):
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        cmd = argv[0] if argv else "show"
        if cmd == "save":
            return cmd_save()
        if cmd == "inject":
            return cmd_inject()
        if cmd == "show":
            with open(_state_path(), encoding="utf-8") as fh:
                print(fh.read())
            return 0
        print(__doc__)
        return 0
    except Exception:
        return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
