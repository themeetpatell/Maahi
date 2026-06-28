// Telegram bot — Maahi's mobile / lockscreen surface.
// Long-poll incoming messages; route through the shared chat path.
// In-memory per-chat history (resets on /reset or process restart).

import { isSTTEnabled, isTTSEnabled, transcribe, synthesize } from "./voice.js";

const TG_API = "https://api.telegram.org/bot";

function token() { return process.env.TELEGRAM_BOT_TOKEN; }

function allowedIds() {
  return (process.env.TELEGRAM_ALLOWED_IDS || "")
    .split(",").map(s => s.trim()).filter(Boolean);
}

export function isTelegramEnabled() {
  return Boolean(token());
}

// ─── State ──────────────────────────────────────────────────────
let _polling      = false;
let _lastUpdateId = 0;
let _handleChat   = null;          // set by startTelegramBot
const _history    = new Map();     // chatId -> [{role, content}]
const MAX_HISTORY    = 20;
const POLL_TIMEOUT_S = 30;

// ─── HTTP helpers ───────────────────────────────────────────────
async function tg(method, body) {
  if (!token()) throw new Error("TELEGRAM_BOT_TOKEN not set");
  const r = await fetch(`${TG_API}${token()}/${method}`, {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify(body),
  });
  return r.json();
}

async function sendMessage(chatId, text, opts = {}) {
  const safe = String(text ?? "").trim() || "(empty)";
  for (const chunk of chunkText(safe, 4000)) {
    try {
      const r = await tg("sendMessage", {
        chat_id: chatId, text: chunk, parse_mode: "Markdown",
        disable_web_page_preview: true, ...opts,
      });
      if (!r.ok && r.error_code === 400) {
        // Markdown parse failure — retry plain text
        await tg("sendMessage", {
          chat_id: chatId, text: chunk, disable_web_page_preview: true, ...opts,
        });
      }
    } catch (e) {
      console.warn("[telegram] sendMessage failed:", e.message);
    }
  }
}

async function sendChatAction(chatId, action = "typing") {
  try { await tg("sendChatAction", { chat_id: chatId, action }); } catch {}
}

async function sendAudio(chatId, audioBuffer, opts = {}) {
  if (!token()) return;
  const fd = new FormData();
  fd.append("chat_id", String(chatId));
  fd.append("audio", new Blob([audioBuffer], { type: "audio/mpeg" }), opts.filename || "maahi.mp3");
  if (opts.title)     fd.append("title", opts.title);
  if (opts.performer) fd.append("performer", opts.performer);
  try {
    await fetch(`${TG_API}${token()}/sendAudio`, { method: "POST", body: fd });
  } catch (e) {
    console.warn("[telegram] sendAudio failed:", e.message);
  }
}

async function downloadFile(fileId) {
  const info = await tg("getFile", { file_id: fileId });
  if (!info.ok) throw new Error("getFile: " + info.description);
  const path = info.result.file_path;
  const r = await fetch(`https://api.telegram.org/file/bot${token()}/${path}`);
  if (!r.ok) throw new Error(`download ${path}: ${r.status}`);
  const buf = Buffer.from(await r.arrayBuffer());
  return { buffer: buf, path, mimeType: guessMimeType(path) };
}

function guessMimeType(path) {
  const ext = String(path).split(".").pop()?.toLowerCase();
  return {
    jpg: "image/jpeg", jpeg: "image/jpeg", png: "image/png",
    gif: "image/gif", webp: "image/webp", heic: "image/heic",
    ogg: "audio/ogg", oga: "audio/ogg", mp3: "audio/mpeg",
    wav: "audio/wav", m4a: "audio/mp4",
  }[ext] || "application/octet-stream";
}

// ─── Outbound notifications (used by crons) ─────────────────────
export async function notifyTelegram(text) {
  if (!token()) return;
  const ids = allowedIds();
  if (!ids.length) return;
  for (const chatId of ids) {
    await sendMessage(chatId, text);
  }
}

// ─── Polling loop ───────────────────────────────────────────────
export async function startTelegramBot({ handleChat }) {
  if (!token()) {
    console.log("[telegram] TELEGRAM_BOT_TOKEN not set — bot disabled");
    return;
  }
  if (_polling) return;
  if (typeof handleChat !== "function") {
    console.warn("[telegram] startTelegramBot needs a handleChat function");
    return;
  }
  _handleChat = handleChat;

  const me = await tg("getMe", {});
  if (!me.ok) {
    console.warn("[telegram] getMe failed (bad token?):", me.description);
    return;
  }
  const ids = allowedIds();
  console.log(
    `[telegram] @${me.result.username} polling | allowed: ${ids.length || "NONE — set TELEGRAM_ALLOWED_IDS"}`,
  );

  _polling = true;
  (async function loop() {
    while (_polling) {
      try {
        const updates = await tg("getUpdates", {
          offset:          _lastUpdateId + 1,
          timeout:         POLL_TIMEOUT_S,
          allowed_updates: ["message"],
        });
        if (!updates.ok) {
          console.warn("[telegram] getUpdates:", updates.description);
          await sleep(5000);
          continue;
        }
        for (const update of updates.result || []) {
          _lastUpdateId = update.update_id;
          handleUpdate(update).catch(e =>
            console.warn("[telegram] handler:", e.message),
          );
        }
      } catch (e) {
        console.warn("[telegram] poll error:", e.message);
        await sleep(5000);
      }
    }
  })();
}

export function stopTelegramBot() { _polling = false; }

// ─── Update dispatch ────────────────────────────────────────────
async function handleUpdate(update) {
  const msg = update.message;
  if (!msg || !msg.chat?.id) return;
  const chatId  = msg.chat.id;
  const userId  = msg.from?.id;
  const userIds = allowedIds();

  if (!userIds.length || !userIds.includes(String(userId))) {
    await sendMessage(
      chatId,
      `🔒 Not authorized.\nYour Telegram user ID: \`${userId}\`\n\nAdd it to *TELEGRAM_ALLOWED_IDS* in .env (comma-separated) and restart Maahi to allow yourself.`,
    );
    return;
  }

  if (typeof msg.text === "string" && msg.text.startsWith("/")) {
    return handleCommand(msg, chatId);
  }

  let userText = msg.text || msg.caption || "";
  const files = [];

  if (msg.photo?.length) {
    const photo = msg.photo[msg.photo.length - 1];
    try {
      const f = await downloadFile(photo.file_id);
      files.push({ mimeType: f.mimeType, data: f.buffer.toString("base64") });
      if (!userText) userText = "(image)";
    } catch (e) {
      console.warn("[telegram] photo download:", e.message);
    }
  }
  if (msg.document?.mime_type?.startsWith("image/")) {
    try {
      const f = await downloadFile(msg.document.file_id);
      files.push({ mimeType: f.mimeType, data: f.buffer.toString("base64") });
      if (!userText) userText = "(image)";
    } catch (e) {
      console.warn("[telegram] doc download:", e.message);
    }
  }

  let cameFromVoice = false;
  if (msg.voice || msg.audio || msg.video_note) {
    if (!isSTTEnabled()) {
      await sendMessage(chatId, "🎙️ Voice not supported — add OPENAI_API_KEY to .env to enable Whisper STT.");
      return;
    }
    const fileId = msg.voice?.file_id || msg.audio?.file_id || msg.video_note?.file_id;
    try {
      await sendChatAction(chatId, "typing");
      const f = await downloadFile(fileId);
      const transcript = await transcribe(f.buffer, f.mimeType);
      if (!transcript) {
        await sendMessage(chatId, "🎙️ Couldn't make out anything — try again?");
        return;
      }
      userText = transcript;
      cameFromVoice = true;
      await sendMessage(chatId, `🎙️ _Heard:_ "${escapeMd(transcript.slice(0, 500))}"`);
    } catch (e) {
      await sendMessage(chatId, `🎙️ STT failed: ${e.message}`);
      return;
    }
  }

  if (!userText && !files.length) {
    await sendMessage(chatId, "Send me text, an image with caption, or /help.");
    return;
  }

  await sendChatAction(chatId, files.length ? "upload_photo" : "typing");

  const history = (_history.get(chatId) || []).slice();
  history.push({ role: "user", content: userText });

  try {
    const result = await _handleChat({
      messages:       history,
      files,
      mode:           "general",
      maxTokens:      2048,
      conversationId: `tg:${chatId}`,
    });
    const reply = (result?.text || "").trim() || "(no reply)";
    history.push({ role: "assistant", content: reply });
    _history.set(chatId, history.slice(-MAX_HISTORY));
    await sendMessage(chatId, reply);

    // If the user sent voice, reply with voice too (when TTS is enabled).
    if (cameFromVoice && isTTSEnabled()) {
      try {
        await sendChatAction(chatId, "record_voice");
        const audio = await synthesize(stripMdForTTS(reply));
        await sendAudio(chatId, audio, { title: "Maahi", performer: "Maahi" });
      } catch (e) {
        console.warn("[telegram] TTS reply failed:", e.message);
      }
    }
  } catch (e) {
    await sendMessage(chatId, `⚠️ Error: ${e.message}`);
  }
}

async function handleCommand(msg, chatId) {
  const cmd = msg.text.split(/\s+/)[0].toLowerCase();
  const aliases = {
    "/memory":   "Show my most recent memories — top 10 facts across categories.",
    "/tasks":    "List all my open tasks.",
    "/briefing": "Give me my full daily briefing right now.",
  };
  switch (cmd) {
    case "/start":
      await sendMessage(
        chatId,
        "🚀 *Maahi online.* I have your memory, tasks, calendar, gmail, and 29+ tools.\nText me anything.\n\n*Commands:* /reset /help /memory /tasks /briefing",
      );
      return;
    case "/help":
      await sendMessage(
        chatId,
        "*Commands:*\n`/reset` – clear conversation\n`/memory` – recent memories\n`/tasks` – open tasks\n`/briefing` – on-demand daily briefing\n`/help` – this",
      );
      return;
    case "/reset":
      _history.delete(chatId);
      await sendMessage(chatId, "🧹 Conversation cleared.");
      return;
    case "/memory":
    case "/tasks":
    case "/briefing": {
      // Re-dispatch as a regular chat message using the alias text
      const fake = { ...msg, text: aliases[cmd], entities: undefined };
      return handleUpdate({ message: fake });
    }
    default:
      await sendMessage(chatId, `Unknown command: \`${cmd}\` — try /help`);
  }
}

// ─── Utilities ──────────────────────────────────────────────────
function chunkText(s, n) {
  if (s.length <= n) return [s];
  const out = [];
  for (let i = 0; i < s.length; i += n) out.push(s.slice(i, i + n));
  return out;
}

function escapeMd(s) {
  return String(s ?? "")
    .replace(/\*/g, "")
    .replace(/_/g, "")
    .replace(/`/g, "")
    .replace(/\[/g, "(")
    .replace(/\]/g, ")");
}

// Strip markdown noise so TTS doesn't read out "asterisk".
function stripMdForTTS(s) {
  return String(s ?? "")
    .replace(/```[\s\S]*?```/g, " (code block) ")
    .replace(/`([^`]+)`/g, "$1")
    .replace(/\*\*([^*]+)\*\*/g, "$1")
    .replace(/\*([^*]+)\*/g, "$1")
    .replace(/_([^_]+)_/g, "$1")
    .replace(/^#{1,6}\s+/gm, "")
    .replace(/^[-*]\s+/gm, "")
    .replace(/\[([^\]]+)\]\([^)]+\)/g, "$1")
    .replace(/https?:\/\/\S+/g, " (link) ")
    .replace(/\n{2,}/g, ". ")
    .replace(/\s{2,}/g, " ")
    .trim();
}

function sleep(ms) { return new Promise(r => setTimeout(r, ms)); }
