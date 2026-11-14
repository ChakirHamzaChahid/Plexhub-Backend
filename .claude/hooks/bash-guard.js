#!/usr/bin/env node
/**
 * bash-guard — hook PreToolUse (outils Bash / PowerShell) pour PlexHub Backend.
 *
 * Rend EXÉCUTOIRES des interdits jusqu'ici écrits en prose :
 *   1. secrets : aucune commande qui lit/affiche `.env` (`.env.example` autorisé)
 *      ni `gh secret set|delete|remove` ;
 *   2. git : pas de `add -A|.|-u` sans chemins, pas de `commit -a`, pas de
 *      `push --force`, `reset --hard`, `clean -f`, `checkout -- .`, `restore .`,
 *      pas de branche ni de worktree (branche unique `develop`) ;
 *   3. base de données locale : pas de suppression brutale de `data/*.db` sans
 *      accord explicite de Chakir.
 *
 * Coupe-circuit : BASH_GUARD=off (à dire à Chakir, jamais en silence).
 * Entrée illisible : laisse passer.
 */
const ENV_SECRET = /(^|[\s'"=:/\\(])\.env(?=$|[\s'"&;|)])/i;
const GH_SECRET_WRITE = /\bgh\s+secret\s+(set|delete|remove|rm)\b/i;
const DB_WIPE = /\b(rm|del|remove-item|unlink)\b[^\n]*(data[\\/].*\.db\b|plexhub\.db\b)/i;

function segments(cmd) {
  return cmd.split(/&&|\|\||;|\||\n/).map((s) => s.trim()).filter(Boolean);
}

function tokens(seg) {
  const out = [];
  const re = /"([^"]*)"|'([^']*)'|(\S+)/g;
  let m;
  while ((m = re.exec(seg))) out.push(m[1] ?? m[2] ?? m[3]);
  return out;
}

function gitProblem(tok) {
  let i = 1;
  while (i < tok.length && tok[i].startsWith("-")) {
    if (tok[i] === "-c" || tok[i] === "-C") i += 2; else i += 1;
  }
  const sub = tok[i];
  const args = tok.slice(i + 1);
  const has = (...f) => args.some((a) => f.includes(a));
  const dd = args.indexOf("--");
  const paths = dd >= 0 ? args.slice(dd + 1) : args.filter((a) => !a.startsWith("-"));
  const bare = paths.some((p) => p === "." || p === "*" || p === ":/" || p === "./");
  switch (sub) {
    case "add":
      if (bare) return "`git add .` interdit : liste les chemins explicitement";
      if (has("-A", "--all", "-u", "--update") && paths.length === 0)
        return "`git add -A/-u` sans chemins interdit : `git add <chemins>` explicite";
      return null;
    case "commit":
      if (args.some((a) => /^-[a-zA-Z]*a[a-zA-Z]*$/.test(a) && !a.startsWith("--")) || has("--all"))
        return "`git commit -a` interdit : il prend tous les fichiers suivis ; `git add <chemins>` puis `git diff --cached --name-only`";
      return null;
    case "push":
      if (has("--force", "-f", "--force-with-lease", "--force-if-includes") || args.some((a) => a.startsWith("+")))
        return "`git push --force` interdit";
      return null;
    case "reset":
      if (has("--hard", "--merge", "--keep")) return "`git reset --hard` interdit (perte de travail non commité)";
      return null;
    case "clean":
      if (args.some((a) => /^-[a-zA-Z]*f/.test(a) || a === "--force")) return "`git clean -f` interdit (supprime des fichiers non suivis)";
      return null;
    case "checkout":
      if (has("-b", "-B", "--orphan")) return "création de branche interdite : branche unique `develop`";
      if (dd >= 0 && bare) return "`git checkout -- .` interdit (annule tout le travail non commité)";
      return null;
    case "switch":
      if (has("-c", "-C", "--create", "--force-create", "--orphan")) return "création de branche interdite : branche unique `develop`";
      return null;
    case "branch":
      if (paths.length > 0 && !has("-d", "-D", "--delete", "-l", "--list", "-a", "-r", "-v", "-vv", "--show-current", "--contains", "--merged", "--no-merged"))
        return "création de branche interdite : branche unique `develop`";
      return null;
    case "worktree":
      if (args[0] === "add") return "worktree interdit : branche unique `develop`, un seul rédacteur";
      return null;
    case "restore":
      if (bare && !has("--staged", "-S")) return "`git restore .` interdit (annule tout le travail non commité)";
      return null;
    default:
      return null;
  }
}

function problem(cmd) {
  if (ENV_SECRET.test(cmd)) return "commande qui touche à `.env` (secrets locaux) : INTERDIT. Utilise `.env.example` ou demande à Chakir";
  if (GH_SECRET_WRITE.test(cmd)) return "`gh secret set/delete/remove` : INTERDIT ABSOLU. Arrête-toi et demande à Chakir";
  if (DB_WIPE.test(cmd)) return "suppression directe d'une base SQLite dans `data/` : interdite sans accord explicite de Chakir";
  for (const seg of segments(cmd)) {
    const tok = tokens(seg);
    const k = tok.findIndex((t) => t === "git" || /[\\/]git(\.exe)?$/i.test(t));
    if (k >= 0) {
      const p = gitProblem(tok.slice(k));
      if (p) return p;
    }
  }
  return null;
}

let raw = "";
process.stdin.setEncoding("utf8");
process.stdin.on("data", (c) => { raw += c; });
process.stdin.on("end", () => {
  if ((process.env.BASH_GUARD || "").toLowerCase() === "off") process.exit(0);
  let data;
  try { data = JSON.parse(raw.replace(/^\uFEFF/, "") || "{}"); } catch { process.exit(0); }
  const tool = data.tool_name || "";
  if (tool !== "Bash" && tool !== "PowerShell") process.exit(0);
  const cmd = (data.tool_input && data.tool_input.command) || "";
  if (!cmd) process.exit(0);
  const p = problem(cmd);
  if (p) {
    process.stderr.write(`bash-guard : ${p}.\n`);
    process.exit(2);
  }
  process.exit(0);
});
