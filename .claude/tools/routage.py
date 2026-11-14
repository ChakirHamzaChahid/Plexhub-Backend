#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""routage - calcule le couple (modele, effort) de la grille model-effort-routing.

La skill `.claude/skills/model-effort-routing/SKILL.md` fait foi ; ce script en
applique les regles mecaniquement pour qu'une lecture rapide ne les fausse pas
(constat SCR-4 du 2026-11-13 : T2 et PERF routes en `medium` alors que leurs
notes donnaient `high`).

Usage :
    python .claude/tools/routage.py "C1=1 C2=1 C3=1 C4=2 C5=1"            # tache qui ecrit
    python .claude/tools/routage.py "C1=2 C2=2 C3=1 C4=1 C5=2" --lecteur  # revue, audit
    python .claude/tools/routage.py "..." --lecteur --securite            # security-reviewer
    python .claude/tools/routage.py "C1=2 C2=0 C3=0 C4=0 C5=0" --lecture-seule [--large]   # Explore
    python .claude/tools/routage.py check < plan.txt   # verifie chaque ligne ROUTAGE

`check` lit des lignes `ROUTAGE <id> : model=<m> effort=<e> agent=<fiche> — C1=x ...`
et sort 1 si le modele ou l'effort declare differe du calcul. Une ligne marquee
`tentative 2` / `tentative 3` (escalade, §4) est seulement affichee ; une ligne
`model=session` est verifiee contre la regle du ticket minuscule.
Les drapeaux --lecteur / --securite / --lecture-seule / --large peuvent aussi
figurer dans la ligne ROUTAGE sous forme de mots : lecteur, securite, lecture seule.
"""
import re
import sys

ORDER = {"haiku": 0, "sonnet": 1, "opus": 2}


def parse(s):
    vals = dict((k.upper(), int(v)) for k, v in re.findall(r"\b(C[1-5])\s*=\s*([0-2])\b", s, re.I))
    missing = [k for k in ("C1", "C2", "C3", "C4", "C5") if k not in vals]
    if missing:
        raise ValueError("notes manquantes : " + ", ".join(missing))
    return vals


def route(c, lecteur=False, securite=False, lecture_seule=False, large=False):
    """Renvoie (modele, effort, raison, notes) selon la grille."""
    c1, c2, c3, c4, c5 = (c["C%d" % i] for i in range(1, 6))
    notes = []
    if lecteur or securite:
        # Regle « agents en lecture seule » (2026-11-13) : jamais haiku en detection.
        if securite:
            return "opus", ("high" if c3 == 2 else "medium"), "lecteur securite", notes
        return "sonnet", ("high" if c2 == 2 else "medium"), "lecteur" + (" · zone C2 = 2" if c2 == 2 else ""), notes
    if lecture_seule and c5 == 0 and c3 < 2:
        # Regle 2bis : recherche mecanique (Explore), effort = session.
        if large:
            notes.append("palier Haiku x5 au-dela de 100K tokens : contexte large -> sonnet (E8)")
            return "sonnet", "session", "lecture seule · contexte large", notes
        return "haiku", "session", "lecture seule mecanique (2bis)", notes
    # Modele (regles 1-3)
    if c2 == 2 or c3 == 2 or (c5 == 2 and c1 == 2):
        why = [r for r, ok in (("C2 = 2", c2 == 2), ("C3 = 2", c3 == 2), ("C5 = 2 et C1 = 2", c5 == 2 and c1 == 2)) if ok]
        model, mwhy = "opus", " · ".join(why)
    elif c5 == 0 and c1 == 0 and c2 == 0 and c4 == 0:
        model, mwhy = "haiku", "mecanique, 1 fichier, aucun risque, controle automatique"
    else:
        model, mwhy = "sonnet", "cas general"
    # Effort (ordre : cas high, sinon medium)
    if model == "haiku":
        effort, ewhy = "medium", "haiku : l'effort compte peu"
    elif c2 == 2 and c3 >= 1:
        effort, ewhy = "high", "C2 = 2 et C3 >= 1"
    elif c3 == 2:
        effort, ewhy = "high", "C3 = 2"
    elif c3 == 1 and c4 == 2:
        effort, ewhy = "high", "C3 = 1 et C4 = 2"
    else:
        effort, ewhy = "medium", "aucun cas high" + (" (C2 = 2 seul : E10)" if c2 == 2 else "")
    # Variantes
    if c1 == 0 and c2 == 0 and c3 == 0 and c5 <= 1:
        notes.append("ticket minuscule possible (E4) : la session principale le fait si une poignee de lignes")
    if c2 <= 1 and c3 == 0 and c4 == 0 and model != "haiku":
        low_model = "opus" if (c5 == 0 and c1 >= 1) else "sonnet"
        notes.append("pilote low possible (developpeurs) : %s · low, fiche <agent>-low" % low_model)
    return model, effort, mwhy + " ; " + ewhy, notes


def flags_from(agent, line):
    """Drapeaux deduits de la fiche appelee, ou de mots explicites dans la ligne."""
    a, t = agent.lower(), line.lower()
    securite = a.startswith("security-reviewer")
    return dict(lecteur=securite or "reviewer" in a or "auditor" in a or bool(re.search(r"\blecteur\b", t)),
                securite=securite,
                lecture_seule=a in ("explore",) or "lecture seule" in t,
                large="very thorough" in t or "contexte large" in t)


def fmt(model, effort, why, notes):
    out = "%s · %s  (%s)" % (model, effort, why)
    for n in notes:
        out += "\n  - " + n
    return out


def check(lines):
    bad = 0
    seen = 0
    for line in lines:
        if "ROUTAGE" not in line:
            continue
        seen += 1
        m = re.search(r"model\s*=\s*(\w+)", line)
        e = re.search(r"effort\s*=\s*(\w+)", line)
        a = re.search(r"agent\s*=\s*([\w-]+)", line)
        try:
            c = parse(line)
        except ValueError as err:
            print("?? %s\n   %s" % (line.strip(), err))
            bad += 1
            continue
        if re.search(r"tentative\s*[23]", line, re.I):
            print("-- %s\n   escalade (§4) : non verifiee" % line.strip())
            continue
        dm, de, ag = (m.group(1) if m else "?"), (e.group(1) if e else "?"), (a.group(1) if a else "")
        if dm == "session":
            ok = c["C1"] == 0 and c["C2"] == 0 and c["C3"] == 0 and c["C5"] <= 1
            print("%s %s\n   ticket minuscule : %s" % ("OK" if ok else "KO", line.strip(),
                  "conditions remplies" if ok else "conditions NON remplies (C1=0 C2=0 C3=0 C5<=1)"))
            bad += 0 if ok else 1
            continue
        fl = flags_from(ag, line)
        cm, ce, why, notes = route(c, **fl)
        # Le pilote low est un choix permis quand la grille donne medium et que le ticket est eligible.
        low_ok = de == "low" and ce == "medium" and any("pilote low" in n for n in notes)
        effort_ok = de == ce or ce == "session" or low_ok
        suffix_ok = True
        if ag and ce in ("high", "low") and not low_ok and not ag.endswith("-" + ce):
            suffix_ok = False
        if ag and de == "medium" and (ag.endswith("-high") or ag.endswith("-low")):
            suffix_ok = False
        ok = dm == cm and effort_ok and suffix_ok
        bad += 0 if ok else 1
        print("%s %s\n   grille : %s%s" % ("OK" if ok else "KO", line.strip(), fmt(cm, ce, why, notes),
              "" if suffix_ok else "\n   fiche : le suffixe de l'agent ne correspond pas a l'effort"))
    if not seen:
        print("Aucune ligne ROUTAGE trouvee.")
        return 2
    print("\n%d ligne(s), %d ecart(s)." % (seen, bad))
    return 1 if bad else 0


def main(argv):
    if not argv or argv[0] in ("-h", "--help"):
        print(__doc__)
        return 2
    if argv[0] == "check":
        return check(sys.stdin.read().splitlines())
    try:
        c = parse(argv[0])
    except ValueError as err:
        print(err)
        return 2
    print(fmt(*route(c, lecteur="--lecteur" in argv, securite="--securite" in argv,
                     lecture_seule="--lecture-seule" in argv, large="--large" in argv)))
    return 0


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.exit(main(sys.argv[1:]))
