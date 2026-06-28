// One-time migration: legacy JSON stores -> SQLite + vector embeddings.
// Safe to call on every boot; isBackfilled() makes it idempotent.

import path from "node:path";
import fs from "node:fs/promises";
import { fileURLToPath } from "node:url";
import { getDb } from "../db.js";
import { embed, toBlob, isEmbeddingsEnabled } from "./embed.js";

const __filename = fileURLToPath(import.meta.url);
const __dirname  = path.dirname(__filename);
const DATA = path.join(__dirname, "..", "..", "data");

async function readJsonSafe(name, fallback) {
  try { return JSON.parse(await fs.readFile(path.join(DATA, name), "utf8")); }
  catch { return fallback; }
}

export function isBackfilled() {
  const db = getDb();
  const a = db.prepare("SELECT COUNT(*) AS n FROM facts").get().n;
  const b = db.prepare("SELECT COUNT(*) AS n FROM people").get().n;
  const c = db.prepare("SELECT COUNT(*) AS n FROM documents").get().n;
  const d = db.prepare("SELECT COUNT(*) AS n FROM episodes").get().n;
  return (a + b + c + d) > 0;
}

export async function backfillAll({ force = false } = {}) {
  if (!isEmbeddingsEnabled()) {
    return { skipped: true, reason: "VOYAGE_API_KEY not set" };
  }
  if (!force && isBackfilled()) {
    return { skipped: true, reason: "already backfilled" };
  }

  const stats = { facts: 0, people: 0, documents: 0, episodes: 0 };
  stats.facts     = await backfillFacts();
  stats.people    = await backfillPeople();
  stats.documents = await backfillNotes();
  stats.episodes  = await backfillEpisodes();
  return { skipped: false, stats };
}

async function backfillFacts() {
  const db = getDb();
  const mem = await readJsonSafe("memories.json", { facts: {} });
  const entries = Object.entries(mem.facts || {});
  if (!entries.length) return 0;

  const rows = entries.map(([key, v]) => {
    const value     = typeof v === "object" ? v.value : v;
    const category  = (typeof v === "object" && v.category) || "general";
    const updatedAt = (typeof v === "object" && v.updatedAt) || new Date().toISOString();
    return { key, value: String(value), category, updatedAt };
  });

  const vectors = await embed(rows.map(r => `${r.key}: ${r.value}`), "document");

  const insertFact = db.prepare(`
    INSERT INTO facts (key, value, category, source, created_at, updated_at)
    VALUES (?, ?, ?, 'imported', ?, ?)
    ON CONFLICT(key) DO UPDATE SET
      value=excluded.value, category=excluded.category, updated_at=excluded.updated_at
  `);
  const lookupFact = db.prepare("SELECT id FROM facts WHERE key = ?");
  const deleteVec  = db.prepare("DELETE FROM vec_facts WHERE rowid = ?");
  const insertVec  = db.prepare("INSERT INTO vec_facts(rowid, embedding) VALUES (?, ?)");

  const tx = db.transaction(() => {
    rows.forEach((r, i) => {
      insertFact.run(r.key, r.value, r.category, r.updatedAt, r.updatedAt);
      const id = Number(lookupFact.get(r.key)?.id);
      if (!Number.isInteger(id)) return;
      const bid = BigInt(id);
      deleteVec.run(bid);
      insertVec.run(bid, toBlob(vectors[i]));
    });
  });
  tx();
  return rows.length;
}

async function backfillPeople() {
  const db = getDb();
  const data = await readJsonSafe("contacts.json", { contacts: [] });
  const contacts = data.contacts || [];
  if (!contacts.length) return 0;

  const rows = contacts.map(c => ({
    name:             c.name,
    company:          c.company || null,
    role:             c.role || null,
    relationship:     c.relationship || null,
    email:            c.email || null,
    phone:            c.phone || null,
    notes:            c.notes || null,
    last_interaction: c.last_interaction || null,
    created_at:       c.createdAt || new Date().toISOString(),
  }));

  const vectors = await embed(rows.map(personText), "document");

  const insertPerson = db.prepare(`
    INSERT INTO people (name, company, role, relationship, email, phone, notes, last_interaction, created_at, updated_at)
    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    ON CONFLICT(name) DO UPDATE SET
      company=excluded.company, role=excluded.role, relationship=excluded.relationship,
      email=excluded.email, phone=excluded.phone, notes=excluded.notes,
      last_interaction=excluded.last_interaction, updated_at=excluded.updated_at
  `);
  const lookupPerson = db.prepare("SELECT id FROM people WHERE name = ?");
  const deleteVec    = db.prepare("DELETE FROM vec_people WHERE rowid = ?");
  const insertVec    = db.prepare("INSERT INTO vec_people(rowid, embedding) VALUES (?, ?)");

  const tx = db.transaction(() => {
    rows.forEach((r, i) => {
      const now = new Date().toISOString();
      insertPerson.run(
        r.name, r.company, r.role, r.relationship,
        r.email, r.phone, r.notes, r.last_interaction,
        r.created_at, now,
      );
      const id = Number(lookupPerson.get(r.name)?.id);
      if (!Number.isInteger(id)) return;
      const bid = BigInt(id);
      deleteVec.run(bid);
      insertVec.run(bid, toBlob(vectors[i]));
    });
  });
  tx();
  return rows.length;
}

async function backfillNotes() {
  const db = getDb();
  const data = await readJsonSafe("notes.json", { notes: [] });
  const notes = data.notes || [];
  if (!notes.length) return 0;

  const rows = notes.map(n => ({
    source_id:  String(n.id ?? ""),
    title:      n.title || null,
    content:    String(n.content || ""),
    tags:       JSON.stringify(n.tags || []),
    created_at: n.createdAt || new Date().toISOString(),
  }));

  const vectors = await embed(rows.map(r => `${r.title || ""}\n${r.content}`), "document");

  const insertDoc = db.prepare(`
    INSERT INTO documents (kind, source_id, title, content, metadata, tags, created_at)
    VALUES ('note', ?, ?, ?, NULL, ?, ?)
  `);
  const insertVec = db.prepare("INSERT INTO vec_documents(rowid, embedding) VALUES (?, ?)");

  const tx = db.transaction(() => {
    rows.forEach((r, i) => {
      const info = insertDoc.run(r.source_id, r.title, r.content, r.tags, r.created_at);
      const id = Number(info.lastInsertRowid);
      if (!Number.isInteger(id)) return;
      insertVec.run(BigInt(id), toBlob(vectors[i]));
    });
  });
  tx();
  return rows.length;
}

async function backfillEpisodes() {
  const db = getDb();
  const data = await readJsonSafe("conversations.json", { conversations: [] });
  const messages = [];
  for (const c of data.conversations || []) {
    for (const m of c.messages || []) {
      const content = typeof m.content === "string" ? m.content : "";
      if (!content) continue;
      messages.push({
        conversation_id: c.id || null,
        role:            m.role || "user",
        content,
        mode:            c.mode || null,
        created_at:      m.createdAt || c.updatedAt || new Date().toISOString(),
      });
    }
  }
  if (!messages.length) return 0;

  const vectors = await embed(messages.map(m => m.content.slice(0, 8000)), "document");

  const insertEp = db.prepare(`
    INSERT INTO episodes (conversation_id, role, content, mode, created_at)
    VALUES (?, ?, ?, ?, ?)
  `);
  const insertVec = db.prepare("INSERT INTO vec_episodes(rowid, embedding) VALUES (?, ?)");

  const tx = db.transaction(() => {
    messages.forEach((m, i) => {
      const info = insertEp.run(m.conversation_id, m.role, m.content, m.mode, m.created_at);
      const id = Number(info.lastInsertRowid);
      if (!Number.isInteger(id)) return;
      insertVec.run(BigInt(id), toBlob(vectors[i]));
    });
  });
  tx();
  return messages.length;
}

function personText(p) {
  return [
    p.name,
    p.company      && `at ${p.company}`,
    p.role         && `role ${p.role}`,
    p.relationship && `relationship ${p.relationship}`,
    p.email,
    p.phone,
    p.notes,
  ].filter(Boolean).join(". ");
}
