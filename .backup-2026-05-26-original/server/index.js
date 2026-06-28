import express from "express";
import cors from "cors";
import dotenv from "dotenv";
import fs from "node:fs/promises";
import path from "node:path";
import { fileURLToPath } from "node:url";
import cron from "node-cron";
import nodemailer from "nodemailer";
import { google } from "googleapis";
import { initDb } from "./db.js";
import { backfillAll } from "./memory/backfill.js";
import {
  recall,
  formatForPrompt,
  writeFact,
  writePerson,
  deletePersonByName,
  getPersonByName,
  listAllPeople,
} from "./memory/retrieve.js";
import { captureTurn } from "./memory/capture.js";
import {
  isClaudeEnabled,
  activeClaudeModel,
  claudeChat,
  claudeStream,
} from "./claude.js";
import {
  isTelegramEnabled,
  startTelegramBot,
  notifyTelegram,
} from "./telegram.js";
import { isSTTEnabled, isTTSEnabled } from "./voice.js";
import { scanInbox }    from "./watchers/inbox.js";
import { scanCalendar } from "./watchers/calendar.js";
import {
  formBeliefs,
  getUnresolvedBeliefs,
  markBeliefsSurfaced,
  expireStaleBeliefs,
} from "./watchers/beliefs.js";

dotenv.config();

const app = express();
const port               = process.env.PORT || 8787;
const GEMINI_KEY         = process.env.GEMINI_API_KEY;
const MODEL              = process.env.GEMINI_MODEL || "gemini-2.5-flash";
const GOOGLE_KEY         = process.env.GOOGLE_SEARCH_KEY;
const GOOGLE_CX          = process.env.GOOGLE_SEARCH_CX;
const GMAIL_USER         = process.env.GMAIL_USER;
const GMAIL_PASS         = process.env.GMAIL_APP_PASSWORD;
const GOOGLE_CLIENT_ID   = process.env.GOOGLE_CLIENT_ID;
const GOOGLE_CLIENT_SECRET = process.env.GOOGLE_CLIENT_SECRET;
const GOOGLE_REDIRECT_URI  = process.env.GOOGLE_REDIRECT_URI || `http://localhost:${process.env.PORT||8787}/api/auth/google/callback`;

const __filename = fileURLToPath(import.meta.url);
const __dirname  = path.dirname(__filename);
const DATA = path.join(__dirname, "..", "data");
const FILES = {
  memory:        path.join(DATA, "memories.json"),
  tasks:         path.join(DATA, "tasks.json"),
  convos:        path.join(DATA, "conversations.json"),
  notes:         path.join(DATA, "notes.json"),
  reminders:     path.join(DATA, "reminders.json"),
  notifications: path.join(DATA, "notifications.json"),
  contacts:      path.join(DATA, "contacts.json"),
  google_tokens: path.join(DATA, "google_tokens.json"),
};

// ─── Data helpers ─────────────────────────────────────────────
async function ensureData() {
  await fs.mkdir(DATA, { recursive: true });
  const defaults = {
    [FILES.memory]:        { facts: {}, categories: {} },
    [FILES.tasks]:         { tasks: [] },
    [FILES.convos]:        { conversations: [] },
    [FILES.notes]:         { notes: [] },
    [FILES.reminders]:     { reminders: [] },
    [FILES.notifications]: { notifications: [] },
    [FILES.contacts]:      { contacts: [] },
  };
  for (const [file, fallback] of Object.entries(defaults)) {
    try { await fs.access(file); } catch { await fs.writeFile(file, JSON.stringify(fallback, null, 2)); }
  }
}
async function readJson(f, fallback) {
  try { return JSON.parse(await fs.readFile(f, "utf8")); } catch { return fallback; }
}
async function writeJson(f, v) { await fs.writeFile(f, JSON.stringify(v, null, 2), "utf8"); }

// ─── Memory ───────────────────────────────────────────────────
function tokenize(s) {
  return String(s||"").toLowerCase().replace(/[^a-z0-9\s]/g," ").split(/\s+/).filter(t=>t.length>2);
}
function relevance(q, t) {
  const qs=new Set(tokenize(q)); if (!qs.size) return 0;
  const ts=tokenize(t); if (!ts.length) return 0;
  let h=0; for (const w of ts) if (qs.has(w)) h++;
  return h/Math.max(qs.size,1);
}
async function getMemoryContext(query, topN=8) {
  try {
    const results = await recall(query, { limit: topN });
    return formatForPrompt(results);
  } catch (e) {
    console.warn("[memory] semantic recall failed, falling back:", e.message);
    const mem = await readJson(FILES.memory, { facts: {} });
    const entries = Object.entries(mem.facts || {});
    if (!entries.length) return "";
    return entries
      .map(([k, v]) => ({ k, v: typeof v === "object" ? v.value : v, score: relevance(query, `${k} ${typeof v === "object" ? v.value : v}`) }))
      .sort((a, b) => b.score - a.score).slice(0, topN)
      .map(x => `- ${x.k}: ${x.v}`).join("\n");
  }
}
async function saveMemoryFact(key, value, category="general") {
  const mem = await readJson(FILES.memory,{facts:{},categories:{}});
  mem.facts[key] = {value,category,updatedAt:new Date().toISOString()};
  if (!mem.categories[category]) mem.categories[category]=[];
  if (!mem.categories[category].includes(key)) mem.categories[category].push(key);
  await writeJson(FILES.memory, mem);
  try { await writeFact({ key, value, category, source: "chat" }); }
  catch (e) { console.warn("[memory] writeFact failed:", e.message); }
}
async function getAllMemory()  { return readJson(FILES.memory,{facts:{},categories:{}}); }
async function clearMemory()   { await writeJson(FILES.memory,{facts:{},categories:{}}); }

// ─── Tasks ────────────────────────────────────────────────────
async function addTask(text,priority="medium",deadline=null) {
  const d=await readJson(FILES.tasks,{tasks:[]});
  const t={id:Date.now(),text,priority,deadline,done:false,createdAt:new Date().toISOString()};
  d.tasks.push(t); await writeJson(FILES.tasks,d); return t;
}
async function listTasks(filter="open") {
  const d=await readJson(FILES.tasks,{tasks:[]});
  if (filter==="all")  return d.tasks.slice(-25);
  if (filter==="done") return d.tasks.filter(t=>t.done).slice(-25);
  return d.tasks.filter(t=>!t.done).slice(-25);
}
async function completeTask(taskId) {
  const d=await readJson(FILES.tasks,{tasks:[]});
  const t=d.tasks.find(x=>x.id===taskId);
  if (t){t.done=true;t.completedAt=new Date().toISOString();}
  await writeJson(FILES.tasks,d); return t;
}

// ─── Conversations ────────────────────────────────────────────
async function saveConversation(id,title,messages) {
  const d=await readJson(FILES.convos,{conversations:[]});
  const idx=d.conversations.findIndex(c=>c.id===id);
  const c={id,title,messages,updatedAt:new Date().toISOString()};
  if (idx>=0) d.conversations[idx]=c; else d.conversations.push(c);
  if (d.conversations.length>50) d.conversations=d.conversations.slice(-50);
  await writeJson(FILES.convos,d);
}
async function listConversations() {
  const d=await readJson(FILES.convos,{conversations:[]});
  return d.conversations.map(c=>({id:c.id,title:c.title,updatedAt:c.updatedAt,count:c.messages?.length||0}));
}
async function getConversation(id) {
  const d=await readJson(FILES.convos,{conversations:[]});
  return d.conversations.find(c=>c.id===id)||null;
}
async function deleteConversation(id) {
  const d=await readJson(FILES.convos,{conversations:[]});
  d.conversations=d.conversations.filter(c=>c.id!==id);
  await writeJson(FILES.convos,d);
}

// ─── Notes ────────────────────────────────────────────────────
async function saveNote(title,content,tags=[]) {
  const d=await readJson(FILES.notes,{notes:[]});
  const n={id:Date.now(),title,content,tags,createdAt:new Date().toISOString()};
  d.notes.push(n);
  if (d.notes.length>100) d.notes=d.notes.slice(-100);
  await writeJson(FILES.notes,d); return n;
}
async function searchNotes(query) {
  const d=await readJson(FILES.notes,{notes:[]});
  return d.notes
    .map(n=>({...n,score:relevance(query,`${n.title} ${n.content} ${(n.tags||[]).join(" ")}`)}))
    .filter(n=>n.score>0).sort((a,b)=>b.score-a.score).slice(0,5);
}

// ─── Reminders ────────────────────────────────────────────────
async function setReminder(text,datetime) {
  const d=await readJson(FILES.reminders,{reminders:[]});
  const r={id:Date.now(),text,datetime,fired:false,createdAt:new Date().toISOString()};
  d.reminders.push(r); await writeJson(FILES.reminders,d); return r;
}
async function listReminders() {
  const d=await readJson(FILES.reminders,{reminders:[]});
  return d.reminders.filter(r=>!r.fired);
}
async function deleteReminder(id) {
  const d=await readJson(FILES.reminders,{reminders:[]});
  d.reminders=d.reminders.filter(r=>r.id!==id);
  await writeJson(FILES.reminders,d);
}

// ─── Contacts ─────────────────────────────────────────────────
async function saveContact(name,data) {
  const d=await readJson(FILES.contacts,{contacts:[]});
  const idx=d.contacts.findIndex(c=>c.name.toLowerCase()===name.toLowerCase());
  const contact={id:idx>=0?d.contacts[idx].id:Date.now(),name,...data,updatedAt:new Date().toISOString()};
  if (idx>=0) d.contacts[idx]=contact; else d.contacts.push(contact);
  await writeJson(FILES.contacts,d);
  try { await writePerson({ name, ...data }); }
  catch (e) { console.warn("[memory] writePerson failed:", e.message); }
  return contact;
}
async function getContact(name) {
  const d=await readJson(FILES.contacts,{contacts:[]});
  return d.contacts.find(c=>c.name.toLowerCase().includes(name.toLowerCase()))||null;
}
async function listContacts() { return (await readJson(FILES.contacts,{contacts:[]})).contacts; }
async function searchContacts(query) {
  const d=await readJson(FILES.contacts,{contacts:[]});
  return d.contacts
    .map(c=>({...c,score:relevance(query,`${c.name} ${c.company||""} ${c.relationship||""} ${c.notes||""}`)}))
    .filter(c=>c.score>0).sort((a,b)=>b.score-a.score).slice(0,5);
}
async function deleteContact(name) {
  const d=await readJson(FILES.contacts,{contacts:[]});
  d.contacts=d.contacts.filter(c=>!c.name.toLowerCase().includes(name.toLowerCase()));
  await writeJson(FILES.contacts,d);
  try { deletePersonByName(name); }
  catch (e) { console.warn("[memory] deletePersonByName failed:", e.message); }
}

// ─── Notifications ────────────────────────────────────────────
async function pushNotification(text,type="reminder") {
  const d=await readJson(FILES.notifications,{notifications:[]});
  d.notifications.push({id:Date.now(),text,type,read:false,createdAt:new Date().toISOString()});
  if (d.notifications.length>50) d.notifications=d.notifications.slice(-50);
  await writeJson(FILES.notifications,d);
}
async function getUnreadNotifications() {
  return (await readJson(FILES.notifications,{notifications:[]})).notifications.filter(n=>!n.read);
}
async function markNotificationsRead() {
  const d=await readJson(FILES.notifications,{notifications:[]});
  d.notifications=d.notifications.map(n=>({...n,read:true}));
  await writeJson(FILES.notifications,d);
}

// ─── Google OAuth2 ────────────────────────────────────────────
function getOAuthClient() {
  if (!GOOGLE_CLIENT_ID||!GOOGLE_CLIENT_SECRET) return null;
  return new google.auth.OAuth2(GOOGLE_CLIENT_ID, GOOGLE_CLIENT_SECRET, GOOGLE_REDIRECT_URI);
}
async function getAuthClient() {
  const client = getOAuthClient(); if (!client) return null;
  const tokens = await readJson(FILES.google_tokens, null); if (!tokens) return null;
  client.setCredentials(tokens);
  client.on("tokens", async (t) => {
    const existing = await readJson(FILES.google_tokens, {});
    await writeJson(FILES.google_tokens, { ...existing, ...t });
  });
  return client;
}
async function isGoogleConnected() {
  const tokens = await readJson(FILES.google_tokens, null);
  return !!(tokens?.access_token || tokens?.refresh_token);
}

// ─── Gmail SMTP (send) ────────────────────────────────────────
function getMailer() {
  if (!GMAIL_USER||!GMAIL_PASS) return null;
  return nodemailer.createTransport({ service:"gmail", auth:{user:GMAIL_USER,pass:GMAIL_PASS} });
}

// ─── URL reader ───────────────────────────────────────────────
async function fetchUrl(url) {
  const r = await fetch(url, {
    headers:{"User-Agent":"Mozilla/5.0 (compatible; MaahiBot/1.0)"},
    signal: AbortSignal.timeout(10000),
  });
  return (await r.text())
    .replace(/<script[\s\S]*?<\/script>/gi,"")
    .replace(/<style[\s\S]*?<\/style>/gi,"")
    .replace(/<nav[\s\S]*?<\/nav>/gi,"")
    .replace(/<footer[\s\S]*?<\/footer>/gi,"")
    .replace(/<[^>]+>/g," ").replace(/\s+/g," ").trim().slice(0,10000);
}

// ─── Cron: reminders every minute ─────────────────────────────
cron.schedule("* * * * *", async () => {
  try {
    const d=await readJson(FILES.reminders,{reminders:[]}); const now=new Date(); let changed=false;
    for (const r of d.reminders) {
      if (!r.fired&&new Date(r.datetime)<=now) {
        r.fired=true; changed=true;
        const text = `⏰ Reminder: ${r.text}`;
        await pushNotification(text, "reminder");
        notifyTelegram(text).catch(() => {});
      }
    }
    if (changed) await writeJson(FILES.reminders,d);
  } catch(e) { console.error("[cron:reminder]",e.message); }
});

// ─── Cron: morning briefing 8am Dubai (4am UTC) ───────────────
cron.schedule("0 4 * * *", async () => {
  try {
    const tasks=await listTasks("open"); const mem=await getAllMemory();
    const nd=await readJson(FILES.notes,{notes:[]}); const reminders=await listReminders();
    const hi=tasks.filter(t=>t.priority==="high");
    // Pull unresolved beliefs Maahi has formed about today.
    let beliefBlock = "";
    try {
      const beliefs = getUnresolvedBeliefs(5);
      if (beliefs.length) {
        beliefBlock = `\n**Maahi's beliefs about today:**\n${beliefs.map(b => `- [${b.kind}] ${b.content}`).join("\n")}`;
        markBeliefsSurfaced(beliefs.map(b => b.id));
      }
    } catch (e) { console.warn("[cron:briefing] beliefs lookup failed:", e.message); }

    const briefing=[
      `☀️ **Good Morning, Meet!**`,
      `**${new Date().toLocaleDateString("en-US",{timeZone:"Asia/Dubai",weekday:"long",month:"long",day:"numeric"})}**`,
      tasks.length?`\n**Tasks:** ${tasks.length} open (${hi.length} high)\n${hi.map(t=>`- ${t.text}`).join("\n")}`:"No open tasks.",
      reminders.length?`\n**Reminders:**\n${reminders.map(r=>`- ${r.text} at ${new Date(r.datetime).toLocaleTimeString("en-US",{timeZone:"Asia/Dubai",hour:"2-digit",minute:"2-digit",hour12:true})}`).join("\n")}`:"",
      beliefBlock,
      nd.notes.slice(-3).length?`\n**Recent Notes:**\n${nd.notes.slice(-3).map(n=>`- ${n.title}`).join("\n")}`:"",
      `**Memory:** ${Object.keys(mem.facts||{}).length} facts stored`,
    ].filter(Boolean).join("\n");
    await pushNotification(briefing,"briefing");
    notifyTelegram(briefing).catch(() => {});
  } catch(e) { console.error("[cron:briefing]",e.message); }
});

// ─── Cron: end-of-day debrief 9pm Dubai (5pm UTC) ─────────────
cron.schedule("0 17 * * *", async () => {
  try {
    const todayISO = new Date().toISOString().slice(0, 10);
    const tasks = await listTasks("all");
    const completedToday = tasks.filter(t => t.done && t.completedAt?.startsWith(todayISO));
    const stillOpen = tasks.filter(t => !t.done);
    const high = stillOpen.filter(t => t.priority === "high");

    let summary = `🌙 *End of day, Meet.*`;
    if (completedToday.length) {
      summary += `\n\n*Shipped today (${completedToday.length}):*\n${completedToday.slice(0,8).map(t => `- ${t.text}`).join("\n")}`;
    } else {
      summary += `\n\n_No tasks marked done today._`;
    }
    if (high.length) {
      summary += `\n\n*High-priority still open (${high.length}):*\n${high.slice(0,5).map(t => `- ${t.text}`).join("\n")}`;
    }
    summary += `\n\nOpen: ${stillOpen.length}. Rest well.`;

    notifyTelegram(summary).catch(() => {});
    await pushNotification(summary, "debrief");
  } catch(e) { console.error("[cron:debrief]", e.message); }
});

// ─── Cron: inbox watcher every 15 min ─────────────────────────
cron.schedule("*/15 * * * *", async () => {
  try {
    const auth = await getAuthClient();
    if (!auth) return;
    const result = await scanInbox({ auth });
    if (result?.surfaced) console.log(`[cron:inbox] surfaced ${result.surfaced}/${result.scanned}`);
  } catch(e) { console.error("[cron:inbox]", e.message); }
});

// ─── Cron: calendar watcher every 10 min ──────────────────────
cron.schedule("*/10 * * * *", async () => {
  try {
    const auth = await getAuthClient();
    if (!auth) return;
    const result = await scanCalendar({ auth });
    if (result?.prepped) console.log(`[cron:calendar] prepped ${result.prepped} event(s)`);
  } catch(e) { console.error("[cron:calendar]", e.message); }
});

// ─── Cron: belief formation 7am Dubai (3am UTC) — before briefing ──
cron.schedule("0 3 * * *", async () => {
  try {
    expireStaleBeliefs();
    const result = await formBeliefs();
    if (result?.formed) console.log(`[cron:beliefs] formed ${result.formed} new belief(s)`);
  } catch(e) { console.error("[cron:beliefs]", e.message); }
});

// ─── Tools ────────────────────────────────────────────────────
const TOOLS = [
  // Web & Research
  { name:"web_search",          description:"Search Google for current info, news, trends.", input_schema:{type:"object",properties:{query:{type:"string"}},required:["query"]} },
  { name:"read_url",            description:"Read and extract content from any URL. Use immediately when Meet shares a link.", input_schema:{type:"object",properties:{url:{type:"string"},question:{type:"string",description:"What to look for"}},required:["url"]} },
  { name:"summarize_youtube",   description:"Get info from a YouTube video URL.", input_schema:{type:"object",properties:{url:{type:"string"}},required:["url"]} },
  { name:"search_linkedin",     description:"Search for a person or company on LinkedIn. Returns profile info, job history, posts.", input_schema:{type:"object",properties:{name:{type:"string",description:"Person or company name"},company:{type:"string",description:"Company name (optional)"},info_type:{type:"string",enum:["profile","company","posts","all"],description:"What to look for"}},required:["name"]} },
  // Memory
  { name:"remember_fact",       description:"Save a fact about Meet.", input_schema:{type:"object",properties:{key:{type:"string"},value:{type:"string"},category:{type:"string",enum:["personal","business","preferences","goals","people","general"]}},required:["key","value"]} },
  { name:"recall_memory",       description:"Search memories about Meet.", input_schema:{type:"object",properties:{query:{type:"string"}},required:["query"]} },
  // Tasks
  { name:"add_task",            description:"Add a task.", input_schema:{type:"object",properties:{text:{type:"string"},priority:{type:"string",enum:["high","medium","low"]},deadline:{type:"string"}},required:["text"]} },
  { name:"list_tasks",          description:"Show tasks.", input_schema:{type:"object",properties:{filter:{type:"string",enum:["open","done","all"]}}} },
  { name:"complete_task",       description:"Mark task done.", input_schema:{type:"object",properties:{task_id:{type:"number"}},required:["task_id"]} },
  // Notes
  { name:"save_note",           description:"Save a note, draft, SOP, idea.", input_schema:{type:"object",properties:{title:{type:"string"},content:{type:"string"},tags:{type:"array",items:{type:"string"}}},required:["title","content"]} },
  { name:"search_notes",        description:"Search saved notes.", input_schema:{type:"object",properties:{query:{type:"string"}},required:["query"]} },
  // Contacts
  { name:"save_contact",        description:"Save or update a person. Use when Meet mentions anyone.", input_schema:{type:"object",properties:{name:{type:"string"},company:{type:"string"},relationship:{type:"string"},email:{type:"string"},phone:{type:"string"},notes:{type:"string"},last_interaction:{type:"string"}},required:["name"]} },
  { name:"get_contact",         description:"Look up a person by name.", input_schema:{type:"object",properties:{name:{type:"string"}},required:["name"]} },
  { name:"search_contacts",     description:"Search contacts.", input_schema:{type:"object",properties:{query:{type:"string"}},required:["query"]} },
  { name:"list_contacts",       description:"List all contacts.", input_schema:{type:"object",properties:{}} },
  { name:"delete_contact",      description:"Delete a contact.", input_schema:{type:"object",properties:{name:{type:"string"}},required:["name"]} },
  // Gmail (OAuth2)
  { name:"read_gmail",          description:"Read Gmail inbox. Search emails by sender, subject, keyword, or date.", input_schema:{type:"object",properties:{query:{type:"string",description:"Gmail search query e.g. 'from:boss@example.com' or 'subject:invoice' or 'is:unread'"},max_results:{type:"number",description:"Number of emails (default 5, max 10)"},read_full:{type:"boolean",description:"Read full body of first result"}},required:["query"]} },
  { name:"send_email",          description:"Send an email.", input_schema:{type:"object",properties:{to:{type:"string"},subject:{type:"string"},body:{type:"string"},cc:{type:"string"}},required:["to","subject","body"]} },
  // Google Calendar (OAuth2)
  { name:"list_calendar",       description:"List upcoming calendar events. Use when Meet asks about schedule, meetings, or what's coming up.", input_schema:{type:"object",properties:{days:{type:"number",description:"Days ahead to look (default 7)"},max_results:{type:"number",description:"Max events (default 10)"}}} },
  { name:"create_calendar_event",description:"Create a calendar event.", input_schema:{type:"object",properties:{title:{type:"string"},start_datetime:{type:"string",description:"ISO 8601 with timezone e.g. 2025-03-26T14:00:00+04:00"},end_datetime:{type:"string",description:"ISO 8601 with timezone"},description:{type:"string"},location:{type:"string"},attendees:{type:"array",items:{type:"string"},description:"List of email addresses"}},required:["title","start_datetime","end_datetime"]} },
  { name:"delete_calendar_event",description:"Delete a calendar event by ID.", input_schema:{type:"object",properties:{event_id:{type:"string"}},required:["event_id"]} },
  // Reminders
  { name:"set_reminder",        description:"Set a reminder at exact datetime.", input_schema:{type:"object",properties:{text:{type:"string"},datetime:{type:"string",description:"ISO 8601"}},required:["text","datetime"]} },
  { name:"list_reminders",      description:"Show active reminders.", input_schema:{type:"object",properties:{}} },
  { name:"delete_reminder",     description:"Delete a reminder.", input_schema:{type:"object",properties:{id:{type:"number"}},required:["id"]} },
  // Utilities
  { name:"get_datetime",        description:"Get current date/time (Dubai by default).", input_schema:{type:"object",properties:{timezone:{type:"string"}}} },
  { name:"calculate",           description:"Evaluate math.", input_schema:{type:"object",properties:{expression:{type:"string"}},required:["expression"]} },
  { name:"generate_document",   description:"Create structured document.", input_schema:{type:"object",properties:{type:{type:"string",enum:["email","sop","proposal","brief","framework","report","checklist"]},title:{type:"string"},content:{type:"string"},tags:{type:"array",items:{type:"string"}}},required:["type","title","content"]} },
  { name:"daily_briefing",      description:"Full daily briefing.", input_schema:{type:"object",properties:{}} },
  { name:"generate_image",      description:"Generate an image from a description.", input_schema:{type:"object",properties:{prompt:{type:"string"}},required:["prompt"]} },
];

// ─── Tool execution ───────────────────────────────────────────
async function executeTool(name, input) {
  switch (name) {

    case "web_search": {
      try {
        if (GOOGLE_KEY&&GOOGLE_CX) {
          const url=`https://www.googleapis.com/customsearch/v1?key=${GOOGLE_KEY}&cx=${GOOGLE_CX}&q=${encodeURIComponent(input.query||"")}&num=5`;
          const r=await fetch(url); const data=await r.json();
          if (data.error) throw new Error(data.error.message);
          if (data.items?.length) return `Google results for "${input.query}":\n\n`+data.items.map(i=>`**${i.title}**\n${i.snippet}\n${i.link}`).join("\n\n");
          return `No results for "${input.query}".`;
        }
        const r=await fetch(`https://api.duckduckgo.com/?q=${encodeURIComponent(input.query||"")}&format=json&no_html=1&no_redirect=1`);
        const data=await r.json();
        return `**${data?.Heading||input.query}**\n${data?.AbstractText||"No summary."}\n${(data?.RelatedTopics||[]).slice(0,5).map(x=>x?.Text).filter(Boolean).join("\n")}`;
      } catch(e) { return `Search failed: ${e.message}`; }
    }

    case "read_url": {
      try {
        const text = await fetchUrl(input.url);
        return `**From:** ${input.url}${input.question?`\n(Focus: "${input.question}")`:"" }\n\n${text}`;
      } catch(e) { return `Failed to read URL: ${e.message}`; }
    }

    case "summarize_youtube": {
      try {
        const videoId = input.url.match(/(?:v=|youtu\.be\/|shorts\/)([^&\n?#]+)/)?.[1];
        if (!videoId) return "Could not extract YouTube video ID.";
        const r=await fetch(`https://www.youtube.com/watch?v=${videoId}`,{headers:{"User-Agent":"Mozilla/5.0"}});
        const html=await r.text();
        const title=html.match(/"title":"([^"]+)"/)?.[1]?.replace(/\\u[\dA-F]{4}/gi,c=>String.fromCharCode(parseInt(c.replace(/\\u/,""),16)))||"Unknown";
        const description=html.match(/"shortDescription":"([^"]+)"/)?.[1]?.replace(/\\n/g,"\n").replace(/\\"/g,'"')||"";
        const channel=html.match(/"ownerChannelName":"([^"]+)"/)?.[1]||"";
        return `**YouTube: ${title}**\nChannel: ${channel}\nURL: ${input.url}\n\n${description.slice(0,2000)}`;
      } catch(e) { return `YouTube fetch failed: ${e.message}`; }
    }

    case "search_linkedin": {
      try {
        const type = input.info_type || "profile";
        const searchType = type === "company" ? "linkedin.com/company" : "linkedin.com/in";
        const query = `site:${searchType} "${input.name}"${input.company?` "${input.company}"`:""}`;
        // Search Google for the LinkedIn profile
        let searchResult = "";
        if (GOOGLE_KEY && GOOGLE_CX) {
          const url=`https://www.googleapis.com/customsearch/v1?key=${GOOGLE_KEY}&cx=${GOOGLE_CX}&q=${encodeURIComponent(query)}&num=5`;
          const r=await fetch(url); const data=await r.json();
          if (data.items?.length) {
            searchResult = data.items.map(i=>`**${i.title}**\n${i.snippet}\n${i.link}`).join("\n\n");
            // Try to read the first LinkedIn URL
            const linkedinUrl = data.items[0]?.link;
            if (linkedinUrl) {
              try {
                const pageContent = await fetchUrl(linkedinUrl);
                if (pageContent.length > 200) {
                  return `**LinkedIn: ${input.name}**\nURL: ${linkedinUrl}\n\n${pageContent.slice(0,3000)}`;
                }
              } catch(_) {}
            }
            return `**LinkedIn search: ${input.name}**\n\n${searchResult}`;
          }
        }
        return `No LinkedIn results found for "${input.name}". Try connecting Google Search in settings.`;
      } catch(e) { return `LinkedIn search failed: ${e.message}`; }
    }

    case "remember_fact": {
      await saveMemoryFact(input.key, input.value, input.category||"general");
      return `Remembered: ${input.key} = ${input.value}`;
    }
    case "recall_memory": {
      try {
        const results = await recall(input.query, { limit: 10 });
        if (!results.length) return "No matching memories.";
        return formatForPrompt(results);
      } catch (e) {
        const mem = await getAllMemory();
        const results = Object.entries(mem.facts || {})
          .map(([k, v]) => ({ k, v: typeof v === "object" ? v.value : v, score: relevance(input.query, `${k} ${typeof v === "object" ? v.value : v}`) }))
          .filter(x => x.score > 0).sort((a, b) => b.score - a.score).slice(0, 8);
        return results.length ? results.map(r => `- **${r.k}**: ${r.v}`).join("\n") : "No matching memories.";
      }
    }

    case "add_task":      { const t=await addTask(input.text,input.priority||"medium",input.deadline||null); return `Task added (ID:${t.id}): "${t.text}" [${t.priority}]${t.deadline?` due ${t.deadline}`:""}`;  }
    case "list_tasks":    { const tasks=await listTasks(input.filter||"open"); return tasks.length?tasks.map((t,i)=>`${i+1}. [${t.done?"DONE":t.priority?.toUpperCase()}] ${t.text} (ID:${t.id})${t.deadline?` due:${t.deadline}`:""}`).join("\n"):"No tasks."; }
    case "complete_task": { const t=await completeTask(input.task_id); return t?`Completed: "${t.text}"`:"Task not found."; }

    case "save_note":    { const n=await saveNote(input.title,input.content,input.tags||[]); return `Note saved: "${n.title}" (ID:${n.id})`; }
    case "search_notes": { const notes=await searchNotes(input.query); return notes.length?notes.map(n=>`**${n.title}**\n${n.content.slice(0,200)}...`).join("\n\n"):"No matching notes."; }

    case "save_contact": {
      const c=await saveContact(input.name,{company:input.company,relationship:input.relationship,email:input.email,phone:input.phone,notes:input.notes,last_interaction:input.last_interaction});
      return `Contact saved: ${c.name}${c.company?` @ ${c.company}`:""}${c.relationship?` (${c.relationship})`:""}`;
    }
    case "get_contact": {
      const c=await getContact(input.name);
      if (!c) return `No contact found for "${input.name}".`;
      return [`**${c.name}**${c.company?` @ ${c.company}`:""}`,c.relationship&&`Role: ${c.relationship}`,c.email&&`Email: ${c.email}`,c.phone&&`Phone: ${c.phone}`,c.last_interaction&&`Last seen: ${c.last_interaction}`,c.notes&&`Notes: ${c.notes}`].filter(Boolean).join("\n");
    }
    case "search_contacts": {
      try {
        const results = await recall(input.query, { kinds: ["people"], limit: 8 });
        if (results.length) return results.map(r => `- **${r.name}**${r.company?` @ ${r.company}`:""}${r.role?` (${r.role})`:""}${r.relationship?` — ${r.relationship}`:""}${r.notes?`\n  ${r.notes.slice(0,160)}`:""}`).join("\n");
      } catch (e) { /* fall through to legacy keyword search */ }
      const cs = await searchContacts(input.query);
      return cs.length ? cs.map(c => `- **${c.name}**${c.company?` @ ${c.company}`:""}${c.relationship?` (${c.relationship})`:""}`).join("\n") : "No contacts found.";
    }
    case "list_contacts":   { const cs=await listContacts(); return cs.length?cs.map(c=>`- **${c.name}**${c.company?` @ ${c.company}`:""}${c.relationship?` (${c.relationship})`:""}`).join("\n"):"No contacts saved."; }
    case "delete_contact":  { await deleteContact(input.name); return `Contact "${input.name}" deleted.`; }

    case "read_gmail": {
      const auth = await getAuthClient();
      if (!auth) return "Gmail not connected. Click **Connect Google** in the app header to authorize.";
      try {
        const gmail = google.gmail({version:"v1",auth});
        const list = await gmail.users.messages.list({userId:"me",q:input.query||"is:inbox",maxResults:Math.min(input.max_results||5,10)});
        const msgs = list.data.messages||[];
        if (!msgs.length) return "No emails found matching that query.";
        const details = await Promise.all(msgs.slice(0,input.read_full?1:5).map(async m=>{
          const msg = await gmail.users.messages.get({userId:"me",id:m.id,format:input.read_full?"full":"metadata",metadataHeaders:["From","To","Subject","Date"]});
          const headers = msg.data.payload?.headers||[];
          const get = k => headers.find(h=>h.name.toLowerCase()===k.toLowerCase())?.value||"";
          const subject=get("Subject"); const from=get("From"); const date=get("Date");
          if (input.read_full) {
            let body="";
            const parts=msg.data.payload?.parts||[];
            const textPart=parts.find(p=>p.mimeType==="text/plain")||msg.data.payload;
            if (textPart?.body?.data) body=Buffer.from(textPart.body.data,"base64").toString("utf8").slice(0,3000);
            return `**From:** ${from}\n**Subject:** ${subject}\n**Date:** ${date}\n\n${body}`;
          }
          return `- **${subject}** | From: ${from} | ${date}`;
        }));
        return `**Gmail: "${input.query}"** (${msgs.length} results)\n\n${details.join("\n")}`;
      } catch(e) { return `Gmail error: ${e.message}`; }
    }

    case "send_email": {
      // Try OAuth first, fall back to SMTP
      const auth = await getAuthClient();
      if (auth) {
        try {
          const gmail = google.gmail({version:"v1",auth});
          const isHtml = input.body.includes("<");
          const raw = Buffer.from(
            `From: ${GMAIL_USER}\r\nTo: ${input.to}\r\n${input.cc?`Cc: ${input.cc}\r\n`:""}`+
            `Subject: ${input.subject}\r\nContent-Type: ${isHtml?"text/html":"text/plain"}; charset=utf-8\r\n\r\n${input.body}`
          ).toString("base64url");
          await gmail.users.messages.send({userId:"me",requestBody:{raw}});
          return `Email sent to ${input.to} — "${input.subject}"`;
        } catch(e) { return `Gmail send failed: ${e.message}`; }
      }
      const mailer = getMailer();
      if (!mailer) return "Email not configured. Add GMAIL credentials or connect Google account.";
      try {
        await mailer.sendMail({from:`Maahi <${GMAIL_USER}>`,to:input.to,cc:input.cc,subject:input.subject,html:input.body.includes("<")?input.body:input.body.replace(/\n/g,"<br>"),text:input.body});
        return `Email sent to ${input.to} — "${input.subject}"`;
      } catch(e) { return `Email failed: ${e.message}`; }
    }

    case "list_calendar": {
      const auth = await getAuthClient();
      if (!auth) return "Google Calendar not connected. Click **Connect Google** in the app header.";
      try {
        const cal = google.calendar({version:"v3",auth});
        const now = new Date();
        const end = new Date(now.getTime()+(input.days||7)*24*60*60*1000);
        const res = await cal.events.list({calendarId:"primary",timeMin:now.toISOString(),timeMax:end.toISOString(),maxResults:input.max_results||15,singleEvents:true,orderBy:"startTime"});
        const events = res.data.items||[];
        if (!events.length) return `No events in the next ${input.days||7} days.`;
        return `**Calendar — next ${input.days||7} days:**\n\n`+events.map(e=>{
          const start = e.start?.dateTime||e.start?.date;
          const dt = start ? new Date(start).toLocaleString("en-US",{timeZone:"Asia/Dubai",weekday:"short",month:"short",day:"numeric",hour:"2-digit",minute:"2-digit",hour12:true}) : "All day";
          return `- **${e.summary||"(no title)"}** — ${dt}${e.location?` @ ${e.location}`:""}${e.id?` (ID: ${e.id})`:""}`;
        }).join("\n");
      } catch(e) { return `Calendar error: ${e.message}`; }
    }

    case "create_calendar_event": {
      const auth = await getAuthClient();
      if (!auth) return "Google Calendar not connected. Click **Connect Google** in the app header.";
      try {
        const cal = google.calendar({version:"v3",auth});
        const event = {
          summary: input.title,
          description: input.description,
          location: input.location,
          start: {dateTime:input.start_datetime,timeZone:"Asia/Dubai"},
          end:   {dateTime:input.end_datetime,  timeZone:"Asia/Dubai"},
          attendees: input.attendees?.map(e=>({email:e})),
        };
        const res = await cal.events.insert({calendarId:"primary",resource:event,sendUpdates:input.attendees?.length?"all":"none"});
        const link = res.data.htmlLink;
        return `Event created: **${input.title}**\nStart: ${input.start_datetime}\n${link?`Link: ${link}`:""}`;
      } catch(e) { return `Calendar error: ${e.message}`; }
    }

    case "delete_calendar_event": {
      const auth = await getAuthClient();
      if (!auth) return "Google Calendar not connected.";
      try {
        const cal = google.calendar({version:"v3",auth});
        await cal.events.delete({calendarId:"primary",eventId:input.event_id});
        return `Event ${input.event_id} deleted.`;
      } catch(e) { return `Delete failed: ${e.message}`; }
    }

    case "set_reminder": {
      const r=await setReminder(input.text,input.datetime);
      return `Reminder set (ID:${r.id}): "${r.text}" at ${new Date(input.datetime).toLocaleString("en-US",{timeZone:"Asia/Dubai",weekday:"short",month:"short",day:"numeric",hour:"2-digit",minute:"2-digit",hour12:true})} (Dubai)`;
    }
    case "list_reminders": { const rs=await listReminders(); return rs.length?rs.map(r=>`- (ID:${r.id}) "${r.text}" at ${new Date(r.datetime).toLocaleString("en-US",{timeZone:"Asia/Dubai"})}`).join("\n"):"No active reminders."; }
    case "delete_reminder": { await deleteReminder(input.id); return `Reminder ${input.id} deleted.`; }

    case "get_datetime": {
      const tz=input.timezone||"Asia/Dubai";
      return `**${new Date().toLocaleString("en-US",{timeZone:tz,weekday:"long",year:"numeric",month:"long",day:"numeric",hour:"2-digit",minute:"2-digit",hour12:true})}** (${tz})`;
    }
    case "calculate": {
      try {
        const expr=String(input.expression||"");
        const safe=expr.replace(/[^0-9+\-*/().%\se]/gi,"");
        if (safe.length<expr.replace(/\s/g,"").length) return "Invalid expression.";
        return `${expr} = **${Function(`"use strict"; return (${safe})`)()} **`;
      } catch(e) { return `Calc error: ${e.message}`; }
    }
    case "generate_document": {
      const n=await saveNote(`[${input.type.toUpperCase()}] ${input.title}`,input.content,[...(input.tags||[]),input.type]);
      return `Document saved (ID:${n.id}).\n\n${input.content}`;
    }
    case "daily_briefing": {
      const tasks=await listTasks("open"); const mem=await getAllMemory();
      const nd=await readJson(FILES.notes,{notes:[]}); const reminders=await listReminders(); const contacts=await listContacts();
      const hi=tasks.filter(t=>t.priority==="high");
      return [
        `**${new Date().toLocaleDateString("en-US",{timeZone:"Asia/Dubai",weekday:"long",year:"numeric",month:"long",day:"numeric"})}**`,
        `\n**Tasks:** ${tasks.length} open (${hi.length} high priority)`,
        hi.length?`**High:**\n${hi.map(t=>`- ${t.text}`).join("\n")}`:"",
        tasks.length?`**All Open:**\n${tasks.map((t,i)=>`${i+1}. [${t.priority}] ${t.text}`).join("\n")}`:"No tasks.",
        reminders.length?`\n**Reminders:** ${reminders.length}\n${reminders.map(r=>`- "${r.text}" at ${new Date(r.datetime).toLocaleString("en-US",{timeZone:"Asia/Dubai"})}`).join("\n")}`:"",
        nd.notes.slice(-3).length?`\n**Recent Notes:**\n${nd.notes.slice(-3).map(n=>`- ${n.title}`).join("\n")}`:"",
        `\n**Contacts:** ${contacts.length} | **Memory:** ${Object.keys(mem.facts||{}).length} facts`,
      ].filter(Boolean).join("\n");
    }
    case "generate_image": {
      try {
        const body={contents:[{role:"user",parts:[{text:`Generate an image: ${input.prompt}`}]}],generationConfig:{responseModalities:["TEXT","IMAGE"]}};
        const r=await fetch(`https://generativelanguage.googleapis.com/v1beta/models/gemini-2.0-flash-preview-image-generation:generateContent?key=${GEMINI_KEY}`,{method:"POST",headers:{"content-type":"application/json"},body:JSON.stringify(body)});
        const data=await r.json();
        if (data.error) return `Image generation failed: ${data.error.message}`;
        const imgPart=data.candidates?.[0]?.content?.parts?.find(p=>p.inlineData);
        if (!imgPart) return "No image returned.";
        return `IMAGE:${imgPart.inlineData.mimeType}:${imgPart.inlineData.data}`;
      } catch(e) { return `Image generation failed: ${e.message}`; }
    }

    default: return `Unknown tool: ${name}`;
  }
}

// ─── Agent modes ──────────────────────────────────────────────
const MODES = {
  general: "All-purpose strategic advisor. Startups, ops, product, sales, life.",
  ops:     "Operations commander. Systems, bottlenecks, cadence, accountability.",
  sales:   "Sales strategist. Pipeline, objections, velocity, outreach.",
  soulmap: "Soulmap product architect. Matching, growth loops, UX, retention.",
  writer:  "Content creator. Stories, copy, social posts. Match Meet's voice.",
  analyst: "Data analyst. Research, competitive intel, financial modeling.",
};

const BASE_SYSTEM = `You are Maahi, Meet Patel's personal AI — more powerful than Jarvis.

## Meet Patel:
- Serial entrepreneur, 8+ years. Dubai, UAE.
- Founder: StartupOS, ZeroHuman, MealVerse
- At Finanshels (operations & Pre-Sales)
- Building Soulmap — AI dating app with soul profiling + agent-to-agent matching
- Authored two love stories. Behavioral science student. Storyteller.

## Tools — USE WITHOUT ASKING:
Search: web_search, read_url, summarize_youtube, search_linkedin
Memory: remember_fact, recall_memory
Tasks: add_task, list_tasks, complete_task
Notes: save_note, search_notes
Contacts: save_contact, get_contact, search_contacts, list_contacts, delete_contact
Gmail: read_gmail, send_email
Calendar: list_calendar, create_calendar_event, delete_calendar_event
Reminders: set_reminder, list_reminders, delete_reminder
Utilities: get_datetime, calculate, generate_document, daily_briefing, generate_image

## Auto-behaviors:
- URL shared → read_url immediately
- Person mentioned → save_contact
- "Remind me" → get_datetime first, then set_reminder (ISO format, UTC+4 Dubai)
- Personal info shared → remember_fact
- Task mentioned → add_task
- IMAGE:... returned → tell Meet the image is ready (UI renders it)

## Email: Always show draft BEFORE sending unless explicitly told to send directly.
## Calendar: Always confirm datetime in Dubai timezone before creating events.
## Tone: Warm, witty, sharp, practical. Never robotic. Never "As an AI".`;

function buildSystem(mode, memCtx) {
  const { base, volatile } = buildSystemParts(mode, memCtx);
  return volatile ? `${base}\n\n${volatile}` : base;
}

// Split for prompt caching: the frozen BASE_SYSTEM gets cached;
// mode + memory context vary per request and live in the second block.
function buildSystemParts(mode, memCtx) {
  const modeDesc = MODES[mode] || MODES.general;
  const lines = [`## Mode: ${modeDesc}`];
  if (memCtx) lines.push(`\n## Known context about Meet:\n${memCtx}`);
  return { base: BASE_SYSTEM, volatile: lines.join("\n") };
}

// ─── Gemini helpers ───────────────────────────────────────────
function buildGeminiTools() {
  return [{functionDeclarations:TOOLS.map(t=>({name:t.name,description:t.description,parameters:t.input_schema}))}];
}
function toGeminiContents(messages, files=[]) {
  return messages.map((m,i)=>{
    const role=m.role==="assistant"?"model":"user";
    const parts=[{text:String(m.content||" ")}];
    if (m.role==="user"&&i===messages.length-1&&files.length>0)
      for (const f of files) parts.push({inlineData:{mimeType:f.mimeType,data:f.data}});
    return {role,parts};
  });
}

// ─── Streaming agent loop ──────────────────────────────────────
async function streamAgentLoop({system,messages,maxTokens,files=[],maxIter=8,res}) {
  let contents=toGeminiContents(messages,files); const allTools=[];
  for (let i=0;i<maxIter;i++) {
    const body={contents,systemInstruction:{parts:[{text:system}]},tools:buildGeminiTools(),generationConfig:{maxOutputTokens:maxTokens}};
    const apiRes=await fetch(`https://generativelanguage.googleapis.com/v1beta/models/${MODEL}:streamGenerateContent?alt=sse&key=${GEMINI_KEY}`,{method:"POST",headers:{"content-type":"application/json"},body:JSON.stringify(body)});
    if (!apiRes.ok||!apiRes.body){const err=await apiRes.json().catch(()=>({}));res.write(`data: ${JSON.stringify({type:"error",message:err?.error?.message||"Stream failed"})}\n\n`);return{toolCalls:allTools};}
    const reader=apiRes.body.getReader(); const dec=new TextDecoder();
    let buf="",fullText="",functionCalls=[];
    while(true){const{done,value}=await reader.read();if(done)break;buf+=dec.decode(value,{stream:true});const lines=buf.split("\n");buf=lines.pop()||"";
      for(const line of lines){const tr=line.trim();if(!tr.startsWith("data:"))continue;const raw=tr.slice(5).trim();if(!raw||raw==="[DONE]")continue;let e;try{e=JSON.parse(raw);}catch{continue;}
        for(const part of e.candidates?.[0]?.content?.parts||[]){
          if(part.text){fullText+=part.text;res.write(`data: ${JSON.stringify({type:"token",text:part.text})}\n\n`);}
          if(part.functionCall)functionCalls.push(part.functionCall);
        }
      }
    }
    if(!functionCalls.length)return{toolCalls:allTools,text:fullText};
    const results=[];
    for(const fc of functionCalls){
      res.write(`data: ${JSON.stringify({type:"tool_call",name:fc.name,input:fc.args})}\n\n`);
      const result=await executeTool(fc.name,fc.args||{});
      allTools.push({name:fc.name,input:fc.args,result});
      if(typeof result==="string"&&result.startsWith("IMAGE:")){
        const[,mimeType,b64]=result.split(":");
        res.write(`data: ${JSON.stringify({type:"image",mimeType,data:b64})}\n\n`);
        results.push({functionResponse:{name:fc.name,response:{result:"Image generated."}}});
      } else {
        res.write(`data: ${JSON.stringify({type:"tool_result",name:fc.name,result})}\n\n`);
        results.push({functionResponse:{name:fc.name,response:{result}}});
      }
    }
    contents=[...contents,{role:"model",parts:functionCalls.map(fc=>({functionCall:fc}))},{role:"user",parts:results}];
  }
  return{toolCalls:allTools};
}

// ─── Non-streaming agent loop ──────────────────────────────────
async function agentLoop({system,messages,maxTokens,files=[],maxIter=8}) {
  let contents=toGeminiContents(messages,files); const allTools=[];
  for(let i=0;i<maxIter;i++){
    const body={contents,systemInstruction:{parts:[{text:system}]},tools:buildGeminiTools(),generationConfig:{maxOutputTokens:maxTokens}};
    const r=await fetch(`https://generativelanguage.googleapis.com/v1beta/models/${MODEL}:generateContent?key=${GEMINI_KEY}`,{method:"POST",headers:{"content-type":"application/json"},body:JSON.stringify(body)});
    const data=await r.json();
    if(data?.error)return{text:`Error: ${data.error.message}`,toolCalls:allTools};
    const parts=data.candidates?.[0]?.content?.parts||[];
    const funcCalls=parts.filter(p=>p.functionCall).map(p=>p.functionCall);
    if(!funcCalls.length)return{text:parts.filter(p=>p.text).map(p=>p.text).join("\n"),toolCalls:allTools};
    const results=[];
    for(const fc of funcCalls){const result=await executeTool(fc.name,fc.args||{});allTools.push({name:fc.name,input:fc.args,result});results.push({functionResponse:{name:fc.name,response:{result:typeof result==="string"&&result.startsWith("IMAGE:")?"Image generated.":result}}});}
    contents=[...contents,{role:"model",parts:funcCalls.map(fc=>({functionCall:fc}))},{role:"user",parts:results}];
  }
  return{text:"Hit max iterations.",toolCalls:allTools};
}

// ─── Express ───────────────────────────────────────────────────
const ALLOWED_ORIGINS=process.env.ALLOWED_ORIGINS?process.env.ALLOWED_ORIGINS.split(","):["http://localhost:8787","http://127.0.0.1:8787"];
app.use(cors({origin:ALLOWED_ORIGINS,credentials:true}));
app.use(express.json({limit:"20mb"}));
await ensureData();

// ─── Memory layer (SQLite + sqlite-vec + Voyage embeddings) ───
try {
  await initDb();
  const backfillResult = await backfillAll();
  if (backfillResult.skipped) {
    console.log("[memory] backfill skipped:", backfillResult.reason);
  } else {
    console.log("[memory] backfilled:", backfillResult.stats);
  }
} catch (e) {
  console.warn("[memory] init failed — semantic recall disabled:", e.message);
}

// ─── Telegram bot (mobile reach) ──────────────────────────────
if (isTelegramEnabled()) {
  startTelegramBot({ handleChat: runChatRequest })
    .catch(e => console.warn("[telegram] start failed:", e.message));
}

app.use(express.static(path.join(__dirname,"..","dist")));

// ─── Routes ───────────────────────────────────────────────────
app.get("/api/health", async (_req,res) => res.json({
  ok: true,
  brain: isClaudeEnabled() ? "claude" : "gemini",
  model: isClaudeEnabled() ? activeClaudeModel() : MODEL,
  geminiModel: MODEL,
  claudeEnabled: isClaudeEnabled(),
  telegramEnabled: isTelegramEnabled(),
  sttEnabled: isSTTEnabled(),
  ttsEnabled: isTTSEnabled(),
  tools: TOOLS.length,
  gmail: !!(GMAIL_USER && GMAIL_PASS),
  googleSearch: !!(GOOGLE_KEY && GOOGLE_CX),
  googleConnected: await isGoogleConnected(),
}));

// Google OAuth
app.get("/api/auth/google", (_req,res) => {
  const client=getOAuthClient();
  if (!client) return res.status(500).send("Google OAuth not configured. Add GOOGLE_CLIENT_ID and GOOGLE_CLIENT_SECRET to .env");
  const url=client.generateAuthUrl({access_type:"offline",prompt:"consent",scope:["https://www.googleapis.com/auth/gmail.modify","https://www.googleapis.com/auth/calendar","profile","email"]});
  res.redirect(url);
});
app.get("/api/auth/google/callback", async (req,res) => {
  const client=getOAuthClient();
  if (!client) return res.status(500).send("OAuth not configured.");
  try {
    const {tokens}=await client.getToken(req.query.code);
    await writeJson(FILES.google_tokens,tokens);
    res.send(`<html><body style="font-family:system-ui;background:#070c18;color:#e2e8f0;display:flex;align-items:center;justify-content:center;height:100vh;margin:0"><div style="text-align:center"><div style="font-size:48px;margin-bottom:16px">✅</div><h2>Google Connected!</h2><p style="color:#64748b">Gmail & Calendar are now active in Maahi.</p><button onclick="window.close()" style="margin-top:16px;padding:10px 24px;background:#38bdf8;color:#000;border:none;border-radius:8px;cursor:pointer;font-weight:600">Close</button></div></body></html>`);
  } catch(e) { res.status(500).send(`Auth failed: ${e.message}`); }
});
app.get("/api/auth/google/status",  async (_req,res) => res.json({connected:await isGoogleConnected()}));
app.delete("/api/auth/google",      async (_req,res) => { try{await fs.unlink(FILES.google_tokens);}catch{} res.json({ok:true}); });

app.get("/api/memory",    async (_req,res) => res.json(await getAllMemory()));
app.delete("/api/memory", async (_req,res) => {await clearMemory();res.json({ok:true});});
app.get("/api/tasks",     async (req,res) => res.json({tasks:await listTasks(req.query.filter||"open")}));
app.get("/api/conversations",        async (_req,res) => res.json({conversations:await listConversations()}));
app.get("/api/conversations/:id",    async (req,res) => {const c=await getConversation(req.params.id);c?res.json(c):res.status(404).json({error:"Not found"});});
app.post("/api/conversations",       async (req,res) => {const{id,title,messages}=req.body;await saveConversation(id,title,messages);res.json({ok:true});});
app.delete("/api/conversations/:id", async (req,res) => {await deleteConversation(req.params.id);res.json({ok:true});});
app.get("/api/notes",     async (req,res) => {if(req.query.q)return res.json({notes:await searchNotes(req.query.q)});const d=await readJson(FILES.notes,{notes:[]});res.json({notes:d.notes.slice(-20)});});
app.get("/api/contacts",             async (_req,res) => res.json({contacts:await listContacts()}));
app.delete("/api/contacts/:name",    async (req,res) => {await deleteContact(decodeURIComponent(req.params.name));res.json({ok:true});});
app.get("/api/reminders",            async (_req,res) => res.json({reminders:await listReminders()}));
app.delete("/api/reminders/:id",     async (req,res) => {await deleteReminder(Number(req.params.id));res.json({ok:true});});
app.get("/api/notifications",        async (_req,res) => res.json({notifications:await getUnreadNotifications()}));
app.post("/api/notifications/read",  async (_req,res) => {await markNotificationsRead();res.json({ok:true});});

// Shared chat path — used by both /api/chat (web) and Telegram bot.
async function runChatRequest({ messages, files = [], mode = "general", maxTokens = 2048, conversationId = null }) {
  if (!isClaudeEnabled() && !GEMINI_KEY) {
    throw new Error("No LLM configured. Set ANTHROPIC_API_KEY or GEMINI_API_KEY in .env");
  }
  const filtered = (messages || [])
    .filter(m => m?.role === "user" || m?.role === "assistant")
    .map(m => ({ role: m.role, content: String(m.content || "") }));
  const last = [...filtered].reverse().find(m => m.role === "user")?.content || "";
  const memCtx = await getMemoryContext(last);

  let result;
  if (isClaudeEnabled()) {
    const { base, volatile } = buildSystemParts(mode, memCtx);
    result = await claudeChat({
      baseSystem:     base,
      volatileSystem: volatile,
      tools:          TOOLS,
      messages:       filtered,
      maxTokens,
      files,
      executeTool,
    });
  } else {
    result = await agentLoop({
      system:    buildSystem(mode, memCtx),
      messages:  filtered,
      maxTokens,
      files,
    });
  }
  captureTurn({ userMessage: last, assistantReply: result?.text || "", conversationId, mode })
    .catch(e => console.warn("[memory] capture failed:", e.message));
  return result;
}

app.post("/api/chat", async (req,res) => {
  try {
    const { messages, mode = "general", max_tokens = 2048, files = [], conversation_id = null } = req.body || {};
    if (!Array.isArray(messages) || !messages.length) return res.status(400).json({ error: "messages required" });
    const result = await runChatRequest({
      messages, files, mode,
      maxTokens: Number(max_tokens) || 2048,
      conversationId: conversation_id,
    });
    res.json(result);
  } catch (e) { res.status(500).json({ error: e.message }); }
});

app.post("/api/chat/stream", async (req,res) => {
  if (!isClaudeEnabled() && !GEMINI_KEY) return res.status(500).json({error:"No LLM configured. Set ANTHROPIC_API_KEY or GEMINI_API_KEY in .env"});
  try {
    const{messages,mode="general",max_tokens=2048,files=[],conversation_id=null}=req.body||{};
    if (!Array.isArray(messages)||!messages.length) return res.status(400).json({error:"messages required"});
    const filtered=messages.filter(m=>m?.role==="user"||m?.role==="assistant").map(m=>({role:m.role,content:String(m.content||"")}));
    const last=[...filtered].reverse().find(m=>m.role==="user")?.content||"";
    res.setHeader("Content-Type","text/event-stream");
    res.setHeader("Cache-Control","no-cache");
    res.setHeader("Connection","keep-alive");
    res.write(`data: ${JSON.stringify({type:"start",mode,brain:isClaudeEnabled()?"claude":"gemini"})}\n\n`);
    const memCtx = await getMemoryContext(last);
    let toolCalls, text;
    if (isClaudeEnabled()) {
      const { base, volatile } = buildSystemParts(mode, memCtx);
      ({ toolCalls, text } = await claudeStream({
        baseSystem:    base,
        volatileSystem: volatile,
        tools:         TOOLS,
        messages:      filtered,
        maxTokens:     Number(max_tokens) || 2048,
        files,
        res,
        executeTool,
      }));
    } else {
      ({ toolCalls, text } = await streamAgentLoop({ system: buildSystem(mode, memCtx), messages: filtered, maxTokens: Number(max_tokens) || 2048, files, res }));
    }
    res.write(`data: ${JSON.stringify({type:"done",text:text||"",toolCalls})}\n\n`);
    res.end();
    captureTurn({ userMessage: last, assistantReply: text || "", conversationId: conversation_id, mode })
      .catch(e => console.warn("[memory] capture failed:", e.message));
  } catch(e){if(!res.headersSent)return res.status(500).json({error:e.message});res.write(`data: ${JSON.stringify({type:"error",message:e.message})}\n\n`);res.end();}
});

app.get("*",(_req,res)=>res.sendFile(path.join(__dirname,"..","dist","index.html")));
app.listen(port,()=>console.log(`Maahi :${port} | ${MODEL} | ${TOOLS.length} tools | Google:${!!(GOOGLE_KEY&&GOOGLE_CX)} | OAuth:${!!(GOOGLE_CLIENT_ID&&GOOGLE_CLIENT_SECRET)}`));
