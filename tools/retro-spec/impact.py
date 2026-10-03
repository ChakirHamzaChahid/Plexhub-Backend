#!/usr/bin/env python3
"""impact.py — quelles pièces de la rétro-spec sont touchées par un delta git.

Lit les preuves `chemin:ligne` de docs/retro-spec/**/*.md (hors _genere/ et
technique/db/) et les croise avec `git diff --name-only <REF>..HEAD`. Sortie :
pour chaque fiche/volet dont une preuve pointe vers un fichier modifié, les FR
concernées (celles dont le bloc cite ce fichier). Lecture seule.

Utilisé par /sync-context (mettre à jour les fiches touchées, puis check.py) et
/wf-audit-incremental §4bis (re-vérifier les FR « implémenté » dont la preuve a bougé).

Usage : python tools/retro-spec/impact.py <REF> [--root <dépôt>] [--json]
"""
import argparse
import glob
import json
import os
import re
import subprocess
import sys

EVID = re.compile(r"`(?:(tv|backend):)?([\w./@-]+\.[\w]+):(\d+)(?:-(\d+))?`")
FR_DEF = re.compile(r"^\s*[-*]\s+\*\*(FR-(?:TV|BE)-\d{3,})\*\*", re.M)
SKIP = ("/_genere/", "/technique/db/")


def main(argv):
    ap = argparse.ArgumentParser()
    ap.add_argument("ref")
    ap.add_argument("--root", default=os.environ.get("CLAUDE_PROJECT_DIR") or os.getcwd())
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args(argv)
    root = os.path.abspath(a.root)
    r = subprocess.run(["git", "diff", "--name-only", "%s..HEAD" % a.ref], cwd=root, capture_output=True, text=True)
    if r.returncode != 0:
        print("ref invalide : %s" % a.ref)
        return 2
    changed = set(x.strip() for x in r.stdout.splitlines() if x.strip())
    out = []
    for d in sorted(glob.glob(os.path.join(root, "docs", "retro-spec", "**", "*.md"), recursive=True)):
        if any(s in d.replace("\\", "/") for s in SKIP):
            continue
        rel = os.path.relpath(d, root).replace("\\", "/")
        t = open(d, encoding="utf-8", errors="replace").read()
        hits = sorted({m.group(2) for m in EVID.finditer(t) if not m.group(1) and m.group(2) in changed})
        if not hits:
            continue
        defs = list(FR_DEF.finditer(t))
        frs = []
        for i, m in enumerate(defs):
            end = defs[i + 1].start() if i + 1 < len(defs) else len(t)
            block = t[m.start():end]
            if any(("`%s:" % h) in block for h in hits):
                frs.append(m.group(1))
        out.append({"doc": rel, "files": hits, "fr": frs})
    if a.json:
        print(json.dumps({"ref": a.ref, "changed": len(changed), "impacted": out}, ensure_ascii=False, indent=1))
        return 0
    print("rétro-spec : %d fichier(s) modifié(s) depuis %s, %d document(s) touché(s)" % (len(changed), a.ref, len(out)))
    for o in out:
        print("- %s  (%d fichier(s) : %s)" % (o["doc"], len(o["files"]), ", ".join(o["files"][:5]) + (" …" if len(o["files"]) > 5 else "")))
        if o["fr"]:
            print("    FR à re-vérifier : %s" % ", ".join(o["fr"][:40]) + (" …" if len(o["fr"]) > 40 else ""))
    return 0


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.exit(main(sys.argv[1:]))
