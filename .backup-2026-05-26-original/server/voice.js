// Voice surfaces:
// - STT: OpenAI Whisper (cheap, accurate, handles Telegram .ogg natively)
// - TTS: ElevenLabs (default voice "Rachel"; configurable via env)

const WHISPER_URL    = "https://api.openai.com/v1/audio/transcriptions";
const ELEVENLABS_URL = "https://api.elevenlabs.io/v1/text-to-speech";

export function isSTTEnabled() {
  return Boolean(process.env.OPENAI_API_KEY);
}

export function isTTSEnabled() {
  return Boolean(process.env.ELEVENLABS_API_KEY);
}

/**
 * Transcribe audio bytes → text via OpenAI Whisper.
 * @param {Buffer} buffer
 * @param {string} mimeType  e.g. "audio/ogg", "audio/mpeg"
 * @returns {Promise<string>}
 */
export async function transcribe(buffer, mimeType = "audio/ogg") {
  if (!isSTTEnabled()) throw new Error("OPENAI_API_KEY not set");
  const ext = (mimeType.split("/")[1] || "ogg").split(";")[0];
  const fd = new FormData();
  fd.append("file", new Blob([buffer], { type: mimeType }), `voice.${ext}`);
  fd.append("model", "whisper-1");
  fd.append("response_format", "text");
  const r = await fetch(WHISPER_URL, {
    method:  "POST",
    headers: { "Authorization": `Bearer ${process.env.OPENAI_API_KEY}` },
    body:    fd,
  });
  if (!r.ok) {
    const detail = await r.text().catch(() => "");
    throw new Error(`Whisper ${r.status}: ${detail.slice(0, 200)}`);
  }
  return (await r.text()).trim();
}

/**
 * Synthesize text → mp3 audio bytes via ElevenLabs.
 * @param {string} text
 * @returns {Promise<Buffer>} mp3 buffer (audio/mpeg)
 */
export async function synthesize(text) {
  if (!isTTSEnabled()) throw new Error("ELEVENLABS_API_KEY not set");
  const voiceId = process.env.ELEVENLABS_VOICE_ID || "21m00Tcm4TlvDq8ikWAM";
  const model   = process.env.ELEVENLABS_MODEL    || "eleven_turbo_v2_5";
  const safe    = String(text || "").slice(0, 5000);
  if (!safe.trim()) throw new Error("synthesize: empty text");

  const r = await fetch(`${ELEVENLABS_URL}/${voiceId}`, {
    method:  "POST",
    headers: {
      "xi-api-key":   process.env.ELEVENLABS_API_KEY,
      "Content-Type": "application/json",
      "Accept":       "audio/mpeg",
    },
    body: JSON.stringify({
      text:           safe,
      model_id:       model,
      output_format:  "mp3_44100_128",
      voice_settings: { stability: 0.5, similarity_boost: 0.75, style: 0.0, use_speaker_boost: true },
    }),
  });
  if (!r.ok) {
    const detail = await r.text().catch(() => "");
    throw new Error(`ElevenLabs ${r.status}: ${detail.slice(0, 200)}`);
  }
  return Buffer.from(await r.arrayBuffer());
}
