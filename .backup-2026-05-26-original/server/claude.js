// Claude (Anthropic) client — primary brain for Maahi.
// - Sonnet 4.6 for chat (with adaptive thinking)
// - Haiku 4.5 for cheap extraction (memory/capture.js)
// - Prompt caching on the frozen BASE_SYSTEM + tool definitions

import Anthropic from "@anthropic-ai/sdk";

const CLAUDE_MODEL       = process.env.CLAUDE_MODEL       || "claude-sonnet-4-6";
const CLAUDE_HAIKU_MODEL = process.env.CLAUDE_HAIKU_MODEL || "claude-haiku-4-5";

let _client = null;
function client() {
  if (!_client) _client = new Anthropic();
  return _client;
}

export function isClaudeEnabled() {
  return Boolean(process.env.ANTHROPIC_API_KEY);
}

export function activeClaudeModel() {
  return CLAUDE_MODEL;
}

export function activeHaikuModel() {
  return CLAUDE_HAIKU_MODEL;
}

// ─── Conversion helpers ─────────────────────────────────────────
function toClaudeTools(tools) {
  return tools.map(t => ({
    name: t.name,
    description: t.description,
    input_schema: t.input_schema,
  }));
}

function buildClaudeMessages(messages, files = []) {
  return messages.map((m, i) => {
    const isLastUser = m.role === "user" && i === messages.length - 1;
    if (!isLastUser || !files.length) {
      return { role: m.role, content: String(m.content ?? "") };
    }
    const blocks = [];
    for (const f of files) {
      if (f.mimeType?.startsWith("image/") && f.data) {
        blocks.push({
          type: "image",
          source: { type: "base64", media_type: f.mimeType, data: f.data },
        });
      }
    }
    blocks.push({ type: "text", text: String(m.content ?? "") });
    return { role: "user", content: blocks };
  });
}

function buildClaudeSystem(baseSystem, volatileSystem) {
  const blocks = [];
  if (baseSystem)     blocks.push({ type: "text", text: baseSystem,     cache_control: { type: "ephemeral" } });
  if (volatileSystem) blocks.push({ type: "text", text: volatileSystem });
  return blocks;
}

function stringifyToolResult(result) {
  if (typeof result === "string") return result;
  try { return JSON.stringify(result); }
  catch { return String(result); }
}

// ─── Non-streaming agent loop ───────────────────────────────────
export async function claudeChat({
  baseSystem,
  volatileSystem,
  tools,
  messages,
  maxTokens = 4096,
  files = [],
  maxIter = 8,
  executeTool,
}) {
  if (!isClaudeEnabled()) throw new Error("ANTHROPIC_API_KEY not set");
  if (typeof executeTool !== "function") throw new Error("claudeChat requires executeTool function");

  const c = client();
  const claudeTools = toClaudeTools(tools);
  let convo = buildClaudeMessages(messages, files);
  const allToolCalls = [];
  let finalText = "";

  for (let iter = 0; iter < maxIter; iter++) {
    let response;
    try {
      response = await c.messages.create({
        model:       CLAUDE_MODEL,
        max_tokens:  maxTokens,
        system:      buildClaudeSystem(baseSystem, volatileSystem),
        tools:       claudeTools,
        messages:    convo,
        thinking:    { type: "adaptive" },
      });
    } catch (e) {
      return { text: `Claude error: ${e.message}`, toolCalls: allToolCalls };
    }

    finalText = response.content
      .filter(b => b.type === "text")
      .map(b => b.text)
      .join("\n")
      .trim();

    if (response.stop_reason !== "tool_use") {
      return { text: finalText, toolCalls: allToolCalls };
    }

    const toolUses = response.content.filter(b => b.type === "tool_use");
    const toolResults = [];
    for (const tu of toolUses) {
      let result;
      try { result = await executeTool(tu.name, tu.input); }
      catch (e) { result = `Tool error: ${e.message}`; }
      allToolCalls.push({ name: tu.name, input: tu.input, result });
      const content = typeof result === "string" && result.startsWith("IMAGE:")
        ? "Image generated."
        : stringifyToolResult(result);
      toolResults.push({ type: "tool_result", tool_use_id: tu.id, content });
    }

    convo = [
      ...convo,
      { role: "assistant", content: response.content },
      { role: "user",      content: toolResults },
    ];
  }

  return { text: finalText || "Hit max iterations.", toolCalls: allToolCalls };
}

// ─── Streaming agent loop — writes SSE to res ───────────────────
export async function claudeStream({
  baseSystem,
  volatileSystem,
  tools,
  messages,
  maxTokens = 4096,
  files = [],
  maxIter = 8,
  res,
  executeTool,
}) {
  if (!isClaudeEnabled()) {
    res.write(`data: ${JSON.stringify({ type: "error", message: "ANTHROPIC_API_KEY not set" })}\n\n`);
    return { toolCalls: [], text: "" };
  }
  if (typeof executeTool !== "function") {
    res.write(`data: ${JSON.stringify({ type: "error", message: "claudeStream requires executeTool" })}\n\n`);
    return { toolCalls: [], text: "" };
  }

  const c = client();
  const claudeTools = toClaudeTools(tools);
  let convo = buildClaudeMessages(messages, files);
  const allToolCalls = [];
  let finalText = "";

  for (let iter = 0; iter < maxIter; iter++) {
    let message;
    let iterationText = "";
    try {
      const stream = c.messages.stream({
        model:      CLAUDE_MODEL,
        max_tokens: maxTokens,
        system:     buildClaudeSystem(baseSystem, volatileSystem),
        tools:      claudeTools,
        messages:   convo,
        thinking:   { type: "adaptive" },
      });

      stream.on("text", (delta) => {
        iterationText += delta;
        res.write(`data: ${JSON.stringify({ type: "token", text: delta })}\n\n`);
      });

      message = await stream.finalMessage();
      finalText = iterationText;
    } catch (e) {
      res.write(`data: ${JSON.stringify({ type: "error", message: e.message })}\n\n`);
      return { toolCalls: allToolCalls, text: finalText };
    }

    if (message.stop_reason !== "tool_use") {
      return { text: finalText, toolCalls: allToolCalls };
    }

    const toolUses = message.content.filter(b => b.type === "tool_use");
    const toolResults = [];

    for (const tu of toolUses) {
      res.write(`data: ${JSON.stringify({ type: "tool_call", name: tu.name, input: tu.input })}\n\n`);
      let result;
      try { result = await executeTool(tu.name, tu.input); }
      catch (e) { result = `Tool error: ${e.message}`; }
      allToolCalls.push({ name: tu.name, input: tu.input, result });

      if (typeof result === "string" && result.startsWith("IMAGE:")) {
        const [, mimeType, b64] = result.split(":");
        res.write(`data: ${JSON.stringify({ type: "image", mimeType, data: b64 })}\n\n`);
        toolResults.push({ type: "tool_result", tool_use_id: tu.id, content: "Image generated." });
      } else {
        const resultStr = stringifyToolResult(result);
        res.write(`data: ${JSON.stringify({ type: "tool_result", name: tu.name, result: resultStr })}\n\n`);
        toolResults.push({ type: "tool_result", tool_use_id: tu.id, content: resultStr });
      }
    }

    convo = [
      ...convo,
      { role: "assistant", content: message.content },
      { role: "user",      content: toolResults },
    ];
  }

  return { text: finalText || "Hit max iterations.", toolCalls: allToolCalls };
}

// ─── Haiku 4.5: cheap structured extraction for memory/capture ──
const EXTRACTION_SCHEMA = {
  type: "object",
  additionalProperties: false,
  required: ["facts", "people"],
  properties: {
    facts: {
      type: "array",
      items: {
        type: "object",
        additionalProperties: false,
        required: ["key", "value", "category"],
        properties: {
          key:      { type: "string" },
          value:    { type: "string" },
          category: {
            type: "string",
            enum: ["personal", "business", "preferences", "goals", "people", "decisions", "general"],
          },
        },
      },
    },
    people: {
      type: "array",
      items: {
        type: "object",
        additionalProperties: false,
        required: ["name", "interaction_summary", "company", "role", "relationship"],
        properties: {
          name:                { type: "string" },
          company:             { anyOf: [{ type: "string" }, { type: "null" }] },
          role:                { anyOf: [{ type: "string" }, { type: "null" }] },
          relationship:        { anyOf: [{ type: "string" }, { type: "null" }] },
          interaction_summary: { type: "string" },
        },
      },
    },
  },
};

// Generic Haiku judge — system + prompt + JSON schema → parsed object (or null).
export async function judgeWithHaiku({ system, prompt, schema, maxTokens = 1024 }) {
  if (!isClaudeEnabled()) return null;
  const c = client();
  try {
    const response = await c.messages.create({
      model:      CLAUDE_HAIKU_MODEL,
      max_tokens: maxTokens,
      system,
      messages:   [{ role: "user", content: prompt }],
      output_config: { format: { type: "json_schema", schema } },
    });
    const text = response.content.filter(b => b.type === "text").map(b => b.text).join("");
    return JSON.parse(text);
  } catch (e) {
    console.warn("[claude] judgeWithHaiku failed:", e.message);
    return null;
  }
}

// Sonnet 4.6 structured output — for higher-quality multi-step reasoning over JSON.
export async function sonnetJSON({ system, prompt, schema, maxTokens = 2048 }) {
  if (!isClaudeEnabled()) return null;
  const c = client();
  try {
    const response = await c.messages.create({
      model:      CLAUDE_MODEL,
      max_tokens: maxTokens,
      system,
      messages:   [{ role: "user", content: prompt }],
      thinking:   { type: "adaptive" },
      output_config: { format: { type: "json_schema", schema } },
    });
    const text = response.content.filter(b => b.type === "text").map(b => b.text).join("");
    return JSON.parse(text);
  } catch (e) {
    console.warn("[claude] sonnetJSON failed:", e.message);
    return null;
  }
}

// Sonnet 4.6 plain text — for prose generation (calendar prep, daily debrief).
export async function sonnetText({ system, prompt, maxTokens = 1024 }) {
  if (!isClaudeEnabled()) return null;
  const c = client();
  try {
    const response = await c.messages.create({
      model:      CLAUDE_MODEL,
      max_tokens: maxTokens,
      system,
      messages:   [{ role: "user", content: prompt }],
      thinking:   { type: "adaptive" },
    });
    return response.content.filter(b => b.type === "text").map(b => b.text).join("").trim();
  } catch (e) {
    console.warn("[claude] sonnetText failed:", e.message);
    return null;
  }
}

export async function extractWithHaiku({ userMessage, assistantReply }) {
  if (!isClaudeEnabled()) return null;
  const c = client();
  const system = `You are a memory-extraction subroutine for Meet Patel's personal AI "Maahi".
From an exchange, extract ONLY information that is new, durable, and worth remembering long-term.

STRICT RULES:
- Skip greetings, small-talk, status checks, anything trivially obvious.
- Skip facts already implied by Meet's static profile (he's in Dubai, runs Finanshels, building Soulmap).
- A "fact" must be specific and durable: preferences, decisions, goals, dates, names, numbers, relationships.
- A "person" is someone mentioned by name OR referred to specifically. Skip vague references ("a friend").
- Be conservative. When in doubt, output empty arrays.`;

  const prompt = `EXCHANGE:
USER: ${String(userMessage || "").slice(0, 4000)}

A: ${String(assistantReply || "").slice(0, 4000) || "(no reply)"}

Return only the JSON object matching the schema. If nothing durable to remember, return {"facts":[],"people":[]}.`;

  try {
    const response = await c.messages.create({
      model:      CLAUDE_HAIKU_MODEL,
      max_tokens: 1024,
      system,
      messages:   [{ role: "user", content: prompt }],
      output_config: {
        format: { type: "json_schema", schema: EXTRACTION_SCHEMA },
      },
    });
    const text = response.content
      .filter(b => b.type === "text")
      .map(b => b.text)
      .join("");
    return JSON.parse(text);
  } catch (e) {
    console.warn("[claude] extractWithHaiku failed:", e.message);
    return null;
  }
}
