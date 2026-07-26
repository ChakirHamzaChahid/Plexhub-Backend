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
  // Le bandeau réel a la forme : « À JOUR AU : 2026-07-26 (HEAD develop `9da9d46`, release **v1.7.1**). »
  // → nom de branche OPTIONNEL entre « HEAD » et le hash, et texte libre avant la parenthèse fermante.
  // Ne surtout PAS réexiger le « ) » juste après le hash : c'est ce qui rendait ce détecteur muet
  // (regex NO MATCH → `m` null → aucune sortie, échec avalé). Cf. audit v1, AUDIT-P7-003.
  const m = claude.match(/À JOUR AU\s*:\s*([0-9-]+)\s*\(HEAD\s+(?:[^`'\s)]+\s+)?[`']?([0-9a-f]{7,40})[`']?/i);
  const head = git("rev-parse --short HEAD");
  if (m && head) {
    const bannerDate = m[1], bannerHead = m[2];
    const fresh = head.startsWith(bannerHead) || bannerHead.startsWith(head);
    if (!fresh) {
      const since = git(`rev-parse ${bannerHead}`) ? `${bannerHead}..HEAD` : "";
      const commits = since ? git(`log --oneline ${since}`) : "";
      const nb = commits ? commits.split("\n").length : "?";
      const files = since ? git(`diff --name-only ${since}`).split("\n").filter(Boolean) : [];
      const uniqZones = [...new Set(files.map(p => p.split("/").slice(0, 2).join("/")))].slice(0, 12);

      // Un commit /sync-context ne peut pas contenir son propre hash : le bandeau est
      // donc TOUJOURS en retard d'au moins ce commit-là. Crier « PÉRIMÉ » là-dessus
      // rendrait l'alerte permanente — donc du bruit qu'on apprend à survoler, et un
      // détecteur qu'on ignore ne vaut pas mieux que le détecteur muet qu'on vient de
      // réparer. On ne dégrade en note informative QUE si la dérive est 100 %
      // documentaire ; le moindre fichier de code rétablit l'alerte forte.
      // Défaut sûr : `files` vide (bandeau inconnu de git) ⇒ alerte forte.
      const DOC_ONLY = /^(CLAUDE\.md$|docs\/|\.claude\/|[^/]+\.md$)/;
      const docOnly = files.length > 0 && files.every(p => DOC_ONLY.test(p));
      if (docOnly) {
        process.stdout.write(
          `\n✅ CLAUDE.md à jour sur le CODE (HEAD ${head}).\n` +
          `   ℹ️  ${nb} commit(s) documentaire(s) depuis le bandeau ${bannerHead}` +
          (uniqZones.length ? ` — ${uniqZones.join(", ")}` : "") + " ; aucune dérive de code.\n"
        );
      } else {
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
      }
    } else {
      process.stdout.write(`\n✅ CLAUDE.md à jour (HEAD ${head}).\n`);
    }
  } else if (head) {
    // Bandeau absent ou non parsable : SANS ce message le détecteur est aveugle en silence
    // (mode de panne réel constaté par l'audit v1). Un détecteur muet doit se signaler.
    process.stdout.write(
      "\n========================================================\n" +
      "⚠️  CLAUDE.md — bandeau de fraîcheur introuvable ou non parsable\n" +
      "   Format attendu : « À JOUR AU : <AAAA-MM-JJ> (HEAD [branche] `<hash>` …) »\n" +
      `   HEAD réel : ${head}\n` +
      "   → Le détecteur de dérive est INOPÉRANT tant que le bandeau n'est pas remis au format.\n" +
      "   → Ne te fie à aucun n° de ligne/section de CLAUDE.md sans vérifier dans le code.\n" +
      "========================================================\n"
    );
  }
} catch (e) { /* silencieux : ne jamais bloquer le démarrage */ }
