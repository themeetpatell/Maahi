// Auto-capture every chat turn into the memory layer.
// 1. Episodes: each user message + assistant reply is inserted + embedded.
// 2. Extraction: an LLM judge runs async to pull out new facts and people.

import { getDb } from "../db.js";
import { embed, toBlob, isEmbeddingsEnabled } from "./embed.js";
import { writeFact, writePerson } from "./retrieve.js";
import { isClaudeEnabled, extractWithHaiku } from "../claude.js";

const MAX_EPISODE_CHARS = 8000;

/**
 * Capture a complete user/assistant exchange. Fire-and-forget from chat endpoints.
 * @param {{userMessage:string, assistantReply?:string, conversationId?:string|null, mode?:string|null}} args
 */
export async function captureTurn({ userMessage, assistantReply, conversationId = null, mode = null }) {
  if (userMessage)    await captureEpisode("user",      userMessage,    conversationId, mode);
  if (assistantReply) await captureEpisode("assistant", assistantReply, conversationId, mode);

  // Async extraction — never block the caller.
  setImmediate(() => {
    runExtraction({ userMessage, assistantReply, mode }).catch(e => {
      console.warn("[memory] extraction failed:", e.message);
    });
  });
}

async function captureEpisode(role, content, conversationId, mode) {
  if (!content || typeof content !== "string") return;
  const db = getDb();
  const trimmed = content.slice(0, MAX_EPISODE_CHARS);
  const info = db.prepare(`
    INSERT INTO episodes (conversation_id, role, content, mode, created_at)
    VALUES (?, ?, ?, ?, ?)
  `).run(conversationId, role, trimmed, mode, new Date().toISOString());
  const id = Number(info.lastInsertRowid);
  if (!Number.isInteger(id) || !isEmbeddingsEnabled()) return;
  try {
    const vec = await embed(trimmed, "document");
    db.prepare("INSERT INTO vec_episodes(rowid, embedding) VALUES (?, ?)")
      .run(BigInt(id), toBlob(vec));
  } catch (e) {
    console.warn("[memory] episode embed failed:", e.message);
  }
}

// ─── LLM judge: extract new facts + people from the recent exchange ──
async function runExtraction({ userMessage, assistantReply, mode }) {
  const userText  = String(userMessage   || "").slice(0, 4000);
  const replyText = String(assistantReply || "").slice(0, 4000);
  if (userText.length < 8 && replyText.length < 8) return;

  let parsed = null;
  // Prefer Claude Haiku 4.5 (structured output via JSON Schema).
  if (isClaudeEnabled()) {
    parsed = await extractWithHaiku({ userMessage: userText, assistantReply: replyText });
  }
  // Fall back to Gemini if Claude is offline or returned null.
  if (!parsed) {
    parsed = await runGeminiExtraction({ userText, replyText });
  }
  if (!parsed) return;

  const facts  = Array.isArray(parsed.facts)  ? parsed.facts  : [];
  const people = Array.isArray(parsed.people) ? parsed.people : [];

  let savedFacts = 0, savedPeople = 0;

  for (const f of facts) {
    if (!f?.key || !f?.value) continue;
    try {
      await writeFact({
        key:      slug(String(f.key)).slice(0, 100),
        value:    String(f.value).slice(0, 1000),
        category: String(f.category || "general"),
        source:   "extracted",
      });
      savedFacts++;
    } catch (e) { console.warn("[memory] writeFact (extraction):", e.message); }
  }

  for (const p of people) {
    if (!p?.name) continue;
    try {
      const person = await writePerson({
        name:             String(p.name).slice(0, 200),
        company:          p.company || null,
        role:             p.role || null,
        relationship:     p.relationship || null,
        notes:            p.interaction_summary || null,
        last_interaction: new Date().toISOString(),
      });
      if (person?.id && p.interaction_summary) {
        try {
          getDb().prepare(`
            INSERT INTO interactions (person_id, kind, summary, ts)
            VALUES (?, 'mention', ?, ?)
          `).run(person.id, String(p.interaction_summary).slice(0, 1000), new Date().toISOString());
        } catch { /* interactions optional */ }
      }
      savedPeople++;
    } catch (e) { console.warn("[memory] writePerson (extraction):", e.message); }
  }

  if (savedFacts || savedPeople) {
    console.log(`[memory] extracted: ${savedFacts} fact(s), ${savedPeople} person/people`);
  }
}

function slug(s) {
  return String(s).trim().toLowerCase()
    .replace(/[^a-z0-9]+/g, "_")
    .replace(/^_+|_+$/g, "");
}

// ─── Gemini fallback extraction ────────────────────────────────
async function runGeminiExtraction({ userText, replyText }) {
  const GEMINI_KEY   = process.env.GEMINI_API_KEY;
  const GEMINI_MODEL = process.env.GEMINI_MODEL || "gemini-2.5-flash";
  if (!GEMINI_KEY) return null;

  const prompt = `You are a memory-extraction subroutine for Meet Patel's personal AI "Maahi".
From the exchange below, extract ONLY information that is new, durable, and worth remembering long-term.

STRICT RULES:
- Skip greetings, small-talk, status checks, anything trivially obvious.
- Skip facts already implied by Meet's static profile (he's in Dubai, runs Finanshels, building Soulmap).
- A "fact" must be a specific, durable statement: preferences, decisions, goals, dates, names, numbers, relationships.
- A "person" is anyone Meet mentioned by name OR referred to specifically. Skip vague references.
- Be conservative.

Return ONLY this JSON (no markdown):
{"facts":[{"key":"snake_case","value":"...","category":"personal|business|preferences|goals|people|decisions|general"}],"people":[{"name":"...","company":"string-or-null","role":"string-or-null","relationship":"string-or-null","interaction_summary":"..."}]}

EXCHANGE:
USER: ${userText}

A: ${replyText || "(no reply)"}

If nothing durable, return {"facts":[],"people":[]}.`;

  try {
    const r = await fetch(
      `https://generativelanguage.googleapis.com/v1beta/models/${GEMINI_MODEL}:generateContent?key=${GEMINI_KEY}`,
      {
        method: "POST",
        headers: { "content-type": "application/json" },
        body: JSON.stringify({
          contents: [{ role: "user", parts: [{ text: prompt }] }],
          generationConfig: {
            responseMimeType: "application/json",
            temperature: 0.2,
            maxOutputTokens: 1024,
          },
        }),
      },
    );
    const data = await r.json();
    if (data.error) {
      console.warn("[memory] Gemini extraction error:", data.error.message);
      return null;
    }
    const text = data.candidates?.[0]?.content?.parts?.[0]?.text || "";
    return JSON.parse(text);
  } catch (e) {
    console.warn("[memory] Gemini extraction failed:", e.message);
    return null;
  }
}
