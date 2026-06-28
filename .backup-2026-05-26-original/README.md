# Maahi Super App (Claude-Powered)

A React + Express AI assistant app inspired by your Maahi concept, wired securely to Claude.

## Features

- Claude-backed assistant chat via server proxy (`/api/chat`)
- Streaming chat endpoint for token-by-token UX (`/api/chat/stream`)
- Multi-agent modes: `general`, `ops`, `sales`, `soulmap`
- Retrieval-based memory context from persisted memory store
- Tool layer with practical actions:
	- web research (`DuckDuckGo` instant answer)
	- task add/list (local task store)
	- docs lookup (project `README.md`)
- Voice input (SpeechRecognition in Chromium)
- Voice output (SpeechSynthesis)
- Local memory extraction from `<MAAHI_MEMORY>...</MAAHI_MEMORY>` tags
- Memory panel with clear/reset

## API Endpoints

- `GET /api/health` -> service health + active model
- `GET /api/memory` -> persisted memory facts
- `POST /api/chat` -> non-streaming chat completion
- `POST /api/chat/stream` -> SSE streaming chat completion

## Frontend Controls

- `Agent` selector for behavior specialization
- `Stream` toggle for realtime token streaming
- `Tools On/Off` toggle to enable or disable tool layer

## Quick Start

1. Install dependencies:

```bash
npm install
```

2. Create env file:

```bash
cp .env.example .env
```

3. Add your Anthropic API key in `.env`.

4. Run app (frontend + backend together):

```bash
npm run dev
```

- Web app: `http://localhost:5173`
- API: `http://localhost:8787/api/health`

## Scripts

- `npm run dev` -> starts Vite + Express in parallel
- `npm run build` -> production web build
- `npm run preview` -> preview built frontend
- `npm run start` -> run backend only

## Architecture

- Frontend: React in `src/App.jsx`
- API server: Express in `server/index.js`
- Persisted data: `data/memories.json`, `data/tasks.json`
- Vite proxy routes `/api/*` to backend during dev

## Security

Do not put Anthropic API keys in frontend code. Keep keys in `.env` and use server proxy only.
