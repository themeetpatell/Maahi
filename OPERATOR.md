# Maahi Operator — your autonomous Chief of Staff

> Maahi started as a voice OS for your Mac. The Operator is the 100× upgrade:
> a **Claude-powered business brain** that reaches across your entire stack —
> CRM, ads, repos, infra, docs, mail — runs your morning brief, and *acts*.
> JARVIS for the business world. Runs on your Mac **and** always-on in the cloud.

```
You:    "Maahi, what's slipping in my pipeline and any failing deploys?"
Maahi:  "Three deals past their close date in Zoho — the biggest is BiggDate
         Enterprise, 40k, no touch in 11 days. I drafted a follow-up. On ship:
         Vercel has one failed deploy on finanshels-web, build error. Want me
         to send the follow-up and re-trigger the deploy?"
```

---

## What it is

A new subsystem at `maahi/operator/` — deliberately **decoupled** from the
macOS voice stack, so it imports and runs anywhere (your Mac, a Linux box, a
container, Claude Code).

```
maahi/operator/
├── config.py          env-first config (secrets are env-only, never yaml)
├── policy.py          the act-then-report autonomy governor
├── ledger.py          append-only audit log + pending-approval queue
├── connectors/        adapters to your business stack (one file per system)
│   ├── base.py        the Connector contract + risk taxonomy
│   ├── registry.py    discovers connectors, routes calls through the policy
│   ├── zoho_crm.py  notion.py  gdrive.py  meta_ads.py  webflow.py
│   ├── github.py  vercel.py  supabase.py  cloudflare.py  gmail.py
│   └── mcp.py         generic client — reach ANY remote MCP server
├── agent.py           Claude-native tool-use loop over every capability
├── brief.py           the daily executive brief across all ventures
├── core.py            the Operator facade (chat, brief, status, approvals)
├── server.py          FastAPI command-center + cockpit
├── web/index.html     the cockpit UI
└── cli.py             `python -m maahi.operator …`
```

---

## Quick start (cloud operator, 60 seconds)

```bash
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements-operator.txt        # slim, no voice deps

cp .env.example .env                             # then fill in keys
export ANTHROPIC_API_KEY=sk-ant-...              # the only one truly required

bash run_operator.sh doctor                      # see what's configured
bash run_operator.sh                             # serve the cockpit → :7777
```

Open **http://127.0.0.1:7777** — that's your command center. Or drive it from
the terminal:

```bash
bash run_operator.sh brief                        # today's executive brief
bash run_operator.sh chat "brief me on the business"
bash run_operator.sh status                        # systems + readiness
```

Maahi runs with **zero** keys set — every system just shows "not connected" —
and lights up as you add credentials. Nothing crashes on a missing key.

---

## The brain — Claude

The Operator reasons with **Claude** (`claude-opus-4-8` by default) using
native tool use: it sees every connector capability as a tool, calls them in
sequence to finish a job, and synthesizes — it doesn't dump raw data.

The Mac **voice OS** is now Claude-powered too. In `config.yaml`:

```yaml
brain:
  powerful: claude            # the reasoning route runs on Claude
  claude_model: claude-opus-4-8
```

Set `ANTHROPIC_API_KEY` and the voice loop's "powerful" route uses Claude,
falling back to OpenAI then local Ollama if it's ever unavailable. Quick tool
calls still go to the snappy local model — you get Claude's reasoning without
losing Siri-grade latency on "open Slack".

---

## Connectors — what Maahi can reach

| System | Key | What she does | Credentials |
|---|---|---|---|
| Zoho CRM | `zoho_crm` | deals, pipeline, contacts, tasks, notes | `ZOHO_CRM_ACCESS_TOKEN` |
| Gmail | `gmail` | triage unread, search, draft, send | `GMAIL_ACCESS_TOKEN` |
| Notion | `notion` | search, read, create pages, append | `NOTION_TOKEN` |
| Google Drive | `gdrive` | search, read, create docs | `GDRIVE_ACCESS_TOKEN` |
| Meta Ads | `meta_ads` | campaigns, insights, pause, budget | `META_ACCESS_TOKEN`, `META_AD_ACCOUNT_ID` |
| Webflow | `webflow` | sites, collections, items, publish | `WEBFLOW_API_TOKEN` |
| GitHub | `github` | repos, PRs, issues, checks, merge | `GITHUB_TOKEN` |
| Vercel | `vercel` | projects, deployments, redeploy | `VERCEL_TOKEN` |
| Supabase | `supabase` | projects, health, SQL, functions | `SUPABASE_ACCESS_TOKEN` |
| Cloudflare | `cloudflare` | zones, DNS, purge cache | `CLOUDFLARE_API_TOKEN` |
| Any MCP server | `mcp` | call tools on remote MCP endpoints | `MAAHI_MCP_SERVERS` |

Full env reference is in [`.env.example`](.env.example). Run
`bash run_operator.sh doctor` any time to see exactly what's wired and what
each missing system needs.

---

## Autonomy — "act then report"

Every capability is tagged with a **risk**. The policy maps risk × mode to
*do it* or *ask first*:

| risk | example | `suggest` | `act_report` (default) | `autopilot` |
|---|---|---|---|---|
| read | list deals, ad metrics | ✅ do | ✅ do | ✅ do |
| write | draft email, log a CRM task | ask | ✅ do | ✅ do |
| publish | publish a page, merge a PR | ask | **ask** | ✅ do |
| send | send an email | ask | **ask** | ✅ do |
| spend | raise an ad budget | ask | **ask** | ✅ do |
| delete | destructive | ask | **ask** | ✅ do |

In `act_report`, reversible work just happens and Maahi tells you in one line.
Anything outbound, costly, or destructive is **parked** in the approval queue
(visible in the cockpit and `run_operator.sh pending`) until you say yes.
Every action — done or proposed — is written to an append-only **ledger**.

Switch modes live from the cockpit, or:

```bash
curl -XPOST localhost:7777/api/autonomy -d '{"mode":"autopilot"}'
```

---

## The command center (cockpit)

A single-page HUD at `/`:

- **Chat** — talk to Maahi; responses stream, tool calls show inline, approvals
  appear as cards you accept/reject in place.
- **Brief** — the executive narrative + a live pulse tile per system.
- **Systems** — what's connected, what's not, what each missing one needs.
- **Approvals** — the parked-action queue.
- **Activity** — the live ledger feed.

### API (same origin; bearer-auth if `MAAHI_OPERATOR_TOKEN` is set)

| Method | Path | Purpose |
|---|---|---|
| GET | `/api/status` | systems, autonomy, brain state |
| GET | `/api/brief` | the executive brief (`?synthesize=0` to skip Claude) |
| POST | `/api/chat` | stream a turn (SSE) |
| GET | `/api/pending` | parked actions |
| POST | `/api/approve` `{id}` | run a parked action |
| POST | `/api/reject` `{id}` | drop a parked action |
| GET | `/api/ledger` | recent audit entries |
| POST | `/api/autonomy` `{mode}` | set the autonomy mode |

---

## Voice ↔ business

The voice OS gained two tools, so you can run the business by voice:

- **`business_brief`** — "Maahi, brief me on the business."
- **`business_ask`** — "What's slipping in my pipeline?" / "Pause the worst ad."

Both run the full Operator agent and speak back a tight answer. Risky moves are
parked for approval exactly as they are in the cockpit.

---

## Deploy always-on

```bash
docker build -t maahi-operator .
docker run --env-file .env -p 7777:7777 -v maahi_state:/data maahi-operator
```

The image is the slim operator only (no voice stack). Point a domain at it,
put it behind auth (`MAAHI_OPERATOR_TOKEN`), and Maahi runs your business 24/7.

---

## Security

- **Secrets are env-only.** No credential is ever read from or written to
  `config.yaml`. `.env` is gitignored.
- **Least privilege.** Give each token the narrowest scope that works.
- **Audit everything.** The ledger records every read, write, and proposal.
- **The policy is the rail.** Outbound/spend/delete never fire without your yes
  in the default mode. Flip to `autopilot` only when you mean it.
- Param values are redacted in the audit detail for secret-looking keys.

---

## Extend it — add a connector in ~40 lines

Create `maahi/operator/connectors/yourthing.py`:

```python
from __future__ import annotations
import httpx
from .base import Capability, Connector, ConnectorResult

class YourThingConnector(Connector):
    key = "yourthing"; label = "YourThing"
    required_env = ("YOURTHING_TOKEN",)
    blurb = "Get a token at https://yourthing.com/tokens"

    def _client(self) -> httpx.Client:
        return httpx.Client(base_url="https://api.yourthing.com",
            headers={"Authorization": f"Bearer {self.env('YOURTHING_TOKEN')}"},
            timeout=httpx.Timeout(20.0, connect=8.0))

    def capabilities(self):
        return (Capability("list_things", "List things.",
                           {"limit": "int: max"}, "read"),)

    def op_list_things(self, limit: int = 20) -> ConnectorResult:
        try:
            with self._client() as c:
                r = c.get("/things", params={"limit": limit}); r.raise_for_status()
        except httpx.HTTPError as e:
            return ConnectorResult.fail(f"failed: {e}")
        return ConnectorResult.success(f"{len(r.json())} things", data=r.json())
```

Add `("yourthing", "YourThingConnector")` to the roster in
`connectors/registry.py`. Done — it shows up in the brief, the cockpit, the
agent's toolset, and the autonomy policy automatically.

---

Built for Meet. Maahi runs the empire now.
