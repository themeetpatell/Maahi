// Gmail watcher — every 15 min, scan unread inbox + judge importance + DM to Telegram.

import { google } from "googleapis";
import { getDb } from "../db.js";
import { judgeWithHaiku, isClaudeEnabled } from "../claude.js";
import { notifyTelegram } from "../telegram.js";

const SCAN_QUERY = "is:unread newer_than:1d -category:promotions -category:social";
const SCAN_LIMIT = 15;
const SURFACE_LIMIT_PER_RUN = 3;

const IMPORTANCE_SCHEMA = {
  type: "object",
  additionalProperties: false,
  required: ["importance", "reason", "category", "suggested_action"],
  properties: {
    importance:       { type: "string", enum: ["high", "medium", "low", "skip"] },
    reason:           { type: "string" },
    category:         { type: "string", enum: ["investor","client","team","personal","newsletter","automated","other"] },
    suggested_action: { anyOf: [{ type: "string" }, { type: "null" }] },
  },
};

const IMPORTANCE_SYSTEM = `You are a triage subroutine for Meet Patel's personal AI Maahi.
Decide whether an unread email is worth interrupting Meet on Telegram right now.

GUIDELINES:
- "high" = investor/client/co-founder/team-lead with a real ask or decision, time-sensitive personal matter, or hot prospect.
- "medium" = useful but can wait until his next desk session.
- "low" = newsletter, FYI, low-signal notification.
- "skip" = automated, marketing, calendar invite already on his calendar, anything Meet wouldn't want pinged about.
Be conservative — most emails are "low" or "skip". Only "high" gets surfaced to his phone.`;

export async function scanInbox({ auth }) {
  if (!auth) return { skipped: "no Google auth" };
  if (!isClaudeEnabled()) return { skipped: "no Claude (judge needs Haiku)" };

  const db = getDb();
  const state = db.prepare("SELECT last_message_id FROM gmail_state WHERE id = 1").get();
  const lastSeen = state?.last_message_id || null;

  const gmail = google.gmail({ version: "v1", auth });

  let list;
  try {
    list = await gmail.users.messages.list({
      userId:     "me",
      q:          SCAN_QUERY,
      maxResults: SCAN_LIMIT,
    });
  } catch (e) {
    return { error: `Gmail list: ${e.message}` };
  }

  const msgs = list.data.messages || [];
  if (!msgs.length) {
    upsertState(db, lastSeen);
    return { scanned: 0 };
  }

  // Stop when we hit the message we already saw.
  const newMsgs = [];
  for (const m of msgs) {
    if (m.id === lastSeen) break;
    newMsgs.push(m);
  }
  if (!newMsgs.length) {
    upsertState(db, msgs[0].id);
    return { scanned: 0 };
  }

  let surfaced = 0;
  for (const m of newMsgs.slice(0, 8)) {
    if (surfaced >= SURFACE_LIMIT_PER_RUN) break;
    let detail;
    try {
      detail = await gmail.users.messages.get({
        userId:          "me",
        id:              m.id,
        format:          "metadata",
        metadataHeaders: ["From", "Subject", "Date"],
      });
    } catch (e) { continue; }

    const headers = detail.data.payload?.headers || [];
    const get = (k) => headers.find(h => h.name.toLowerCase() === k.toLowerCase())?.value || "";
    const from    = get("From");
    const subject = get("Subject");
    const snippet = detail.data.snippet || "";

    const judgment = await judgeWithHaiku({
      system: IMPORTANCE_SYSTEM,
      prompt: `From: ${from}\nSubject: ${subject}\nSnippet: ${snippet}\n\nJudge importance.`,
      schema: IMPORTANCE_SCHEMA,
      maxTokens: 400,
    });

    if (judgment?.importance === "high") {
      surfaced++;
      const action = judgment.suggested_action ? `\n\n→ ${judgment.suggested_action}` : "";
      const note = `📧 *Important email* [${judgment.category}]\n\n*From:* ${escapeMd(from)}\n*Subject:* ${escapeMd(subject)}\n_${escapeMd(snippet.slice(0,260))}_\n\n${escapeMd(judgment.reason)}${action}`;
      notifyTelegram(note).catch(() => {});
    }
  }

  upsertState(db, msgs[0].id);
  return { scanned: newMsgs.length, surfaced };
}

function upsertState(db, latestMessageId) {
  const now = new Date().toISOString();
  db.prepare(`
    INSERT INTO gmail_state (id, last_message_id, last_scan_at)
    VALUES (1, ?, ?)
    ON CONFLICT(id) DO UPDATE SET
      last_message_id = COALESCE(excluded.last_message_id, gmail_state.last_message_id),
      last_scan_at    = excluded.last_scan_at
  `).run(latestMessageId, now);
}

function escapeMd(s) {
  return String(s ?? "")
    .replace(/\*/g, "")
    .replace(/_/g, "")
    .replace(/`/g, "")
    .replace(/\[/g, "(")
    .replace(/\]/g, ")");
}
