#!/usr/bin/env python3
"""schema-db.py — documentation du schéma de base, SANS AUCUNE DONNÉE.

1. Construit une base SQLite **vide** dans .claude/.cache/retro-spec/ (gitignoré) :
   - Android : à partir du dernier schéma Room exporté
     (core/database/schemas/<Database>/<version>.json) ;
   - FastAPI : en ne lisant QUE `sqlite_master` de data/plexhub.db, ouverte en
     lecture seule (URI mode=ro). Aucune ligne n'est lue ni copiée.
     Les tables virtuelles dont le module n'est pas disponible (ex. vec0 de
     sqlite-vec) et leurs tables fantômes sont listées mais pas recréées.
2. Lance tbls (image Docker officielle ghcr.io/k1low/tbls) qui écrit la doc
   Markdown + ER Mermaid dans docs/retro-spec/technique/db/.
   Sans Docker : --no-tbls écrit seulement schema.md (liste des tables et
   clés étrangères, ER Mermaid compact) par ce script.

Docker distant (ex. le homelab) : --remote chakir@192.168.0.175 — seule la base
VIDE part sur la machine distante, la doc revient, le dossier distant est effacé.
Authentification : clé SSH, sinon mot de passe lu dans la variable d'environnement
RETRO_SPEC_SSH_PASSWORD (jamais écrit sur disque). Requiert paramiko.

Usage : python tools/retro-spec/schema-db.py [--root <dépôt>] [--no-tbls] [--db <chemin>] [--remote user@hôte]
"""
import argparse
import glob
import json
import os
import shutil
import sqlite3
import subprocess
import sys

TBLS_IMAGE = "ghcr.io/k1low/tbls:latest"


def room_db(root, dst):
    files = glob.glob(os.path.join(root, "core", "database", "schemas", "*", "*.json"))
    if not files:
        raise SystemExit("aucun schéma Room exporté trouvé")
    last = max(files, key=lambda p: int(os.path.basename(p)[:-5]))
    d = json.load(open(last, encoding="utf-8"))["database"]
    con = sqlite3.connect(dst)
    skipped = []
    for e in d["entities"]:
        sql = e["createSql"].replace("${TABLE_NAME}", e["tableName"])
        try:
            con.execute(sql)
        except sqlite3.Error as ex:
            skipped.append("%s (%s)" % (e["tableName"], ex))
            continue
        for ix in e.get("indices", []):
            try:
                con.execute(ix["createSql"].replace("${TABLE_NAME}", e["tableName"]))
            except sqlite3.Error as ex:
                skipped.append("index %s (%s)" % (ix.get("name"), ex))
    for v in d.get("views", []):
        try:
            con.execute(v["createSql"].replace("${VIEW_NAME}", v["viewName"]))
        except sqlite3.Error as ex:
            skipped.append("vue %s (%s)" % (v["viewName"], ex))
    con.commit()
    con.close()
    rel = os.path.relpath(last, root).replace("\\", "/")
    return "schéma Room exporté `%s` (version %s)" % (rel, d["version"]), skipped


def sqlite_schema_db(root, dst, src):
    src = src or os.path.join(root, "data", "plexhub.db")
    if not os.path.exists(src):
        raise SystemExit("base introuvable : %s" % src)
    uri = "file:%s?mode=ro" % src.replace("\\", "/")
    s = sqlite3.connect(uri, uri=True)
    rows = s.execute("SELECT type, name, tbl_name, sql FROM sqlite_master WHERE sql IS NOT NULL "
                     "AND name NOT LIKE 'sqlite_%' ORDER BY CASE type WHEN 'table' THEN 0 WHEN 'index' THEN 1 "
                     "WHEN 'view' THEN 2 ELSE 3 END, name").fetchall()
    uv = s.execute("PRAGMA user_version").fetchone()[0]
    s.close()
    virt = [r[1] for r in rows if r[0] == "table" and r[3].upper().startswith("CREATE VIRTUAL TABLE")]
    con = sqlite3.connect(dst)
    skipped = []
    for typ, name, tbl, sql in rows:
        if any(name == v or name.startswith(v + "_") for v in virt):
            try:
                con.execute(sql)
            except sqlite3.Error as ex:
                skipped.append("%s %s (%s)" % (typ, name, ex))
            continue
        try:
            con.execute(sql)
        except sqlite3.Error as ex:
            skipped.append("%s %s (%s)" % (typ, name, ex))
    con.commit()
    con.close()
    rel = os.path.relpath(src, root).replace("\\", "/")
    return "schéma de la base locale `%s` (user_version %d), lu dans sqlite_master uniquement" % (rel, uv), skipped


def annotate(out_dir, origin, skipped):
    readme = os.path.join(out_dir, "README.md")
    if not os.path.exists(readme):
        return
    t = open(readme, encoding="utf-8").read()
    note = ("> ⚙️ Généré par `tools/retro-spec/schema-db.py` (tbls) — ne pas éditer. Source : %s. "
            "Aucune donnée : base reconstruite vide.\n\n" % origin)
    if skipped:
        note += "> Non recréés (module SQLite absent) : %s\n\n" % "; ".join(skipped)
    with open(readme, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(note + t)


def fallback_md(db, out_md, origin, skipped):
    con = sqlite3.connect(db)
    tables = [r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name")]
    L = ["# Schéma de données (généré)", "", "> ⚙️ Généré par `tools/retro-spec/schema-db.py --no-tbls` — ne pas éditer. Source : %s." % origin, ""]
    er = ["```mermaid", "erDiagram"]
    rows = []
    for t in tables:
        cols = con.execute('PRAGMA table_info("%s")' % t).fetchall()
        fks = con.execute('PRAGMA foreign_key_list("%s")' % t).fetchall()
        rows.append("| `%s` | %d | %s |" % (t, len(cols), ", ".join(sorted({"`%s`" % f[2] for f in fks})) or "—"))
        for f in fks:
            er.append('  %s ||--o{ %s : "%s"' % (f[2], t, f[3]))
    er.append("```")
    L += ["| Table | Colonnes | Référence |", "|---|---|---|"] + rows + ["", "## Clés étrangères", ""] + er
    if skipped:
        L += ["", "## Non recréé", ""] + ["- %s" % s for s in skipped]
    con.close()
    os.makedirs(os.path.dirname(out_md), exist_ok=True)
    with open(out_md, "w", encoding="utf-8", newline="\n") as fh:
        fh.write("\n".join(L) + "\n")


def remote_tbls(target, dst, out_dir):
    import posixpath
    import stat
    import paramiko
    user, host = target.split("@", 1)
    pwd = os.environ.get("RETRO_SPEC_SSH_PASSWORD")

    def client():
        c = paramiko.SSHClient()
        c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        c.connect(host, username=user, password=pwd, timeout=15)
        return c

    def run(cmd):
        c = client()
        _, o, e = c.exec_command(cmd, timeout=300)
        rc = o.channel.recv_exit_status()
        res = (rc, o.read().decode("utf-8", "replace"), e.read().decode("utf-8", "replace"))
        c.close()
        return res

    rdir = "/tmp/retro-spec-tbls-%d" % os.getpid()
    run("rm -rf %s && mkdir -p %s/out" % (rdir, rdir))
    c = client()
    sftp = c.open_sftp()
    sftp.put(dst, rdir + "/schema.db")
    with sftp.open(rdir + "/.tbls.yml", "w") as fh:
        fh.write("dsn: sqlite:///work/schema.db\ndocPath: /work/out\nformat:\n  adjust: true\n"
                 "er:\n  format: mermaid\n  distance: 1\n")
    sftp.close()
    c.close()
    rc, o, e = run("docker run --rm -v %s:/work -w /work %s doc --config /work/.tbls.yml --force" % (rdir, TBLS_IMAGE))
    if rc == 0:
        if os.path.isdir(out_dir):
            shutil.rmtree(out_dir)
        c = client()
        sftp = c.open_sftp()

        def get_dir(rd, ld):
            os.makedirs(ld, exist_ok=True)
            for a in sftp.listdir_attr(rd):
                r, l = posixpath.join(rd, a.filename), os.path.join(ld, a.filename)
                if stat.S_ISDIR(a.st_mode):
                    get_dir(r, l)
                else:
                    sftp.get(r, l)
        get_dir(rdir + "/out", out_dir)
        sftp.close()
        c.close()
    run("rm -rf %s" % rdir)
    return rc, (e or o)[-800:]


def main(argv):
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", default=os.environ.get("CLAUDE_PROJECT_DIR") or os.getcwd())
    ap.add_argument("--no-tbls", action="store_true")
    ap.add_argument("--db", default=None, help="FastAPI : base source (défaut data/plexhub.db)")
    ap.add_argument("--remote", default=None, help="user@hôte d'un Docker distant (ex. homelab)")
    a = ap.parse_args(argv)
    root = os.path.abspath(a.root)
    cache = os.path.join(root, ".claude", ".cache", "retro-spec")
    os.makedirs(cache, exist_ok=True)
    dst = os.path.join(cache, "schema.db")
    if os.path.exists(dst):
        os.remove(dst)
    if os.path.exists(os.path.join(root, "settings.gradle.kts")):
        origin, skipped = room_db(root, dst)
    else:
        origin, skipped = sqlite_schema_db(root, dst, a.db)
    out_dir = os.path.join(root, "docs", "retro-spec", "technique", "db")
    print("base vide construite :", origin, "| non recréés :", len(skipped))
    for s in skipped:
        print("   -", s)
    if a.remote:
        rc, msg = remote_tbls(a.remote, dst, out_dir)
        if rc != 0:
            print("tbls distant a échoué (%d) : %s" % (rc, msg))
            fallback_md(dst, os.path.join(out_dir, "schema.md"), origin, skipped)
            return 1
        annotate(out_dir, origin + " — tbls sur Docker distant", skipped)
        print("doc tbls (distant) écrite dans", out_dir)
        return 0
    if a.no_tbls or not shutil.which("docker"):
        fallback_md(dst, os.path.join(out_dir, "schema.md"), origin, skipped)
        print("écrit (sans tbls) :", os.path.join(out_dir, "schema.md"))
        return 0
    cfg = os.path.join(cache, ".tbls.yml")
    with open(cfg, "w", encoding="utf-8", newline="\n") as fh:
        fh.write("dsn: sqlite:///work/.claude/.cache/retro-spec/schema.db\n"
                 "docPath: /work/docs/retro-spec/technique/db\n"
                 "format:\n  adjust: true\n"
                 "er:\n  format: mermaid\n  distance: 1\n")
    if os.path.isdir(out_dir):
        shutil.rmtree(out_dir)
    cmd = ["docker", "run", "--rm", "-v", "%s:/work" % root, "-w", "/work", TBLS_IMAGE,
           "doc", "--config", "/work/.claude/.cache/retro-spec/.tbls.yml", "--force"]
    r = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace")
    if r.returncode != 0:
        print("tbls a échoué (%d) : %s" % (r.returncode, (r.stderr or r.stdout)[-800:]))
        fallback_md(dst, os.path.join(out_dir, "schema.md"), origin, skipped)
        print("repli : schema.md écrit sans tbls")
        return 1
    annotate(out_dir, origin, skipped)
    print("doc tbls écrite dans", out_dir)
    return 0


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.exit(main(sys.argv[1:]))
