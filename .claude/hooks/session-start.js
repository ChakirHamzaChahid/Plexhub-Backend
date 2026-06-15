#!/usr/bin/env node
/* PlexHub Backend — SessionStart : injecte le routeur de workflows + détecte la péremption de CLAUDE.md.
   Sortie = stdout, injectée dans le contexte de la session (cf. hook SessionStart). Ne jamais throw. */
const fs = require("fs");
const cp = require("child_process");

function read(p){ try { return fs.readFileSync(p, "utf8"); } catch { return ""; } }
function git(args){ try { return cp.execSync("git " + args, {stdio:["ignore","pipe","ignore"]}).toString().trim(); } catch { return ""; } }

// 1) Routeur de workflows
const wf = read(".claude/WORKFLOWS.md");
if (wf) process.stdout.write(wf + "\n");

// 2) Détecteur de péremption CLAUDE.md
try {
  const claude = read("CLAUDE.md");
  const m = claude.match(/À JOUR AU\s*:\s*([0-9-]+)\s*\(HEAD\s*[`']?([0-9a-f]{7,40})[`']?\)/i);
  const head = git("rev-parse --short HEAD");
  if (m && head) {
    const bannerDate = m[1], bannerHead = m[2];
    const fresh = head.startsWith(bannerHead) || bannerHead.startsWith(head);
    if (!fresh) {
      const since = git(`rev-parse ${bannerHead}`) ? `${bannerHead}..HEAD` : "";
      const commits = since ? git(`log --oneline ${since}`) : "";
      const nb = commits ? commits.split("\n").length : "?";
      const zones = since ? git(`diff --name-only ${since}`)
        .split("\n").map(p=>p.split("/").slice(0,2).join("/")).filter(Boolean) : [];
      const uniqZones = [...new Set(zones)].slice(0, 12);
      process.stdout.write(
        "\n========================================================\n" +
        "⚠️  CLAUDE.md PÉRIMÉ — dérive détectée\n" +
        `   Bandeau « À JOUR AU » : ${bannerDate} (HEAD ${bannerHead})\n` +
        `   HEAD réel             : ${head}\n` +
        (nb!=="?" ? `   Commits depuis le bandeau : ${nb}\n` : "") +
        (uniqZones.length ? `   Zones modifiées : ${uniqZones.join(", ")}\n` : "") +
        "   → Lance **/sync-context** (MAJ légère bandeau+delta) ou **/refresh-context** (re-cartographie complète) AVANT de te fier aux n° de ligne/sections de CLAUDE.md.\n" +
        "   → Tout fait postérieur au bandeau doit être VÉRIFIÉ dans le code.\n" +
        "========================================================\n"
      );
    } else {
      process.stdout.write(`\n✅ CLAUDE.md à jour (HEAD ${head}).\n`);
    }
  }
} catch (e) { /* silencieux : ne jamais bloquer le démarrage */ }
