// Voyage AI embedding client. Pairs with sqlite-vec for storage.
// Docs: https://docs.voyageai.com/reference/embeddings-api

const VOYAGE_URL    = "https://api.voyageai.com/v1/embeddings";
const BATCH_SIZE    = 128;        // Voyage hard limit per request
const VECTOR_DIM    = 1024;       // voyage-3-large default
const MAX_RETRIES   = 3;

function key()   { return process.env.VOYAGE_API_KEY; }
function model() { return process.env.VOYAGE_MODEL || "voyage-3-large"; }

export function isEmbeddingsEnabled() {
  return Boolean(key());
}

export async function embed(texts, inputType = "document") {
  if (!isEmbeddingsEnabled()) {
    throw new Error("VOYAGE_API_KEY missing. Get one at https://dash.voyageai.com and add to .env");
  }
  const wasSingle = !Array.isArray(texts);
  const inputs = wasSingle ? [texts] : texts;
  if (!inputs.length) return wasSingle ? null : [];

  const cleaned = inputs.map(t => String(t ?? "").trim().slice(0, 32000) || " ");
  const out = [];
  for (const batch of chunk(cleaned, BATCH_SIZE)) {
    const vectors = await callVoyage(batch, inputType);
    out.push(...vectors);
  }
  return wasSingle ? out[0] : out;
}

async function callVoyage(inputs, inputType) {
  let lastErr;
  for (let attempt = 0; attempt < MAX_RETRIES; attempt++) {
    try {
      const r = await fetch(VOYAGE_URL, {
        method: "POST",
        headers: {
          "Content-Type":  "application/json",
          "Authorization": `Bearer ${key()}`,
        },
        body: JSON.stringify({
          input:            inputs,
          model:            model(),
          input_type:       inputType,
          output_dimension: VECTOR_DIM,
          output_dtype:     "float",
          truncation:       true,
        }),
      });
      if (r.status === 429 || r.status >= 500) {
        lastErr = new Error(`Voyage ${r.status}: ${await r.text().catch(()=>"" )}`);
        await sleep(500 * Math.pow(2, attempt));
        continue;
      }
      if (!r.ok) {
        throw new Error(`Voyage ${r.status}: ${await r.text().catch(()=>"" )}`);
      }
      const data = await r.json();
      return data.data.map(d => d.embedding);
    } catch (e) {
      lastErr = e;
      await sleep(500 * Math.pow(2, attempt));
    }
  }
  throw lastErr ?? new Error("Voyage embed failed");
}

export function toBlob(vector) {
  const f32 = new Float32Array(vector);
  return Buffer.from(f32.buffer, f32.byteOffset, f32.byteLength);
}

function chunk(arr, size) {
  const out = [];
  for (let i = 0; i < arr.length; i += size) out.push(arr.slice(i, i + size));
  return out;
}

function sleep(ms) { return new Promise(r => setTimeout(r, ms)); }
