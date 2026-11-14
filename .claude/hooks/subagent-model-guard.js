#!/usr/bin/env node
/**
 * subagent-model-guard — hook PreToolUse (outil Agent / Task).
 *
 * Rend exécutoire la règle « model: toujours explicite » et la règle 2bis de
 * la skill model-effort-routing :
 *   - `model:` obligatoire pour TOUT sous-agent (sauf `fork`, `statusline-setup`,
 *     `claude-code-guide`) ;
 *   - `fable` interdit pour les sous-agents ;
 *   - `Explore` : `haiku` par défaut (`sonnet` seulement si « very thorough »
 *     dans le prompt, jamais `opus`).
 *
 * Coupe-circuit : SUBAGENT_MODEL_GUARD=off (à dire à Chakir, jamais en
 * silence). En cas d'entrée illisible : laisse passer.
 */
const EXEMPT = new Set(["fork", "statusline-setup", "claude-code-guide"]);
const THOROUGH = /very[\s-]*thorough|tr[eè]s approfondi|exhausti/i;

function refuse(msg) {
  process.stderr.write(`subagent-model-guard : ${msg} (skill model-effort-routing) ; écris la ligne ROUTAGE correspondante.\n`);
  process.exit(2);
}

let raw = "";
process.stdin.setEncoding("utf8");
process.stdin.on("data", (c) => { raw += c; });
process.stdin.on("end", () => {
  if ((process.env.SUBAGENT_MODEL_GUARD || "").toLowerCase() === "off") process.exit(0);
  let data;
  try { data = JSON.parse(raw.replace(/^\uFEFF/, "") || "{}"); } catch { process.exit(0); }
  const tool = data.tool_name || "";
  if (tool !== "Agent" && tool !== "Task") process.exit(0);
  const input = data.tool_input || {};
  const type = input.subagent_type || "general-purpose";
  const model = typeof input.model === "string" ? input.model.trim().toLowerCase() : "";
  const prompt = `${input.description || ""}\n${input.prompt || ""}`;

  if (EXEMPT.has(type)) process.exit(0);
  if (model.includes("fable")) {
    refuse(`${type} en \`fable\` refusé — Fable est hors routage pour les sous-agents. Choisis haiku, sonnet ou opus`);
  }
  if (!model) {
    const hint = type === "Explore"
      ? 'model: "haiku" (ou "sonnet" pour un balayage very thorough sur plusieurs modules)'
      : 'model: "sonnet" en général ; "opus" seulement si la grille le donne (agent qui écrit : C2 = 2 ou C3 = 2 ; lecteur : sécurité ou synthèse)';
    refuse(`appel à ${type} sans \`model:\` refusé — il prendrait le modèle de sa fiche ou de la session sans passer par la grille. Relance avec ${hint}`);
  }
  if (type === "Explore") {
    if (model.includes("opus")) refuse('Explore en `opus` refusé — une recherche se fait en "haiku" ; un diagnostic se confie à l\'agent de diagnostic');
    if (model.includes("sonnet") && !THOROUGH.test(prompt)) {
      refuse('Explore en `sonnet` refusé pour une recherche quick/medium — relance avec model: "haiku", ou précise « very thorough » dans le prompt si le balayage couvre plusieurs modules avec synthèse');
    }
  }
  process.exit(0);
});
