#!/usr/bin/env python3
"""gen-effort-twins — jumeaux d'effort des sous-agents Claude Code.

POURQUOI (décision de Chakir, 2026-09-23) : chaque tâche est évaluée pour choisir
son modèle (haiku/sonnet/opus) ET son effort (medium/high). Le modèle se choisit
à l'appel (paramètre `model:` de l'outil Agent), mais l'effort NE PEUT PAS l'être :
il est figé dans le frontmatter de l'agent (doc Claude Code « sub-agents »).
D'où deux fiches par agent :
  - `<agent>.md`       = SOURCE, `effort: medium` (seule fiche à éditer)
  - `<agent>-high.md`  = GÉNÉRÉE par ce script, identique à `effort: high` près
Choisir l'effort d'une tâche = choisir la fiche ; choisir le modèle = `model:`.

Usage :
  python .claude/tools/gen-effort-twins.py           # (re)génère tous les jumeaux
  python .claude/tools/gen-effort-twins.py --check   # échoue si un jumeau manque,
                                                      # est périmé ou orphelin, ou si
                                                      # une fiche de base n'est pas en medium
  python .claude/tools/gen-effort-twins.py --init    # pose `effort: medium` sur les
                                                      # fiches de base, puis génère

Le script ne SUPPRIME jamais de fichier : un jumeau orphelin est signalé, à toi de
le retirer.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
AGENTS = ROOT / ".claude" / "agents"
SUFFIX = "-high"
BASE_EFFORT = "medium"
TWIN_EFFORT = "high"
MARK = (
    "<!-- GÉNÉRÉ par .claude/tools/gen-effort-twins.py depuis {src} — NE PAS ÉDITER : "
    "modifie la fiche de base puis relance le script. -->"
)
FM_RE = re.compile(r"\A---\r?\n(.*?)\r?\n---\r?\n", re.S)


def split(text: str) -> tuple[str, str]:
    m = FM_RE.match(text)
    if not m:
        raise ValueError("frontmatter YAML absent")
    return m.group(1), text[m.end():]


def get_field(fm: str, key: str) -> str | None:
    m = re.search(rf"^{key}:[ \t]*(.*)$", fm, re.M)
    return m.group(1).strip() if m else None


def set_field(fm: str, key: str, value: str) -> str:
    pat = re.compile(rf"^{key}:.*$", re.M)
    if pat.search(fm):
        return pat.sub(lambda _: f"{key}: {value}", fm, count=1)
    anchor = re.search(r"^model:.*$", fm, re.M)
    if anchor:
        return fm[: anchor.end()] + f"\n{key}: {value}" + fm[anchor.end():]
    return fm + f"\n{key}: {value}"


def read(p: Path) -> tuple[str, str]:
    raw = p.read_bytes().decode("utf-8")
    nl = "\r\n" if "\r\n" in raw else "\n"
    return raw.replace("\r\n", "\n"), nl


def bases() -> list[Path]:
    return sorted(p for p in AGENTS.glob("*.md") if not p.stem.endswith(SUFFIX))


def render_twin(base: Path) -> tuple[str, str]:
    text, nl = read(base)
    fm, body = split(text)
    name = get_field(fm, "name") or base.stem
    desc = get_field(fm, "description") or ""
    prefix = f"Variante EFFORT HIGH de `{name}` (invoquer seulement si la grille model-effort-routing le décide) — "
    if desc[:1] in ("'", '"'):
        new_desc = desc[0] + prefix + desc[1:]
    else:
        new_desc = prefix + desc
    fm2 = set_field(fm, "name", name + SUFFIX)
    fm2 = set_field(fm2, "effort", TWIN_EFFORT)
    fm2 = set_field(fm2, "description", new_desc)
    out = f"---\n{fm2}\n---\n{MARK.format(src=base.name)}\n{body}"
    return out, nl


def write(p: Path, text: str, nl: str) -> None:
    p.write_bytes(text.replace("\n", nl).encode("utf-8"))


def init_bases() -> None:
    for b in bases():
        text, nl = read(b)
        fm, body = split(text)
        if get_field(fm, "effort") != BASE_EFFORT:
            write(b, f"---\n{set_field(fm, 'effort', BASE_EFFORT)}\n---\n{body}", nl)
            print(f"base  : {b.name} -> effort: {BASE_EFFORT}")


def generate() -> int:
    n = 0
    for b in bases():
        twin = b.with_name(b.stem + SUFFIX + ".md")
        text, nl = render_twin(b)
        current = read(twin)[0] if twin.exists() else None
        if current != text:
            write(twin, text, nl)
            print(f"jumeau: {twin.name} {'mis à jour' if current else 'créé'}")
            n += 1
    for o in orphans():
        print(f"ORPHELIN (à supprimer à la main) : {o.name}")
    print(f"{len(bases())} fiches de base, {n} jumeau(x) écrit(s).")
    return 0


def orphans() -> list[Path]:
    names = {b.stem for b in bases()}
    return [t for t in sorted(AGENTS.glob(f"*{SUFFIX}.md")) if t.stem[: -len(SUFFIX)] not in names]


def check() -> int:
    errors = []
    for b in bases():
        fm, _ = split(read(b)[0])
        if get_field(fm, "effort") != BASE_EFFORT:
            errors.append(f"{b.name} : effort de base != {BASE_EFFORT}")
        twin = b.with_name(b.stem + SUFFIX + ".md")
        if not twin.exists():
            errors.append(f"{twin.name} : jumeau absent")
        elif read(twin)[0] != render_twin(b)[0]:
            errors.append(f"{twin.name} : jumeau périmé (relance le script)")
    errors += [f"{o.name} : jumeau orphelin" for o in orphans()]
    if errors:
        print("gen-effort-twins --check : ÉCHEC")
        for e in errors:
            print("  - " + e)
        return 1
    print(f"gen-effort-twins --check : OK ({len(bases())} agents, jumeaux à jour).")
    return 0


if __name__ == "__main__":
    args = set(sys.argv[1:])
    if "--check" in args:
        sys.exit(check())
    if "--init" in args:
        init_bases()
    sys.exit(generate())
