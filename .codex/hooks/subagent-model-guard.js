#!/usr/bin/env node
/**
 * subagent-model-guard — hook PreToolUse (outil Agent / Task).
 *
 * Bloque (exit 2) un appel aux agents intégrés `Explore` ou `Plan` qui ne
 * passe pas `model:`. Depuis Claude Code v2.1.198 ils héritent du modèle de la
 * session principale (Opus) : mesuré le 2026-09-25, 24 Explore en Opus sur
 * 14 jours, 1 seul appel Haiku. La règle écrite (skill model-effort-routing,
 * règle 2bis) n'a pas suffi en test : l'orchestrateur l'a lue et ne l'a pas
 * appliquée. Le hook la rend exécutoire ; Claude relance simplement l'appel
 * avec `model:`.
 *
 * Ne bloque rien d'autre. Coupe-circuit : SUBAGENT_MODEL_GUARD=off (à dire à
 * Chakir, jamais en silence). En cas d'entrée illisible : laisse passer.
 */
const GUARDED = new Set(["Explore", "Plan"]);

let raw = "";
process.stdin.setEncoding("utf8");
process.stdin.on("data", (c) => { raw += c; });
process.stdin.on("end", () => {
  if ((process.env.SUBAGENT_MODEL_GUARD || "").toLowerCase() === "off") process.exit(0);
  let data;
  try { data = JSON.parse(raw.replace(/^﻿/, "") || "{}"); } catch { process.exit(0); }
  const tool = data.tool_name || "";
  if (tool !== "Agent" && tool !== "Task") process.exit(0);
  const input = data.tool_input || {};
  const type = input.subagent_type || "";
  if (!GUARDED.has(type)) process.exit(0);
  const model = typeof input.model === "string" ? input.model.trim() : "";
  if (model) process.exit(0);
  const hint = type === "Explore"
    ? 'model: "haiku" pour une recherche quick/medium, "sonnet" si very thorough sur plusieurs modules'
    : 'model: "sonnet" en général, "opus" seulement si C2 = 2 ou C3 = 2';
  process.stderr.write(
    `subagent-model-guard : appel à ${type} sans \`model:\` refusé — il hériterait du modèle de la session (Opus). ` +
    `Relance le même appel avec ${hint} (skill model-effort-routing, règle 2bis), et écris la ligne ROUTAGE correspondante.\n`
  );
  process.exit(2);
});
