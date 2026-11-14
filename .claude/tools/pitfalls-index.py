#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""pitfalls-index — génère .claude/skills/pitfalls/index/<domaine>.md depuis references/.

Un titre par piège = son segment en gras (le titre donné par l'auteur du piège),
tronqué à 320 caractères. On n'édite plus l'index : on ajoute le piège dans
references/, puis on relance ce script.

    python .claude/tools/pitfalls-index.py          # régénère les index
    python .claude/tools/pitfalls-index.py --check  # exit 1 si un index est en retard
"""
import os
import re
import sys
from pathlib import Path

ROOT = Path(os.environ.get("CLAUDE_DOCTOR_ROOT") or Path(__file__).resolve().parents[2])
BASE = ROOT / ".claude" / "skills" / "pitfalls"
BULLET = re.compile(r"^- (\*\*|`)")
MAX = 320


def title(line):
    m = re.match(r"^- \*\*(.+?)\*\*", line)
    t = m.group(1) if m else re.split(r" : | — ", line[2:], maxsplit=1)[0]
    t = re.sub(r"\s+", " ", t).strip().rstrip(" :;—")
    if m and len(t) < 25:  # gras = simple étiquette : on ajoute la suite de la phrase
        rest = line[m.end():].strip().lstrip(":—").strip()
        rest = re.split(r" ; |\. ", rest, maxsplit=1)[0].strip()
        if rest:
            t = "%s : %s" % (t, rest)
    if len(t) > MAX:
        t = t[:MAX].rsplit(" ", 1)[0] + " …"
    return t


def render(domain):
    ref = BASE / "references" / (domain + ".md")
    lines = ref.read_text(encoding="utf-8").replace("\r\n", "\n").split("\n")
    titles = [title(line) for line in lines if BULLET.match(line)]
    head = re.sub(r"^#\s*Pièges connus\s*[—-]\s*", "", lines[0]).strip() or domain
    rel = ".claude/skills/pitfalls/references/%s.md" % domain
    out = [
        "# Index des pièges — %s" % domain, "",
        "> Généré par `python .claude/tools/pitfalls-index.py` depuis `%s` : un titre par piège "
        "(son segment en gras). Ne pas éditer ici — ajouter le piège dans `references/`, puis relancer le script." % rel,
        "",
        "## %s — %s (%d)" % (domain, head, len(titles)), "",
        "Texte complet : `%s`" % rel, "",
    ] + ["- " + t for t in titles] + [""]
    return "\n".join(out), len(titles)


def main():
    check = "--check" in sys.argv
    stale = []
    for ref in sorted((BASE / "references").glob("*.md")):
        d = ref.stem
        text, n = render(d)
        idx = BASE / "index" / (d + ".md")
        cur = idx.read_text(encoding="utf-8").replace("\r\n", "\n") if idx.exists() else ""
        if cur != text:
            stale.append(d)
            if not check:
                nl = "\r\n" if idx.exists() and b"\r\n" in idx.read_bytes() else "\n"
                idx.parent.mkdir(parents=True, exist_ok=True)
                idx.write_bytes(text.replace("\n", nl).encode("utf-8"))
        print("%-20s %3d pièges%s" % (d, n, ("  (EN RETARD)" if check else "  (régénéré)") if d in stale else ""))
    if check and stale:
        print("index en retard : python .claude/tools/pitfalls-index.py")
        return 1
    return 0


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.exit(main())
