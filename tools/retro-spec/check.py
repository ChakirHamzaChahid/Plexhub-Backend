#!/usr/bin/env python3
"""check.py — garde-fou anti-dérive de la rétro-spécification.

Parcourt docs/retro-spec/**/*.md (hors _genere/ et technique/db/, générés) et vérifie :
  ERREUR  une preuve `chemin:ligne` (ou `chemin:ligne-ligne`) dont le fichier
          n'existe plus ou qui pointe au-delà de la fin du fichier ;
  ERREUR  un identifiant d'exigence (FR-TV-###, FR-BE-###) défini deux fois ;
  AVERT.  une exigence définie sans aucune preuve ni statut « absent » ;
  AVERT.  une exigence citée mais définie nulle part ;
  AVERT.  une preuve vers l'autre dépôt (préfixe `tv:` ou `backend:`) non
          vérifiable parce que l'autre dépôt n'est pas à l'emplacement attendu.

Préfixes inter-dépôts : `backend:app/api/media.py:42`, `tv:app/src/.../X.kt:10`.
Emplacements relatifs déclarés dans tools/retro-spec/config.json.

Usage : python tools/retro-spec/check.py [--root <dépôt>]   (exit 1 si erreur)
"""
import argparse
import glob
import json
import os
import re
import sys

EVID = re.compile(r"`(?:(tv|backend):)?([\w./@-]+\.[\w]+):(\d+)(?:-(\d+))?`")
FR_DEF = re.compile(r"^\s*[-*]\s+\*\*(FR-(?:TV|BE)-\d{3})\*\*", re.M)
FR_ANY = re.compile(r"\bFR-(?:TV|BE)-\d{3}\b")
SKIP = ("/_genere/", "/technique/db/")


def load_cfg(root):
    try:
        return json.load(open(os.path.join(root, "tools", "retro-spec", "config.json"), encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def main(argv):
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", default=os.environ.get("CLAUDE_PROJECT_DIR") or os.getcwd())
    a = ap.parse_args(argv)
    root = os.path.abspath(a.root)
    cfg = load_cfg(root)
    here = cfg.get("self", "")
    others = {k: os.path.normpath(os.path.join(root, v)) for k, v in cfg.get("repos", {}).items()}
    base = os.path.join(root, "docs", "retro-spec")
    docs = [p for p in glob.glob(os.path.join(base, "**", "*.md"), recursive=True)
            if not any(s in p.replace("\\", "/") for s in SKIP)]
    errors, warns = [], []
    defined, cited = {}, set()
    n_ev = 0
    line_cache = {}

    def nlines(path):
        if path not in line_cache:
            try:
                with open(path, encoding="utf-8", errors="replace") as fh:
                    line_cache[path] = sum(1 for _ in fh)
            except OSError:
                line_cache[path] = -1
        return line_cache[path]

    for d in sorted(docs):
        rel = os.path.relpath(d, root).replace("\\", "/")
        t = open(d, encoding="utf-8", errors="replace").read()
        for m in EVID.finditer(t):
            n_ev += 1
            repo, path, l1, l2 = m.group(1), m.group(2), int(m.group(3)), int(m.group(4) or m.group(3))
            where = "%s:%d" % (rel, t.count("\n", 0, m.start()) + 1)
            if repo and repo != here:
                rr = others.get(repo)
                if not rr or not os.path.isdir(rr):
                    warns.append("%s  preuve `%s:%s` non vérifiable (dépôt %s absent)" % (where, repo, path, repo))
                    continue
                full = os.path.join(rr, path)
            else:
                full = os.path.join(root, path)
            n = nlines(full)
            if n < 0:
                errors.append("%s  fichier disparu : `%s`" % (where, m.group(0).strip("`")))
            elif l2 > n or l1 < 1:
                errors.append("%s  ligne hors fichier : `%s` (le fichier fait %d lignes)" % (where, m.group(0).strip("`"), n))
        defs = list(FR_DEF.finditer(t))
        for i, m in enumerate(defs):
            fid = m.group(1)
            where = "%s:%d" % (rel, t.count("\n", 0, m.start()) + 1)
            if fid in defined:
                errors.append("%s  %s déjà défini en %s" % (where, fid, defined[fid]))
            else:
                defined[fid] = where
            end = defs[i + 1].start() if i + 1 < len(defs) else len(t)
            nxt = re.search(r"^#{1,6}\s", t[m.end():end], re.M)
            block = t[m.start(): m.end() + nxt.start()] if nxt else t[m.start():end]
            if not EVID.search(block) and not re.search(r"\babsent\b|non implément", block, re.I):
                warns.append("%s  %s sans preuve `fichier:ligne` ni statut « absent »" % (where, fid))
        cited |= set(FR_ANY.findall(t))
    for fid in sorted(cited - set(defined)):
        warns.append("%s cité mais défini nulle part" % fid)
    for w in warns:
        print("AVERT.  " + w)
    for e in errors:
        print("ERREUR  " + e)
    print("check rétro-spec : %d document(s), %d preuve(s), %d exigence(s) — %d erreur(s), %d avertissement(s)"
          % (len(docs), n_ev, len(defined), len(errors), len(warns)))
    return 1 if errors else 0


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.exit(main(sys.argv[1:]))
