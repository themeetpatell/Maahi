// Belief register — once a day, Maahi forms opinions about what Meet should
// know or do today. Beliefs are surfaced in the morning briefing and
// dismissed/resolved over time.

import path from "node:path";
import fs from "node:fs/promises";
import { fileURLToPath } from "node:url";
import { getDb } from "../db.js";
import { isClaudeEnabled, sonnetJSON } from "../claude.js";

const __filename = fileURLToPath(import.meta.url);
const __dirname  = path.dirname(__filename);
const DATA       = path.join(__dirname, "..", "..", "data");
const TASKS_PATH = path.join(DATA, "tasks.json");

const BELIEFS_SCHEMA = {
  type: "object",
  additionalProperties: false,
  required: ["beliefs"],
  properties: {
    beliefs: {
      type: "array",
      items: {
        type: "object",
        additionalProperties: false,
        required: ["kind", "content", "rationale", "confidence"],
        properties: {
          kind:       { type: "string", enum: ["nudge", "concern", "opportunity", "observation"] },
          content:    { type: "string" },
          rationale:  { type: "string" },
          confidence: { type: "number" },
        },
      },
    },
  },
};

const BELIEF_SYSTEM = `You are Maahi, Meet Patel's personal AI, forming OPINIONS about what Meet should know or do today.
You see his recent conversations, open tasks, recently-extracted facts, and existing unresolved beliefs.

Generate 1–4 fresh beliefs. Each must be:
- Specific and actionable (not "you should focus more")
- Non-obvious (skip things Meet would already realize from glancing at his to-do list)
- Different from existing unresolved beliefs (don't restate)
- Honest — when in doubt, output fewer / none

Kinds:
- "nudge" → a small action Meet should take today
- "concern" → something deteriorating or at risk
- "opportunity" → momentum worth pressing on
- "observation" → a pattern Meet may not have noticed

Confidence is 0.0–1.0. Be calibrated — most beliefs should be 0.5–0.8.`;

export async function formBeliefs() {
  if (!isClaudeEnabled()) return { skipped: "no Claude" };
  const db = getDb();

  const recentEpisodes = db.prepare(`
    SELECT role, content, created_at
    FROM episodes
    WHERE created_at > datetime('now', '-24 hours')
    ORDER BY id DESC
    LIMIT 40
  `).all();

  const recentFacts = db.prepare(`
    SELECT key, value, category, created_at
    FROM facts
    WHERE created_at > datetime('now', '-72 hours')
    ORDER BY id DESC
    LIMIT 25
  `).all();

  const openTasks = await readOpenTasks();
  const existing  = getUnresolvedBeliefs(20);

  const prompt = `EXISTING UNRESOLVED BELIEFS (do not restate):
${existing.map(b => `- [${b.kind}] ${b.content}`).join("\n") || "(none)"}

OPEN TASKS:
${openTasks.map(t => `- [${t.priority || "?"}] ${t.text}${t.deadline ? ` (due ${t.deadline})` : ""}`).join("\n") || "(none)"}

RECENT FACTS (last 72h):
${recentFacts.map(f => `- ${f.key}: ${f.value} [${f.category}]`).join("\n") || "(none)"}

RECENT CONVERSATION SAMPLES (last 24h, most recent first):
${recentEpisodes.slice(0, 20).map(e => `[${e.role}] ${truncate(e.content, 220)}`).join("\n") || "(none)"}

Form 1–4 beliefs. Return JSON only.`;

  const out = await sonnetJSON({
    system:    BELIEF_SYSTEM,
    prompt,
    schema:    BELIEFS_SCHEMA,
    maxTokens: 1800,
  });
  if (!out) return { error: "sonnet returned null" };

  const beliefs = Array.isArray(out.beliefs) ? out.beliefs : [];
  if (!beliefs.length) return { formed: 0 };

  const insert = db.prepare(`
    INSERT INTO beliefs (kind, content, rationale, confidence, evidence_json)
    VALUES (?, ?, ?, ?, ?)
  `);
  const tx = db.transaction((rows) => {
    for (const b of rows) {
      insert.run(
        b.kind || "observation",
        String(b.content || "").slice(0, 1000),
        String(b.rationale || "").slice(0, 1000),
        clamp01(Number(b.confidence) || 0.5),
        JSON.stringify({ formed_at: new Date().toISOString() }),
      );
    }
  });
  tx(beliefs);

  return { formed: beliefs.length };
}

export function getUnresolvedBeliefs(limit = 10) {
  return getDb().prepare(`
    SELECT id, kind, content, rationale, confidence, created_at, surfaced_at
    FROM beliefs
    WHERE resolved_at IS NULL
    ORDER BY confidence DESC, created_at DESC
    LIMIT ?
  `).all(limit);
}

export function markBeliefsSurfaced(ids) {
  if (!ids?.length) return;
  const db = getDb();
  const now = new Date().toISOString();
  const stmt = db.prepare("UPDATE beliefs SET surfaced_at = ? WHERE id = ? AND surfaced_at IS NULL");
  const tx = db.transaction((idList) => { for (const id of idList) stmt.run(now, id); });
  tx(ids);
}

export function resolveBelief(id, resolution = "acted") {
  const db = getDb();
  const now = new Date().toISOString();
  db.prepare("UPDATE beliefs SET resolved_at = ?, resolution = ? WHERE id = ?")
    .run(now, resolution, id);
}

export function expireStaleBeliefs() {
  const db = getDb();
  const cutoff = new Date(Date.now() - 7 * 24 * 60 * 60 * 1000).toISOString();
  db.prepare(`
    UPDATE beliefs
    SET resolved_at = ?, resolution = 'stale'
    WHERE resolved_at IS NULL AND created_at < ?
  `).run(new Date().toISOString(), cutoff);
}

async function readOpenTasks() {
  try {
    const raw = JSON.parse(await fs.readFile(TASKS_PATH, "utf8"));
    return (raw.tasks || []).filter(t => !t.done).slice(-20);
  } catch { return []; }
}

function truncate(s, n) {
  s = String(s ?? "");
  return s.length > n ? s.slice(0, n) + "…" : s;
}

function clamp01(x) {
  if (!Number.isFinite(x)) return 0.5;
  return Math.max(0, Math.min(1, x));
}
