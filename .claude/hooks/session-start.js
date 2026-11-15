#!/usr/bin/env node
/* PlexHub Backend — SessionStart : injecte le routeur court de workflows + détecte la péremption de CLAUDE.md + signale la CI rouge.
   Sortie = stdout, injectée dans le contexte de la session (cf. hook SessionStart). Ne jamais throw. */
const fs = require("fs");
const cp = require("child_process");
const path = require("path");

function read(p){ try { return fs.readFileSync(p, "utf8"); } catch { return ""; } }
function git(args){ try { return cp.execFileSync("git", Array.isArray(args) ? args : String(args).split(/\s+/), {stdio:["ignore","pipe","ignore"], windowsHide:true}).toString().trim(); } catch { return ""; } }

// 1) Routeur de workflows — version COURTE (économie de tokens : WORKFLOWS-ROUTER.md ~3 Ko au lieu de WORKFLOWS.md complet).
const wf = read(".claude/WORKFLOWS-ROUTER.md") || read(".claude/WORKFLOWS.md");
if (wf) process.stdout.write(wf + "\n");

// 2) Détecteur de péremption CLAUDE.md
try {
  const claude = read("CLAUDE.md");
  const m = claude.match(/À JOUR AU\s*:\s*([0-9-]+)\s*\(HEAD\s+(?:[^`'\s)]+\s+)?[`']?([0-9a-f]{7,40})[`']?/i);
  const head = git("rev-parse --short HEAD");
  if (m && head) {
    const bannerDate = m[1], bannerHead = m[2];
    const fresh = head.startsWith(bannerHead) || bannerHead.startsWith(head);
    if (!fresh) {
      const since = git(["rev-parse", bannerHead]) ? `${bannerHead}..HEAD` : "";
      const commits = since ? git(["log", "--oneline", since]) : "";
      const nb = commits ? commits.split("\n").length : "?";
      const files = since ? git(["diff", "--name-only", since]).split("\n").filter(Boolean) : [];
      const uniqZones = [...new Set(files.map(p => p.split("/").slice(0, 2).join("/")))].slice(0, 12);

      const STRUCTURAL = [
        "pyproject.toml", "requirements.txt", "requirements-dev.txt",
        "app/db/migrations.py", "app/models/database.py", "app/main.py",
        ".claude/skills/pitfalls/"
      ];
      const structural = files.filter(p => STRUCTURAL.some(k => p.includes(k)));
      const DOC_ONLY = /^(CLAUDE\.md$|docs\/|\.claude\/|[^/]+\.md$)/;
      const docOnly = files.length > 0 && files.every(p => DOC_ONLY.test(p));

      if (docOnly) {
        process.stdout.write(
          `\n✅ CLAUDE.md à jour sur le CODE (HEAD ${head}).\n` +
          `   ℹ️  ${nb} commit(s) documentaire(s) depuis le bandeau ${bannerHead}` +
          (uniqZones.length ? ` — ${uniqZones.join(", ")}` : "") + " ; aucune dérive de code.\n"
        );
      } else if (structural.length || files.length === 0) {
        process.stdout.write(
          "\n========================================================\n" +
          "⚠️  CLAUDE.md PÉRIMÉ — dérive STRUCTURELLE détectée\n" +
          `   Bandeau « À JOUR AU » : ${bannerDate} (HEAD ${bannerHead})\n` +
          `   HEAD réel             : ${head}\n` +
          (nb!=="?" ? `   Commits depuis le bandeau : ${nb}\n` : "") +
          (uniqZones.length ? `   Zones modifiées : ${uniqZones.join(", ")}\n` : "") +
          (structural.length ? `   Fichiers STRUCTURELS touchés (${structural.length}) : ${structural.slice(0,4).join(", ")}\n` : "") +
          "   → Lance **/sync-context** (MAJ légère bandeau+delta) ou **/refresh-context** (re-cartographie complète) AVANT de te fier aux n° de ligne/sections de CLAUDE.md.\n" +
          "   → Tout fait postérieur au bandeau doit être VÉRIFIÉ dans le code.\n" +
          "========================================================\n"
        );
      } else {
        process.stdout.write(
          `\nℹ️  CLAUDE.md retarde de ${nb} commit(s) (HEAD ${bannerHead} → ${head}), ` +
          "mais AUCUNE zone structurelle n'a bougé — recalage non urgent.\n"
        );
      }
    } else {
      process.stdout.write(`\n✅ CLAUDE.md à jour (HEAD ${head}).\n`);
    }
  } else if (head) {
    process.stdout.write(
      "\n========================================================\n" +
      "⚠️  CLAUDE.md — bandeau de fraîcheur introuvable ou non parsable\n" +
      "   Format attendu : « À JOUR AU : <AAAA-MM-JJ> (HEAD [branche] `<hash>` …) »\n" +
      `   HEAD réel : ${head}\n` +
      "   → Le détecteur de dérive est INOPÉRANT tant que le bandeau n'est pas remis au format.\n" +
      "========================================================\n"
    );
  }
} catch (e) { /* silencieux : ne jamais bloquer le démarrage */ }

// 3) Signal de CI ROUGE (cache 10 min / 5 min sur erreur, timeout 6 s, silence si hors ligne).
try {
  if ((process.env.PLEXHUB_CI_SIGNAL || "").toLowerCase() !== "off") {
    const CACHE_DIR = path.join(".claude", ".cache");
    const CACHE = path.join(CACHE_DIR, "ci-status.json");
    const TTL_OK_MS = 10 * 60 * 1000;
    const TTL_ERR_MS = 5 * 60 * 1000;

    const branch = git("rev-parse --abbrev-ref HEAD") || "develop";
    const headSha = git("rev-parse HEAD");

    let payload = null;
    try {
      const c = JSON.parse(read(CACHE) || "null");
      if (c && c.branch === branch) {
        const ttl = c.error ? TTL_ERR_MS : TTL_OK_MS;
        if (Date.now() - c.at < ttl) payload = c;
      }
    } catch { /* cache illisible */ }

    if (!payload) {
      let fresh = { at: Date.now(), branch, error: true, runs: [] };
      try {
        const out = cp.execFileSync(
          "gh",
          ["run", "list", "--workflow=tests.yml", "--branch", branch, "--limit", "1",
           "--json", "conclusion,status,headSha,displayTitle,createdAt,url,event"],
          { stdio: ["ignore", "pipe", "ignore"], timeout: 6000, windowsHide: true }
        ).toString();
        const runs = JSON.parse(out);
        if (Array.isArray(runs)) fresh = { at: Date.now(), branch, error: false, runs };
      } catch { /* gh absent / hors ligne / timeout */ }
      payload = fresh;
      try {
        fs.mkdirSync(CACHE_DIR, { recursive: true });
        fs.writeFileSync(CACHE, JSON.stringify(payload));
      } catch { /* ignore */ }
    }

    if (!payload.error && payload.runs.length) {
      const r = payload.runs[0];
      const sha = (r.headSha || "").slice(0, 8);
      const ageH = Math.round((Date.now() - new Date(r.createdAt).getTime()) / 3600000);
      const age = ageH < 48 ? `il y a ${ageH} h` : `il y a ${Math.round(ageH / 24)} j`;
      const running = r.status !== "completed";

      let coverage = "";
      if (headSha && r.headSha && r.headSha !== headSha) {
        const n = git(["rev-list", "--count", `${r.headSha}..HEAD`]);
        coverage = n && Number(n) > 0 ? ` — ${n} commit(s) locaux plus récents` : ` — HEAD ≠ ${sha}`;
      }

      if (running) {
        process.stdout.write(`ℹ️  CI (${branch}) : run en cours (${sha}, ${age})${coverage}.\n`);
      } else if (r.conclusion === "success") {
        if (coverage) process.stdout.write(`ℹ️  CI (${branch}) : vert sur ${sha} (${age})${coverage}.\n`);
      } else {
        process.stdout.write(
          "\n========================================================\n" +
          `🔴 CI ROUGE sur ${branch} (${r.conclusion || r.status}) — run ${sha} (${age})${coverage}\n` +
          (r.displayTitle ? `   Commit : ${r.displayTitle}\n` : "") +
          (r.url ? `   Log    : ${r.url}\n` : "") +
          "========================================================\n"
        );
      }
    }
  }
} catch (e) { /* silencieux */ }
