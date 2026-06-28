// Calendar watcher — every 10 min, find events starting in 25–35 min window,
// generate a prep brief (using memory + recent context), DM to Telegram once per event.

import { google } from "googleapis";
import { getDb } from "../db.js";
import { isClaudeEnabled, sonnetText } from "../claude.js";
import { recall, formatForPrompt } from "../memory/retrieve.js";
import { notifyTelegram } from "../telegram.js";

const PREP_WINDOW_MIN_LOW  = 25;
const PREP_WINDOW_MIN_HIGH = 35;

const PREP_SYSTEM = `You are Maahi, Meet Patel's personal AI, writing a punchy pre-meeting prep brief.

Tone: warm, direct, founder-mode. Less than 120 words.
Structure:
- Who's in the meeting (one line if known)
- What this is likely about (use the description, title, and Meet's memory)
- 1–2 things Meet should remember or open with
Skip generic advice. If nothing notable to add, keep it shorter.`;

export async function scanCalendar({ auth }) {
  if (!auth) return { skipped: "no Google auth" };

  const db   = getDb();
  const cal  = google.calendar({ version: "v3", auth });
  const now  = new Date();
  const end  = new Date(now.getTime() + 60 * 60 * 1000);

  let res;
  try {
    res = await cal.events.list({
      calendarId:   "primary",
      timeMin:      now.toISOString(),
      timeMax:      end.toISOString(),
      singleEvents: true,
      orderBy:      "startTime",
      maxResults:   25,
    });
  } catch (e) {
    return { error: `Calendar list: ${e.message}` };
  }

  const events = res.data.items || [];
  let prepped = 0;

  for (const e of events) {
    if (!e.start?.dateTime) continue;
    const start = new Date(e.start.dateTime);
    const minutesUntil = (start.getTime() - now.getTime()) / 60000;
    if (minutesUntil < PREP_WINDOW_MIN_LOW || minutesUntil > PREP_WINDOW_MIN_HIGH) continue;

    const seen = db.prepare("SELECT 1 FROM calendar_prep_log WHERE event_id = ?").get(e.id);
    if (seen) continue;

    const brief = await buildPrep(e);
    if (brief) {
      const startLocal = start.toLocaleString("en-US", {
        timeZone: "Asia/Dubai", weekday:"short", month:"short",
        day:"numeric", hour:"2-digit", minute:"2-digit", hour12: true,
      });
      const message = `🗓️ *Coming up at ${startLocal}*\n*${escapeMd(e.summary || "(no title)")}*${e.location ? `\n📍 ${escapeMd(e.location)}` : ""}\n\n${brief}`;
      notifyTelegram(message).catch(() => {});
    }
    db.prepare(`
      INSERT INTO calendar_prep_log (event_id, title, sent_at)
      VALUES (?, ?, ?)
      ON CONFLICT(event_id) DO NOTHING
    `).run(e.id, e.summary || null, new Date().toISOString());
    prepped++;
  }

  return { upcoming: events.length, prepped };
}

async function buildPrep(event) {
  if (!isClaudeEnabled()) return null;

  const attendees = (event.attendees || [])
    .filter(a => !a.self && a.email)
    .map(a => a.email)
    .slice(0, 8);
  const description = (event.description || "").slice(0, 1500);

  const queryText = [event.summary || "", attendees.join(" "), description.slice(0, 300)]
    .filter(Boolean).join(" — ").slice(0, 500);
  let memCtx = "";
  try {
    const results = await recall(queryText, { limit: 8 });
    memCtx = formatForPrompt(results);
  } catch { /* ignore */ }

  const prompt = `Upcoming meeting:
Title: ${event.summary || "(no title)"}
Start: ${event.start?.dateTime || event.start?.date}
Location: ${event.location || "(none)"}
Attendees: ${attendees.length ? attendees.join(", ") : "(none listed)"}
Description: ${description || "(none)"}

${memCtx ? `Maahi's memory:\n${memCtx}\n` : ""}
Write the prep brief for Meet.`;

  return await sonnetText({ system: PREP_SYSTEM, prompt, maxTokens: 600 });
}

function escapeMd(s) {
  return String(s ?? "")
    .replace(/\*/g, "")
    .replace(/_/g, "")
    .replace(/`/g, "")
    .replace(/\[/g, "(")
    .replace(/\]/g, ")");
}
