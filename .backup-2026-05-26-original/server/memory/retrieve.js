// Hybrid retrieval: vector KNN (sqlite-vec) + BM25 (FTS5) + recency.
// Returns a unified list across facts / episodes / people / documents.

import { getDb } from "../db.js";
import { embed, toBlob, isEmbeddingsEnabled } from "./embed.js";

const RRF_K = 60;

/**
 * @param {string} query
 * @param {{
 *   kinds?: Array<"facts"|"episodes"|"people"|"documents">,
 *   limit?: number,
 *   perKindLimit?: number,
 * }} [opts]
 */
export async function recall(query, opts = {}) {
  const q = String(query || "").trim();
  if (!q) return [];
  if (!isEmbeddingsEnabled()) return recallKeywordOnly(q, opts);

  const {
    kinds = ["facts", "episodes", "people", "documents"],
    limit = 8,
    perKindLimit = 6,
  } = opts;

  const qBlob = toBlob(await embed(q, "query"));
  const db = getDb();
  const out = [];

  if (kinds.includes("facts")) {
    for (const r of hybrid(db, {
      table: "facts", vec: "vec_facts", fts: "fts_facts",
      select: "id, key, value, category, created_at",
      query: q, qBlob, limit: perKindLimit,
    })) out.push({ kind: "fact", ...r });
  }
  if (kinds.includes("episodes")) {
    for (const r of hybrid(db, {
      table: "episodes", vec: "vec_episodes", fts: "fts_episodes",
      select: "id, conversation_id, role, content, mode, created_at",
      query: q, qBlob, limit: perKindLimit, halfLifeDays: 30,
    })) out.push({ kind: "episode", ...r });
  }
  if (kinds.includes("people")) {
    for (const r of hybrid(db, {
      table: "people", vec: "vec_people", fts: "fts_people",
      select: "id, name, company, role, relationship, email, phone, notes, last_interaction",
      query: q, qBlob, limit: perKindLimit,
    })) out.push({ kind: "person", ...r });
  }
  if (kinds.includes("documents")) {
    for (const r of hybrid(db, {
      table: "documents", vec: "vec_documents", fts: "fts_documents",
      select: "id, kind AS doc_kind, source_id, title, substr(content, 1, 600) AS content_preview, created_at",
      query: q, qBlob, limit: perKindLimit, halfLifeDays: 180,
    })) out.push({ kind: "document", ...r });
  }

  out.sort((a, b) => b._score - a._score);
  return out.slice(0, limit);
}

function hybrid(db, { table, vec, fts, select, query, qBlob, limit, halfLifeDays }) {
  let vecRows = [];
  try {
    vecRows = db.prepare(
      `SELECT rowid AS id, distance FROM ${vec} WHERE embedding MATCH ? ORDER BY distance LIMIT ?`
    ).all(qBlob, limit);
  } catch { vecRows = []; }

  let ftsRows = [];
  try {
    const ftsQuery = sanitizeFts(query);
    if (ftsQuery) {
      ftsRows = db.prepare(
        `SELECT rowid AS id, rank FROM ${fts} WHERE ${fts} MATCH ? ORDER BY rank LIMIT ?`
      ).all(ftsQuery, limit);
    }
  } catch { ftsRows = []; }

  const score = new Map();
  vecRows.forEach((r, i) => score.set(r.id, (score.get(r.id) || 0) + 1 / (RRF_K + i + 1)));
  ftsRows.forEach((r, i) => score.set(r.id, (score.get(r.id) || 0) + 1 / (RRF_K + i + 1)));
  if (!score.size) return [];

  const ids = [...score.keys()];
  const rows = db.prepare(
    `SELECT ${select} FROM ${table} WHERE id IN (${ids.map(() => "?").join(",")})`
  ).all(...ids);

  const enriched = rows.map(r => {
    let s = score.get(r.id) || 0;
    if (halfLifeDays && r.created_at) {
      const ageDays = (Date.now() - new Date(r.created_at).getTime()) / 86400000;
      const decay = Math.exp(-Math.LN2 * ageDays / halfLifeDays);
      s *= 0.5 + 0.5 * decay;
    }
    return { ...r, _score: s };
  });
  enriched.sort((a, b) => b._score - a._score);
  return enriched.slice(0, limit);
}

function sanitizeFts(q) {
  const tokens = q
    .toLowerCase()
    .replace(/[^\p{L}\p{N}\s]/gu, " ")
    .split(/\s+/)
    .filter(t => t.length > 1);
  if (!tokens.length) return "";
  return tokens.map(t => `"${t}"`).join(" OR ");
}

function recallKeywordOnly(query, opts = {}) {
  const { limit = 8 } = opts;
  const db = getDb();
  const out = [];
  const ftsQuery = sanitizeFts(query);
  if (!ftsQuery) return [];
  const targets = [
    { table: "facts",     fts: "fts_facts",     select: "id, key, value, category, created_at",                                                          kindLabel: "fact" },
    { table: "people",    fts: "fts_people",    select: "id, name, company, role, relationship, email, phone, notes, last_interaction",                  kindLabel: "person" },
    { table: "episodes",  fts: "fts_episodes",  select: "id, conversation_id, role, content, mode, created_at",                                          kindLabel: "episode" },
    { table: "documents", fts: "fts_documents", select: "id, kind AS doc_kind, source_id, title, substr(content,1,600) AS content_preview, created_at",  kindLabel: "document" },
  ];
  for (const { table, fts, select, kindLabel } of targets) {
    try {
      const ids = db.prepare(`SELECT rowid AS id, rank FROM ${fts} WHERE ${fts} MATCH ? ORDER BY rank LIMIT ?`).all(ftsQuery, limit);
      if (!ids.length) continue;
      const rows = db.prepare(`SELECT ${select} FROM ${table} WHERE id IN (${ids.map(()=>"?").join(",")})`).all(...ids.map(r=>r.id));
      rows.forEach((r, i) => out.push({ kind: kindLabel, _score: 1 / (i + 1), ...r }));
    } catch { /* ignore */ }
  }
  out.sort((a, b) => b._score - a._score);
  return out.slice(0, limit);
}

export function formatForPrompt(results) {
  if (!results || !results.length) return "";
  const buckets = { fact: [], person: [], episode: [], document: [] };
  for (const r of results) (buckets[r.kind] || (buckets[r.kind] = [])).push(r);

  const sections = [];
  if (buckets.fact?.length) {
    sections.push("### Known facts\n" + buckets.fact.map(r => `- ${r.key}: ${r.value}`).join("\n"));
  }
  if (buckets.person?.length) {
    sections.push("### People in context\n" + buckets.person.map(r =>
      `- **${r.name}**${r.company?` @ ${r.company}`:""}${r.role?` (${r.role})`:""}${r.relationship?` — ${r.relationship}`:""}${r.notes?`. ${truncate(r.notes,160)}`:""}`
    ).join("\n"));
  }
  if (buckets.episode?.length) {
    sections.push("### Past conversation snippets\n" + buckets.episode.map(r => {
      const when = r.created_at ? new Date(r.created_at).toLocaleDateString("en-US", { month:"short", day:"numeric", year:"numeric" }) : "earlier";
      return `- [${when}] ${r.role}: "${truncate(r.content, 240)}"`;
    }).join("\n"));
  }
  if (buckets.document?.length) {
    sections.push("### Related documents\n" + buckets.document.map(r =>
      `- [${r.doc_kind}] ${r.title || "(untitled)"} — ${truncate(r.content_preview || "", 200)}`
    ).join("\n"));
  }
  return sections.join("\n\n");
}

function truncate(s, n) {
  s = String(s ?? "");
  return s.length > n ? s.slice(0, n) + "…" : s;
}

export async function writeFact({ key, value, category = "general", source = "manual" }) {
  const db = getDb();
  const now = new Date().toISOString();
  db.prepare(`
    INSERT INTO facts (key, value, category, source, created_at, updated_at)
    VALUES (?, ?, ?, ?, ?, ?)
    ON CONFLICT(key) DO UPDATE SET
      value=excluded.value, category=excluded.category, updated_at=excluded.updated_at
  `).run(key, String(value), category, source, now, now);
  const id = Number(db.prepare("SELECT id FROM facts WHERE key = ?").get(key)?.id);
  if (Number.isInteger(id) && isEmbeddingsEnabled()) {
    try {
      const vec = await embed(`${key}: ${value}`, "document");
      const bid = BigInt(id);
      db.prepare("DELETE FROM vec_facts WHERE rowid = ?").run(bid);
      db.prepare("INSERT INTO vec_facts(rowid, embedding) VALUES (?, ?)").run(bid, toBlob(vec));
    } catch { /* embedding failed — fact still stored, will retry on next backfill */ }
  }
  return { id, key, value, category };
}

export async function writePerson(person) {
  const db = getDb();
  const now = new Date().toISOString();
  db.prepare(`
    INSERT INTO people (name, company, role, relationship, email, phone, notes, last_interaction, created_at, updated_at)
    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    ON CONFLICT(name) DO UPDATE SET
      company=COALESCE(excluded.company, people.company),
      role=COALESCE(excluded.role, people.role),
      relationship=COALESCE(excluded.relationship, people.relationship),
      email=COALESCE(excluded.email, people.email),
      phone=COALESCE(excluded.phone, people.phone),
      notes=COALESCE(excluded.notes, people.notes),
      last_interaction=COALESCE(excluded.last_interaction, people.last_interaction),
      updated_at=excluded.updated_at
  `).run(
    person.name, person.company || null, person.role || null, person.relationship || null,
    person.email || null, person.phone || null, person.notes || null, person.last_interaction || null,
    now, now,
  );
  const id = Number(db.prepare("SELECT id FROM people WHERE name = ?").get(person.name)?.id);
  if (Number.isInteger(id) && isEmbeddingsEnabled()) {
    try {
      const text = [person.name, person.company, person.role, person.relationship, person.notes].filter(Boolean).join(". ");
      const vec = await embed(text || person.name, "document");
      const bid = BigInt(id);
      db.prepare("DELETE FROM vec_people WHERE rowid = ?").run(bid);
      db.prepare("INSERT INTO vec_people(rowid, embedding) VALUES (?, ?)").run(bid, toBlob(vec));
    } catch { /* skip on failure */ }
  }
  return { id, ...person };
}

export function getPersonByName(name) {
  const db = getDb();
  return db.prepare("SELECT * FROM people WHERE lower(name) = lower(?) LIMIT 1").get(name) || null;
}

export function listAllPeople() {
  return getDb().prepare("SELECT id, name, company, role, relationship, email, phone, notes, last_interaction FROM people ORDER BY updated_at DESC").all();
}

export function deletePersonByName(name) {
  const db = getDb();
  const row = db.prepare("SELECT id FROM people WHERE lower(name) = lower(?)").get(name);
  if (!row) return false;
  db.prepare("DELETE FROM vec_people WHERE rowid = ?").run(BigInt(row.id));
  db.prepare("DELETE FROM people WHERE id = ?").run(row.id);
  return true;
}
