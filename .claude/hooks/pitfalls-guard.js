#!/usr/bin/env node
/**
 * pitfalls-guard — hook PreToolUse pour PlexHub Backend.
 *
 * Quand un agent s'apprête à éditer un fichier, lui rappelle les pièges connus
 * du domaine concerné (.claude/skills/pitfalls/).
 *
 * CE HOOK NE BLOQUE JAMAIS ET NE DÉCIDE D'AUCUNE PERMISSION.
 * Déduplication par (session_id, agent_id, domain) sur 24 h.
 */
const fs = require("fs");
const path = require("path");

const ROOT = process.env.CLAUDE_PROJECT_DIR || process.cwd();
const INDEX = path.join(ROOT, ".claude", "skills", "pitfalls", "SKILL.md");
const INDEX_DIR = path.join(ROOT, ".claude", "skills", "pitfalls", "index");
const MAX_CHARS = 2600;
const SEEN = path.join(ROOT, ".claude", ".cache", "pitfalls-seen.json");
const SEEN_TTL_MS = 24 * 3600 * 1000;

function alreadyShown(ev, domain) {
  const who = `${ev.session_id || "?"}|${ev.agent_id || "main"}|${domain}`;
  let seen = {};
  try { seen = JSON.parse(fs.readFileSync(SEEN, "utf8")) || {}; } catch (e) { seen = {}; }
  const now = Date.now();
  for (const k of Object.keys(seen)) if (now - seen[k] > SEEN_TTL_MS) delete seen[k];
  const hit = Boolean(seen[who]);
  seen[who] = now;
  try { fs.mkdirSync(path.dirname(SEEN), { recursive: true }); fs.writeFileSync(SEEN, JSON.stringify(seen)); } catch (e) {}
  return hit;
}

// Premier motif qui matche gagne : les domaines les plus spécifiques d'abord.
const ROUTES = [
  ["db-migrations",     /(migrations|db_retry|immediate_tx|backup_db|maintenance|database\.py|flock)/i],
  ["scrape-enrichment", /(scrape|tmdb|omdb|poster_match|manual_scrape|match_locked|enrichment)/i],
  ["sync-xtream",       /(sync|xtream|epg|outage|aggregation|category_service|adult)/i],
  ["api-auth",          /(route_audit|deps\.py|api_key|tv_auth|metadata_v1|metadata_|ssrf|metrics|admin\.py|base\.html)/i],
  ["ai-media-plex",     /(embedding|recommendation|ollama|subtitle|download|dav|plex_generator|nfo|vec0)/i],
  ["ops-runtime",       /(main\.py|config\.py|Dockerfile|docker-compose|requirements|pyproject\.toml)/i],
];

function targetPath(input) {
  if (!input || typeof input !== "object") return "";
  if (typeof input.file_path === "string") return input.file_path;
  if (typeof input.notebook_path === "string") return input.notebook_path;
  for (const key of ["edits", "files"]) {
    const v = input[key];
    if (Array.isArray(v) && v.length && typeof v[0] === "object") {
      const p = v[0].file_path || v[0].path;
      if (typeof p === "string") return p;
    }
  }
  return "";
}

const STOP = new Set(["app", "services", "api", "workers", "models", "utils", "scripts", "tests",
  "test", "py", "html", "json", "toml", "yml", "yaml", "chakir", "plexhub", "backend", "users"]);

function relevanceWords(rel) {
  const parts = rel.replace(/\.[a-z0-9]+$/i, "").split(/[\/\\_\-.]+/);
  const out = new Set();
  for (const p of parts) {
    for (const w of p.split(/(?<=[a-z0-9])(?=[A-Z])|(?<=[A-Z])(?=[A-Z][a-z])/)) {
      const k = w.toLowerCase().replace(/s$/, "");
      if (k.length >= 4 && !STOP.has(k) && !STOP.has(k + "s")) out.add(k);
    }
  }
  return [...out];
}

function score(line, words) {
  const l = line.toLowerCase();
  let s = 0;
  for (const w of words) if (l.includes(w)) s += 1;
  return s;
}

function titlesFor(domain, rel) {
  let txt;
  try {
    txt = fs.readFileSync(path.join(INDEX_DIR, domain + ".md"), "utf8");
  } catch (e) {
    try { txt = fs.readFileSync(INDEX, "utf8"); } catch (e2) { return null; }
  }
  const start = txt.indexOf("## " + domain);
  if (start < 0) return null;
  let end = txt.indexOf("\n## ", start + 1);
  if (end < 0) end = txt.length;
  const lines = txt.slice(start, end).split("\n").filter((l) => l.startsWith("- "));
  const words = relevanceWords(rel || "");
  const scored = lines.map((l, i) => ({ l, i, s: score(l, words) }));
  const relevant = scored.filter((x) => x.s > 0).sort((a, b) => b.s - a.s || a.i - b.i);
  const ordered = relevant.length ? relevant.map((x) => x.l) : lines;
  let out = [];
  let used = 0;
  for (const l of ordered) {
    if (used + l.length > MAX_CHARS) break;
    out.push(l);
    used += l.length + 1;
  }
  const rest = lines.length - out.length;
  if (rest > 0) out.push(`- … et ${rest} autres, dans le fichier.`);
  return { count: lines.length, body: out.join("\n") };
}

let raw = "";
process.stdin.setEncoding("utf8");
process.stdin.on("data", (c) => (raw += c));
process.stdin.on("end", () => {
  let ev = {};
  try { ev = JSON.parse(raw.replace(/^\uFEFF/, "") || "{}"); } catch (e) { process.exit(0); }
  const file = targetPath(ev.tool_input);
  if (!file) process.exit(0);
  const rel = file.replace(/\\/g, "/");
  const hit = ROUTES.find(([, re]) => re.test(rel));
  if (!hit) process.exit(0);
  const [domain] = hit;
  const idx = titlesFor(domain, rel);
  if (!idx || !idx.count) process.exit(0);

  const context = alreadyShown(ev, domain)
    ? `Pièges « ${domain} » déjà rappelés à cet agent : relis \`.claude/skills/pitfalls/references/${domain}.md\` si ta modification de ${rel} y touche.`
    : `Pièges connus du domaine « ${domain} » (avant d'éditer ${rel}) :\n` +
      idx.body +
      `\nDétail complet : \`.claude/skills/pitfalls/references/${domain}.md\`.`;

  process.stdout.write(JSON.stringify({
    hookSpecificOutput: { hookEventName: "PreToolUse", additionalContext: context }
  }));
});
