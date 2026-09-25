#!/usr/bin/env python3
"""inventaire.py — inventaire factuel du dépôt pour la rétro-spécification.

LECTURE SEULE sur le code : n'écrit que docs/retro-spec/_genere/inventaire.{md,json}.
Détecte le type de dépôt (Android Gradle ou FastAPI) et extrait, avec leur
preuve `fichier:ligne` :
  - Android : modules, écrans, ViewModels, routes (sealed class Screen), appels
    navigate(), DAO/entités Room, workers, endpoints Retrofit, clés de texte
    par préfixe, parcours Maestro ;
  - FastAPI : endpoints (préfixe include_router + APIRouter + décorateur, garde
    d'accès), tâches planifiées, tables, champs de configuration (noms seuls,
    jamais de valeur), templates admin, tests.
Jamais lus : keystore/, *.jks, *.keystore, .env, fichiers de secrets.

Usage : python tools/retro-spec/inventaire.py [--root <dépôt>] [--check]
  --check : ne réécrit rien, sort en 1 si l'inventaire généré est périmé.
"""
import argparse
import ast
import collections
import json
import os
import re
import subprocess
import sys

DENY = re.compile(r"(^|/)(keystore/|.*\.jks$|.*\.keystore$|\.env$|.*secret.*|google-services\.json$)", re.I)


def git_files(root):
    r = subprocess.run(["git", "ls-files"], cwd=root, capture_output=True)
    return [f for f in r.stdout.decode("utf-8", "replace").split("\n") if f and not DENY.search(f)]


def read(root, f):
    try:
        with open(os.path.join(root, f), encoding="utf-8", errors="replace") as fh:
            return fh.read()
    except OSError:
        return ""


def line_of(text, pos):
    return text.count("\n", 0, pos) + 1


def ref(f, text, pos):
    return "%s:%d" % (f, line_of(text, pos))


# --------------------------------------------------------------------- Android
def android(root, files):
    kt = [f for f in files if f.endswith(".kt") and "/src/test" not in f and "/src/androidTest" not in f]
    inv = collections.OrderedDict()
    st = read(root, "settings.gradle.kts")
    inv["modules"] = re.findall(r'include\("(:[^"]+)"\)', st)
    screens, vms, daos, entities, workers, retro, navs, routes = [], [], [], [], [], [], [], []
    for f in kt:
        t = read(root, f)
        if f.endswith("Screen.kt"):
            for m in re.finditer(r"@Composable\s+(?:internal\s+|private\s+)?fun\s+(\w+Screen)\s*\(", t):
                screens.append({"name": m.group(1), "ref": ref(f, t, m.start())})
        for m in re.finditer(r"@HiltViewModel[\s\S]{0,200}?class\s+(\w+)", t):
            vms.append({"name": m.group(1), "ref": ref(f, t, m.start(1))})
        for m in re.finditer(r"@Dao\s+(?:abstract\s+class|interface)\s+(\w+)", t):
            body = t[m.end():]
            daos.append({"name": m.group(1), "ref": ref(f, t, m.start(1)),
                         "queries": len(re.findall(r"@(Query|Insert|Update|Delete|Upsert|RawQuery)\b", body[:body.find("\n}\n") if "\n}\n" in body else len(body)]))})
        for m in re.finditer(r"@Entity\(([\s\S]{0,300}?)\)\s*(?:data\s+)?class\s+(\w+)", t):
            tn = re.search(r'tableName\s*=\s*"([^"]+)"', m.group(1))
            entities.append({"class": m.group(2), "table": tn.group(1) if tn else m.group(2), "ref": ref(f, t, m.start(2))})
        for m in re.finditer(r"class\s+(\w+)[^{]*?:\s*(?:CoroutineWorker|Worker|ListenableWorker)\(", t):
            workers.append({"name": m.group(1), "ref": ref(f, t, m.start(1))})
        for m in re.finditer(r'@(GET|POST|PUT|DELETE|PATCH|HEAD)\("([^"]*)"\)\s*(?:@[\w.]+(?:\([^)]*\))?\s*)*(?:suspend\s+)?fun\s+(\w+)', t):
            retro.append({"method": m.group(1), "path": m.group(2), "fun": m.group(3),
                          "service": os.path.basename(f)[:-3], "ref": ref(f, t, m.start())})
        for m in re.finditer(r"\.navigate\(\s*([^)\n]{1,80})", t):
            navs.append({"target": m.group(1).strip(), "ref": ref(f, t, m.start())})
        if f.endswith("/Screen.kt") and "sealed class Screen" in t:
            for m in re.finditer(r'(?:data\s+)?object\s+(\w+)\s*:\s*Screen\("([^"]+)"', t):
                routes.append({"name": m.group(1), "route": m.group(2), "ref": ref(f, t, m.start(1))})
            for m in re.finditer(r'(?:data\s+)?class\s+(\w+)\([^)]*\)\s*:\s*Screen\("([^"]+)"', t):
                routes.append({"name": m.group(1), "route": m.group(2), "ref": ref(f, t, m.start(1))})
    inv["routes"] = routes
    inv["screens"] = sorted(screens, key=lambda x: x["name"])
    inv["viewmodels"] = sorted(vms, key=lambda x: x["name"])
    inv["navigate_calls"] = len(navs)
    inv["daos"] = sorted(daos, key=lambda x: x["name"])
    inv["entities"] = sorted(entities, key=lambda x: x["table"])
    inv["workers"] = sorted(workers, key=lambda x: x["name"])
    inv["retrofit"] = sorted(retro, key=lambda x: (x["service"], x["path"]))
    keys = collections.Counter()
    for f in files:
        if f.endswith("values/strings.xml"):
            for k in re.findall(r'<string\s+name="([^"]+)"', read(root, f)):
                keys[k.split("_")[0]] += 1
    inv["string_prefixes"] = keys.most_common(40)
    flows = []
    for f in sorted(x for x in files if x.startswith(".maestro/") and x.endswith((".yaml", ".yml"))):
        t = read(root, f)
        first = next((l.strip("# ").strip() for l in t.splitlines() if l.strip().startswith("#")), "")
        flows.append({"file": f, "comment": first[:120], "steps": len(re.findall(r"^\s*-\s", t, re.M))})
    inv["maestro_flows"] = flows
    return inv


def android_md(inv):
    L = []
    L.append("## Modules Gradle (%d)\n\n%s\n" % (len(inv["modules"]), " · ".join("`%s`" % m for m in inv["modules"])))
    L.append("## Routes de navigation (%d) — `sealed class Screen`\n" % len(inv["routes"]))
    L.append("| Route | Nom | Preuve |\n|---|---|---|")
    L += ["| `%s` | %s | `%s` |" % (r["route"], r["name"], r["ref"]) for r in inv["routes"]]
    L.append("\nAppels `navigate(...)` dans le code : %d.\n" % inv["navigate_calls"])
    L.append("## Écrans Compose (%d)\n\n| Écran | Preuve |\n|---|---|" % len(inv["screens"]))
    L += ["| %s | `%s` |" % (s["name"], s["ref"]) for s in inv["screens"]]
    L.append("\n## ViewModels (%d)\n\n| ViewModel | Preuve |\n|---|---|" % len(inv["viewmodels"]))
    L += ["| %s | `%s` |" % (s["name"], s["ref"]) for s in inv["viewmodels"]]
    L.append("\n## Tables Room (%d)\n\n| Table | Entité | Preuve |\n|---|---|---|" % len(inv["entities"]))
    L += ["| `%s` | %s | `%s` |" % (e["table"], e["class"], e["ref"]) for e in inv["entities"]]
    L.append("\n## DAO (%d)\n\n| DAO | Requêtes | Preuve |\n|---|---|---|" % len(inv["daos"]))
    L += ["| %s | %d | `%s` |" % (d["name"], d["queries"], d["ref"]) for d in inv["daos"]]
    L.append("\n## Workers WorkManager (%d)\n\n| Worker | Preuve |\n|---|---|" % len(inv["workers"]))
    L += ["| %s | `%s` |" % (w["name"], w["ref"]) for w in inv["workers"]]
    L.append("\n## Endpoints appelés (Retrofit, %d)\n\n| Service | Méthode | Chemin | Fonction | Preuve |\n|---|---|---|---|---|" % len(inv["retrofit"]))
    L += ["| %s | %s | `%s` | %s | `%s` |" % (r["service"], r["method"], r["path"] or "/", r["fun"], r["ref"]) for r in inv["retrofit"]]
    L.append("\n## Préfixes des clés de texte (`values/strings.xml`) — signal des domaines fonctionnels\n")
    L.append(" · ".join("`%s` %d" % kv for kv in inv["string_prefixes"]))
    L.append("\n\n## Parcours Maestro (%d)\n\n| Fichier | Description | Étapes |\n|---|---|---|" % len(inv["maestro_flows"]))
    L += ["| `%s` | %s | %d |" % (m["file"], m["comment"], m["steps"]) for m in inv["maestro_flows"]]
    return "\n".join(L) + "\n"


# --------------------------------------------------------------------- FastAPI
def fastapi(root, files):
    inv = collections.OrderedDict()
    main = read(root, "app/main.py")
    inc = {}
    for m in re.finditer(r"include_router\(\s*(\w+)\.router([^\n]*)", main):
        mod, rest = m.group(1), m.group(2)
        pre = re.search(r'prefix\s*=\s*"([^"]*)"', rest)
        guard = "X-API-Key" if "_guard" in rest else ("Basic admin" if "admin_basic" in rest else "")
        inc[mod] = {"prefix": pre.group(1) if pre else "", "guard": guard, "ref": ref("app/main.py", main, m.start())}
    eps = []
    for f in sorted(x for x in files if x.startswith("app/api/") and x.endswith(".py")):
        t = read(root, f)
        mod = os.path.basename(f)[:-3]
        try:
            tree = ast.parse(t)
        except SyntaxError:
            continue
        rprefix, rguard = "", ""
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and getattr(node.func, "id", "") == "APIRouter":
                for kw in node.keywords:
                    if kw.arg == "prefix" and isinstance(kw.value, ast.Constant):
                        rprefix = kw.value.value
                    if kw.arg == "dependencies":
                        src = ast.get_source_segment(t, kw.value) or ""
                        rguard = "X-API-Key" if "verify_api_key" in src else ("Basic admin" if "admin_basic" in src else rguard)
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            for dec in node.decorator_list:
                if not (isinstance(dec, ast.Call) and isinstance(dec.func, ast.Attribute)
                        and getattr(dec.func.value, "id", "") == "router"
                        and dec.func.attr in ("get", "post", "put", "delete", "patch", "api_route")):
                    continue
                path = dec.args[0].value if dec.args and isinstance(dec.args[0], ast.Constant) else "?"
                method = dec.func.attr.upper()
                if dec.func.attr == "api_route":
                    for kw in dec.keywords:
                        if kw.arg == "methods":
                            method = "/".join(getattr(e, "value", "?") for e in getattr(kw.value, "elts", []))
                seg = (ast.get_source_segment(t, dec) or "") + " ".join(
                    ast.get_source_segment(t, d) or "" for d in node.args.defaults + node.args.kw_defaults if d is not None)
                g = inc.get(mod, {}).get("guard") or rguard
                if not g and "verify_api_key" in seg:
                    g = "X-API-Key"
                if not g and "verify_admin" in seg:
                    g = "Basic admin"
                eps.append({"method": method, "path": inc.get(mod, {}).get("prefix", "") + rprefix + path,
                            "fun": node.name, "module": mod, "guard": g or "public ?",
                            "ref": "%s:%d" % (f, dec.lineno)})
    inv["endpoints"] = eps
    inv["include_router"] = inc
    jobs = []
    for m in re.finditer(r"add_job\(\s*([\w.]+)([\s\S]{0,300}?)\)", main):
        idm = re.search(r'id\s*=\s*"([^"]+)"', m.group(2))
        trig = re.search(r'"(interval|cron|date)"', m.group(2))
        jobs.append({"func": m.group(1), "id": idm.group(1) if idm else "", "trigger": trig.group(1) if trig else "",
                     "ref": ref("app/main.py", main, m.start())})
    inv["scheduled_jobs"] = jobs
    inv["workers"] = [f for f in files if f.startswith("app/workers/") and f.endswith(".py") and not f.endswith("__init__.py")]
    tables = []
    mg = read(root, "app/db/migrations.py")
    for m in re.finditer(r"CREATE TABLE(?: IF NOT EXISTS)?\s+(\w+)", mg):
        tables.append({"table": m.group(1), "source": "migrations", "ref": ref("app/db/migrations.py", mg, m.start())})
    md = read(root, "app/models/database.py")
    for m in re.finditer(r'__tablename__\s*=\s*"(\w+)"', md):
        tables.append({"table": m.group(1), "source": "ORM", "ref": ref("app/models/database.py", md, m.start())})
    inv["tables"] = tables
    cfg = read(root, "app/config.py")
    fields = []
    try:
        for node in ast.parse(cfg).body:
            if isinstance(node, ast.ClassDef) and node.name in ("Settings", "Config", "AppSettings"):
                fields += [n.target.id for n in node.body if isinstance(n, ast.AnnAssign) and isinstance(n.target, ast.Name)]
    except SyntaxError:
        pass
    inv["config_fields"] = fields
    inv["services"] = sorted(os.path.basename(f)[:-3] for f in files if f.startswith("app/services/") and f.endswith(".py") and not f.endswith("__init__.py"))
    inv["templates"] = sorted(f for f in files if "/templates/" in f)
    inv["tests"] = len([f for f in files if f.startswith("tests/") and f.endswith(".py")])
    return inv


def fastapi_md(inv):
    L = ["## Endpoints (%d)\n" % len(inv["endpoints"]),
         "| Méthode | Chemin | Garde | Fonction | Preuve |", "|---|---|---|---|---|"]
    L += ["| %s | `%s` | %s | %s | `%s` |" % (e["method"], e["path"], e["guard"], e["fun"], e["ref"])
          for e in sorted(inv["endpoints"], key=lambda e: (e["path"], e["method"]))]
    L.append("\n## Tâches planifiées (APScheduler, %d)\n\n| Fonction | Id | Déclencheur | Preuve |\n|---|---|---|---|" % len(inv["scheduled_jobs"]))
    L += ["| `%s` | %s | %s | `%s` |" % (j["func"], j["id"], j["trigger"], j["ref"]) for j in inv["scheduled_jobs"]]
    L.append("\n## Workers\n\n" + " · ".join("`%s`" % w for w in inv["workers"]))
    L.append("\n## Services (%d)\n\n%s" % (len(inv["services"]), " · ".join("`%s`" % s for s in inv["services"])))
    L.append("\n## Tables (%d déclarations)\n\n| Table | Source | Preuve |\n|---|---|---|" % len(inv["tables"]))
    L += ["| `%s` | %s | `%s` |" % (t["table"], t["source"], t["ref"]) for t in inv["tables"]]
    L.append("\n## Configuration (`app/config.py`, noms seuls — aucune valeur)\n\n" + " · ".join("`%s`" % f for f in inv["config_fields"]))
    L.append("\n\n## Templates admin (%d)\n\n%s" % (len(inv["templates"]), " · ".join("`%s`" % t for t in inv["templates"])))
    L.append("\n\nFichiers de test : %d." % inv["tests"])
    return "\n".join(L) + "\n"


def main(argv):
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", default=os.environ.get("CLAUDE_PROJECT_DIR") or os.getcwd())
    ap.add_argument("--check", action="store_true")
    a = ap.parse_args(argv)
    root = os.path.abspath(a.root)
    files = git_files(root)
    if os.path.exists(os.path.join(root, "settings.gradle.kts")):
        kind, inv = "android", android(root, files)
        body = android_md(inv)
    elif os.path.exists(os.path.join(root, "app", "main.py")):
        kind, inv = "fastapi", fastapi(root, files)
        body = fastapi_md(inv)
    else:
        print("type de dépôt non reconnu"); return 2
    head = subprocess.run(["git", "rev-parse", "--short", "HEAD"], cwd=root, capture_output=True, text=True).stdout.strip()
    md = ("# Inventaire généré (%s)\n\n> ⚙️ **Généré par `tools/retro-spec/inventaire.py` — ne pas éditer à la main.** "
          "Base factuelle de la rétro-spécification : chaque ligne porte sa preuve `fichier:ligne`. "
          "Régénérer : `python tools/retro-spec/inventaire.py`.\n\n" % kind) + body
    out_dir = os.path.join(root, "docs", "retro-spec", "_genere")
    md_path, js_path = os.path.join(out_dir, "inventaire.md"), os.path.join(out_dir, "inventaire.json")
    js = json.dumps(inv, ensure_ascii=False, indent=1)
    if a.check:
        try:
            same = open(md_path, encoding="utf-8").read() == md and open(js_path, encoding="utf-8").read() == js
        except OSError:
            same = False
        print("inventaire à jour" if same else "inventaire PÉRIMÉ — relancer tools/retro-spec/inventaire.py")
        return 0 if same else 1
    os.makedirs(out_dir, exist_ok=True)
    with open(md_path, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(md)
    with open(js_path, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(js)
    counts = {k: (len(v) if isinstance(v, (list, dict)) else v) for k, v in inv.items()}
    print("inventaire %s (HEAD %s) : %s" % (kind, head, counts))
    return 0


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.exit(main(sys.argv[1:]))
