#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
dod-gate — rend la Definition of Done DETERMINISTE pour PlexHub Backend.

Principe : verifie que la DoD a ete jouee VERTE depuis la derniere
modification de code, via deux marqueurs :
    touch  (hook PostToolUse sur Edit|Write|MultiEdit)  -> marqueur SALE
    run    (lance par l'agent ou par toi)               -> marqueur VERT
    check  (hook Stop)                                  -> SALE > VERT = exit 2

Commandes :
    python .claude/tools/dod-gate.py run [--fast] [--only=a,b] [--verbose]
    python .claude/tools/dod-gate.py check          # hook Stop
    python .claude/tools/dod-gate.py touch          # hook PostToolUse (stdin)
    python .claude/tools/dod-gate.py status
    python .claude/tools/dod-gate.py verify [--full]   # gate de lot / release
    python .claude/tools/dod-gate.py on | off

Coupe-circuit : PLEXHUB_DOD_GATE=off, ou `off`.
"""
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
except Exception:
    pass

ROOT = Path(os.environ.get("CLAUDE_PROJECT_DIR") or Path(__file__).resolve().parents[2])
STATE = ROOT / ".claude" / ".gate"
DIRTY = STATE / "dirty"
GREEN = STATE / "green"
BLOCKS = STATE / "blocks"
DISABLED = STATE / "disabled"
MAX_BLOCKS = 3
RUNNING = STATE / "running"
RUNNING_MAX_S = 90 * 60

SOURCE_SUFFIXES = (".py", ".sql")
SOURCE_NAMES = ("pyproject.toml", "requirements.txt", "requirements-dev.txt", "Dockerfile")


def resolve_python():
    """Trouve l'interpreteur Python du venv projet (avec pytest), sinon fallback."""
    env = os.environ.get("PLEXHUB_DOD_PYTHON")
    if env and Path(env).is_file():
        return env, "override PLEXHUB_DOD_PYTHON"
    candidates = [
        ROOT / ".venv" / "Scripts" / "python.exe",
        ROOT / ".venv" / "bin" / "python",
        Path(sys.executable),
    ]
    for c in candidates:
        if c.is_file():
            return str(c), str(c)
    return None, None


def resolve_ruff():
    """Trouve l'executable ruff du venv projet ou dans PATH."""
    candidates = [
        ROOT / ".venv" / "Scripts" / "ruff.exe",
        ROOT / ".venv" / "bin" / "ruff",
    ]
    for c in candidates:
        if c.is_file():
            return str(c)
    w = shutil.which("ruff")
    return w


def build_checks(fast=False):
    py, _ = resolve_python()
    ruff = resolve_ruff()
    if not py:
        return None, "interpreteur Python introuvable (.venv/Scripts/python.exe ou sys.executable)"
    if not ruff:
        return None, "ruff introuvable (.venv/Scripts/ruff.exe ou PATH)"
    pytest_cmd = [py, "-m", "pytest", "-q"] + (["--no-cov"] if fast else [])
    boot_cmd = [
        py, "-c",
        "import os, tempfile; d = tempfile.mkdtemp(); "
        "os.environ['DATA_DIR'] = d; os.environ['LOG_DIR'] = d; "
        "os.environ.setdefault('BACKGROUND_JOBS_ENABLED', 'false'); "
        "from fastapi.testclient import TestClient; from app.main import app; "
        "exec('with TestClient(app) as c:\\n r = c.get(\"/api/health\")\\n assert r.status_code == 200, r.text\\n print(\"health:\", r.status_code)')"
    ]
    return [
        ("ruff", [ruff, "check", "."]),
        ("pytest", pytest_cmd),
        ("boot", boot_cmd),
    ], None


FAST = {"ruff", "pytest"}


def enabled():
    if os.environ.get("PLEXHUB_DOD_GATE", "").lower() in ("off", "0", "false"):
        return False
    return not DISABLED.exists()


def mtime(p):
    try:
        return p.stat().st_mtime
    except OSError:
        return 0.0


def read_json(p, default):
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return default


def write_json(p, obj):
    STATE.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(obj, ensure_ascii=False, indent=1), encoding="utf-8")


def _pid_alive(pid):
    if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0:
        return False
    if os.name == "nt":
        try:
            import ctypes
            k32 = ctypes.windll.kernel32
            k32.OpenProcess.restype = ctypes.c_void_p
            k32.OpenProcess.argtypes = (ctypes.c_ulong, ctypes.c_int, ctypes.c_ulong)
            k32.GetExitCodeProcess.argtypes = (ctypes.c_void_p, ctypes.POINTER(ctypes.c_ulong))
            k32.CloseHandle.argtypes = (ctypes.c_void_p,)
            h = k32.OpenProcess(0x1000, False, pid)
            if not h:
                return False
            try:
                code = ctypes.c_ulong()
                if not k32.GetExitCodeProcess(h, ctypes.byref(code)):
                    return False
                return code.value == 259
            finally:
                k32.CloseHandle(h)
        except Exception:
            return False
    try:
        os.kill(pid, 0)
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def run_in_progress():
    st = read_json(RUNNING, None)
    if not isinstance(st, dict):
        if RUNNING.exists():
            RUNNING.unlink(missing_ok=True)
        return None
    try:
        age = time.time() - float(st.get("at") or 0)
    except (TypeError, ValueError):
        age = -1
    if 0 <= age <= RUNNING_MAX_S and _pid_alive(st.get("pid")):
        st["age_s"] = int(age)
        return st
    RUNNING.unlink(missing_ok=True)
    return None


def is_source(path):
    if not path:
        return False
    p = path.replace("\\", "/")
    name = p.rsplit("/", 1)[-1]
    if name in SOURCE_NAMES:
        return True
    return p.endswith(SOURCE_SUFFIXES)


def rel(path):
    try:
        return str(Path(path).resolve().relative_to(ROOT)).replace("\\", "/")
    except Exception:
        return path


def cmd_touch():
    """Hook PostToolUse : marque SALE si le fichier edite est du source."""
    raw = sys.stdin.read() if not sys.stdin.isatty() else ""
    ev = {}
    try:
        ev = json.loads(raw or "{}")
    except Exception:
        return 0
    ti = ev.get("tool_input") or {}
    paths = []
    for key in ("file_path", "notebook_path"):
        if isinstance(ti.get(key), str):
            paths.append(ti[key])
    for key in ("edits", "files"):
        v = ti.get(key)
        if isinstance(v, list):
            for item in v:
                if isinstance(item, dict):
                    for k in ("file_path", "path"):
                        if isinstance(item.get(k), str):
                            paths.append(item[k])
    hits = [p for p in paths if is_source(p)]
    if not hits:
        return 0
    cur = read_json(DIRTY, {"files": []})
    files = cur.get("files", [])
    for p in hits:
        r = rel(p)
        if r not in files:
            files.append(r)
    write_json(DIRTY, {"at": time.time(), "when": time.strftime("%Y-%m-%d %H:%M:%S"),
                       "files": files[-40:]})
    return 0


def _git(args, timeout=30, stdin=None):
    return subprocess.run(["git"] + args, cwd=ROOT, input=stdin, capture_output=True,
                          text=True, encoding="utf-8", errors="replace", timeout=timeout)


def source_fingerprint():
    try:
        head = _git(["rev-parse", "HEAD"]).stdout.strip() or None
        tree = _git(["ls-tree", "-r", "-z", "HEAD"], timeout=60).stdout
        status = _git(["status", "--porcelain", "-z", "--untracked-files=all"], timeout=60).stdout
    except Exception as e:
        return None, None, "git indisponible : %s" % e
    blobs = {}
    for rec in tree.split("\0"):
        meta, _, path = rec.partition("\t")
        parts = meta.split()
        if len(parts) == 3 and parts[1] == "blob" and is_source(path):
            blobs[path] = parts[2]
    entries, changed, i = status.split("\0"), [], 0
    while i < len(entries):
        e = entries[i]
        i += 1
        if len(e) < 4:
            continue
        xy, path = e[:2], e[3:]
        if xy[0] in "RC" and i < len(entries):
            old = entries[i]
            i += 1
            if is_source(old):
                blobs.pop(old, None)
        if is_source(path):
            changed.append(path)
    present = [p for p in changed if (ROOT / p).is_file()]
    for p in changed:
        if p not in present:
            blobs.pop(p, None)
    if present:
        try:
            out = _git(["hash-object", "--stdin-paths"], timeout=120,
                       stdin="\n".join(present) + "\n").stdout.split()
        except Exception as e:
            return None, head, "git hash-object : %s" % e
        if len(out) != len(present):
            return None, head, "git hash-object incoherent (%d/%d)" % (len(out), len(present))
        blobs.update(zip(present, out))
    h = hashlib.sha256()
    for p in sorted(blobs):
        h.update(("%s %s\n" % (p, blobs[p])).encode("utf-8"))
    return h.hexdigest(), head, None


def git_dirty():
    if os.environ.get("PLEXHUB_DOD_GATE_GIT", "").lower() in ("off", "0", "false"):
        return 0.0, []
    try:
        out = subprocess.run(["git", "status", "--porcelain", "--untracked-files=all"],
                             cwd=ROOT, capture_output=True, text=True,
                             encoding="utf-8", errors="replace", timeout=15).stdout
    except Exception:
        return 0.0, []
    newest, files = 0.0, []
    for line in out.split("\n"):
        path = line[3:].strip().strip('"')
        if " -> " in path:
            path = path.split(" -> ")[-1]
        if not path or not is_source(path):
            continue
        m = mtime(ROOT / path)
        files.append(path)
        newest = max(newest, m)
    return newest, files


def cmd_check():
    """Hook Stop : refuse de clore le tour si du source est non verifie."""
    if not enabled():
        return 0
    d, g = mtime(DIRTY), mtime(GREEN)
    gd, gfiles = git_dirty()
    d = max(d, gd)
    if d == 0 or d <= g:
        BLOCKS.unlink(missing_ok=True)
        return 0
    green_fp = read_json(GREEN, {}).get("fingerprint")
    if green_fp:
        try:
            cur_fp, _, _ = source_fingerprint()
        except Exception:
            cur_fp = None
        if cur_fp and cur_fp == green_fp:
            BLOCKS.unlink(missing_ok=True)
            return 0

    if run_in_progress():
        return 0

    st = read_json(BLOCKS, {})
    files = read_json(DIRTY, {}).get("files", [])
    for f in gfiles:
        if f not in files:
            files.append(f)
    shown = "\n".join("    - %s" % f for f in files[:12])
    more = "\n    - … et %d autres" % (len(files) - 12) if len(files) > 12 else ""

    if st.get("gave_up_for") == d:
        return 0
    n = (st.get("n", 0) if st.get("for") == d else 0) + 1
    if n > MAX_BLOCKS:
        write_json(BLOCKS, {"gave_up_for": d})
        print("[dod-gate] %d blocages consecutifs — je n'insiste plus tant que "
              "rien ne change. La DoD reste NON verifiee sur :\n%s%s"
              % (MAX_BLOCKS, shown, more), file=sys.stderr)
        return 0

    write_json(BLOCKS, {"n": n, "for": d})
    print(
        "DoD NON VERIFIEE — %d fichier(s) source modifie(s) sans controle vert "
        "depuis :\n%s%s\n\n"
        "Lance la DoD, puis reprends :\n"
        "    python .claude/tools/dod-gate.py run\n\n"
        "Elle joue ruff + pytest (avec couverture) + boot /api/health et n'ecrit le "
        "marqueur vert que si TOUT passe. Version rapide (ruff + pytest --no-cov) :\n"
        "    python .claude/tools/dod-gate.py run --fast\n\n"
        "Si ces modifications n'ont deliberement pas a etre verifiees, dis-le a "
        "l'utilisateur et neutralise le gate pour la session :\n"
        "    python .claude/tools/dod-gate.py off\n"
        "(blocage %d/%d — au-dela je n'insiste plus)"
        % (len(files), shown, more, n, MAX_BLOCKS), file=sys.stderr)
    return 2


LOG_DIR = ROOT / ".claude" / ".cache" / "dod-gate"
_FAIL_RE = re.compile(
    r"(FAILED|ERROR|AssertionError|Traceback|SyntaxError|ImportError|ModuleNotFoundError|"
    r"^\s*E\s+|:\d+:\d+: [EFWI]\d+|FAIL Required test coverage)")
_EXCERPT_MAX = 60


def print_failure_excerpt(log):
    try:
        lines = log.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return
    seen, hits = set(), []
    for ln in lines:
        s = ln.rstrip()
        if s and _FAIL_RE.search(s) and s not in seen:
            seen.add(s)
            hits.append(s[:300])
    shown = hits[:_EXCERPT_MAX]
    print("    --- extrait des erreurs (%d/%d lignes retenues) ---" % (len(shown), len(hits)))
    for s in shown:
        print("    " + s)
    if not hits:
        print("    --- aucune ligne d'erreur reconnue : fin du journal ---")
        for s in [ln[:300] for ln in lines[-15:]]:
            print("    " + s)
    print("    journal complet : %s  (lire par plages si besoin)" % rel(log))


def cmd_run(argv):
    other = run_in_progress()
    if other and other.get("pid") != os.getpid():
        print("dod-gate: une DoD tourne deja (pid %s, depuis %d min) — attends sa fin.\n"
              "  Ce n'est NI vert NI rouge, c'est non execute."
              % (other.get("pid"), other.get("age_s", 0) // 60), file=sys.stderr)
        return 3
    write_json(RUNNING, {"pid": os.getpid(), "at": time.time(),
                         "when": time.strftime("%Y-%m-%d %H:%M:%S"), "argv": argv})
    try:
        return _run_checks(argv)
    finally:
        RUNNING.unlink(missing_ok=True)


def _run_checks(argv):
    fast = "--fast" in argv
    verbose = "--verbose" in argv
    only = None
    for a in argv:
        if a.startswith("--only="):
            only = {x.strip() for x in a.split("=", 1)[1].split(",") if x.strip()}
    checks, err = build_checks(fast=fast)
    if err:
        print("dod-gate: %s\n  La DoD n'a PAS ete jouee — c'est non execute." % err, file=sys.stderr)
        return 3
    todo = [(n, c) for n, c in checks
            if (only is None or n in only) and (not fast or n in FAST)]
    if not todo:
        print("dod-gate: aucun controle selectionne.", file=sys.stderr)
        return 1

    print("dod-gate run — %d controle(s)%s\n" % (len(todo), " (--fast)" if fast else ""))
    results, failed, unrun = [], [], []
    for name, cmd in todo:
        t0 = time.time()
        print("  > %-9s %s" % (name, " ".join(cmd)), flush=True)
        launched = True
        log = LOG_DIR / ("%s.log" % name)
        try:
            if verbose:
                p = subprocess.run(cmd, cwd=ROOT, shell=False)
            else:
                LOG_DIR.mkdir(parents=True, exist_ok=True)
                with open(log, "w", encoding="utf-8", errors="replace") as fh:
                    p = subprocess.run(cmd, cwd=ROOT, shell=False,
                                       stdout=fh, stderr=subprocess.STDOUT)
            rc = p.returncode
        except Exception as e:
            rc, launched = -1, False
            print("    ERREUR de lancement : %s" % e, file=sys.stderr)
        dt = time.time() - t0
        if launched and rc != 0 and not verbose:
            print_failure_excerpt(log)
        ok = launched and rc == 0
        status = "VERT" if ok else ("ROUGE" if launched else "NON-EXECUTE")
        results.append({"check": name, "cmd": " ".join(cmd), "exit": rc,
                        "seconds": round(dt, 1), "ok": ok, "status": status})
        print("    %s  exit=%s  %.0fs\n" % (status, rc, dt), flush=True)
        if not ok:
            (failed if launched else unrun).append(name)

    print("== PREUVES ==")
    for r in results:
        print("  %-9s exit=%-4s %5.0fs  %s" % (r["check"], r["exit"], r["seconds"], r["status"]))
    if unrun:
        print("\nDoD NON EXECUTEE sur : %s — probleme d'outillage, pas de code." % ", ".join(unrun),
              file=sys.stderr)
        return 3
    if failed:
        print("\nDoD ROUGE sur : %s — marqueur vert NON ecrit." % ", ".join(failed), file=sys.stderr)
        return 1

    STATE.mkdir(parents=True, exist_ok=True)
    fp, head, why = source_fingerprint()
    if fp is None:
        print("dod-gate: empreinte du code non calculee (%s) — `verify` le refusera." % why,
              file=sys.stderr)
    write_json(GREEN, {"at": time.time(), "when": time.strftime("%Y-%m-%d %H:%M:%S"),
                       "fast": fast, "complete": (not fast and only is None),
                       "head": head, "fingerprint": fp, "checks": results})
    DIRTY.unlink(missing_ok=True)
    BLOCKS.unlink(missing_ok=True)
    scope = "PARTIELLE (--fast)" if fast else "COMPLETE"
    print("\nDoD %s VERTE — marqueur pose, le gate laisse passer." % scope)
    if fast:
        print("⚠️  --fast n'a pas joue couverture ni boot /api/health. Avant de clore un lot, rejoue la DoD complete.")
    return 0


def cmd_verify(argv):
    full = "--full" in argv
    green = read_json(GREEN, None)
    if not green:
        print("dod-gate verify : AUCUNE DoD verte enregistree. Lance :\n"
              "    python .claude/tools/dod-gate.py run", file=sys.stderr)
        return 1
    bad = [c.get("check") for c in green.get("checks", []) if not c.get("ok")]
    if bad:
        print("dod-gate verify : manifeste incoherent (%s)." % ", ".join(bad), file=sys.stderr)
        return 1
    complete = green.get("complete", not green.get("fast", False))
    if full and not complete:
        print("dod-gate verify --full : la derniere DoD verte etait PARTIELLE. Rejoue :\n"
              "    python .claude/tools/dod-gate.py run", file=sys.stderr)
        return 1
    want = green.get("fingerprint")
    if not want:
        print("dod-gate verify : manifeste sans empreinte. Rejoue la DoD.", file=sys.stderr)
        return 1
    cur, head, why = source_fingerprint()
    if cur is None:
        print("dod-gate verify : empreinte non calculable (%s)." % why, file=sys.stderr)
        return 1
    if cur != want:
        print("dod-gate verify : le CODE A CHANGE depuis la DoD verte du %s "
              "(HEAD alors %s, maintenant %s)."
              % (green.get("when"), (green.get("head") or "?")[:10], (head or "?")[:10]),
              file=sys.stderr)
        return 1
    print("dod-gate verify : DoD %s VERTE sur ce code exact (vert du %s, HEAD %s)."
          % ("COMPLETE" if complete else "PARTIELLE", green.get("when"), (head or "?")[:10]))
    return 0


def cmd_status():
    g = mtime(GREEN)
    gd, gfiles = git_dirty()
    d = max(mtime(DIRTY), gd)
    files = list(read_json(DIRTY, {}).get("files", []))
    for f in gfiles:
        if f not in files:
            files.append(f)
    print("dod-gate — %s" % ROOT)
    print("  actif            : %s" % ("oui" if enabled() else "NON (coupe-circuit)"))
    running = run_in_progress()
    print("  DoD en cours     : %s" % (
        "oui (pid %s, depuis %d min)" % (running.get("pid"), running.get("age_s", 0) // 60)
        if running else "non"))
    green = read_json(GREEN, {})
    print("  dernier vert     : %s%s" % (green.get("when") or "jamais",
                                        "" if not green else (" (complet)" if green.get("complete") else " (partiel)")))
    if green.get("fingerprint"):
        cur, _, _ = source_fingerprint()
        print("  manifeste        : HEAD %s · %s" % ((green.get("head") or "?")[:10],
              "le code actuel est CELUI verifie" if cur == green["fingerprint"]
              else "le code a change depuis"))
    print("  source non verifie: %s" % (
        time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(d)) if d else "aucun"))
    for f in files[:15]:
        print("      - %s" % f)
    blocking = enabled() and d and d > g
    print("  verdict          : %s" % (
        "laisse passer (DoD en cours)" if (blocking and running)
        else "BLOQUERAIT" if blocking else "laisse passer"))
    return 0


def main():
    argv = sys.argv[1:]
    action = argv[0] if argv else "status"
    if action == "touch":
        return cmd_touch()
    if action == "check":
        return cmd_check()
    if action == "run":
        return cmd_run(argv[1:])
    if action == "verify":
        return cmd_verify(argv[1:])
    if action == "off":
        STATE.mkdir(parents=True, exist_ok=True)
        DISABLED.write_text("off %s\n" % time.strftime("%Y-%m-%d %H:%M:%S"), encoding="utf-8")
        print("dod-gate DESACTIVE. Reactive avec : dod-gate.py on")
        return 0
    if action == "on":
        DISABLED.unlink(missing_ok=True)
        print("dod-gate reactive.")
        return 0
    return cmd_status()


if __name__ == "__main__":
    sys.exit(main())
