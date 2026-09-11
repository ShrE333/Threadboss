# ThreadBoss V1.3 — Agents Core

V1.3 removes the teammate/mock Knowledge Agent. The real agent backend now lives inside ThreadBoss and is shared by **WhatsApp, Slack, and Telegram**.

## Architecture

```text
WhatsApp / WAHA ─┐
Slack adapter ────┼──> ThreadBoss Agent Backend
Telegram adapter ─┘          │
                              ├── Knowledge Agent (RAG)
                              ├── Planner Agent (task extraction)
                              ├── Follow-up Agent
                              ├── Action Agent (safe planning mode)
                              └── Tool Engine
                                      │
                       ┌──────────────┼───────────────┐
                       │              │               │
                    pgvector        Redis        Local media tools
                   Postgres                      Whisper/Tesseract
```

## What is new

- **Real Knowledge Agent**: stores messages in Postgres/pgvector, creates embeddings, retrieves relevant messages, and generates grounded answers.
- **Planner Agent**: looks at action-like incoming messages and extracts tasks/commitments.
- **Follow-up Agent**: surfaces overdue/upcoming/undated commitments.
- **Action Agent**: included in confirmation-first planning mode. V1.3 deliberately does not silently send/pay/book anything.
- **Shared channel API**: Slack and Telegram adapters use the exact same agent backend via `/v1/messages` and `/v1/query`.
- **V1.2 tools preserved**: OCR, local Whisper STT, image-to-PDF, merge/compress PDF, resize image, QR generation.
- **GOWS @lid self-chat fix preserved**.

## Free AI configuration

V1.3 requires **no paid OpenAI/Anthropic API**. Defaults are chosen from services that offer free allocations/tiers. Free quotas are not unlimited and providers can change them.

| Capability | Default | Cost approach |
|---|---|---|
| Knowledge LLM | Gemini 2.5 Flash-Lite | Google AI Studio free tier |
| Planner LLM | Gemini 2.5 Flash-Lite | Google AI Studio free tier |
| Embeddings | Gemini Embedding 001, 768 dims | Google AI Studio free tier |
| Router (optional) | Cloudflare GLM-4.7-Flash | Workers AI free allocation |
| Groq fallback option | Qwen / GPT-OSS family | Groq Free plan |
| OCR | Tesseract | runs locally |
| STT | faster-whisper base | runs locally |
| PDF/image tools | OSS libraries | runs locally |
| Vector DB | pgvector/Postgres | runs on your current GCP VM |

For the default configuration you only need a **free `GEMINI_API_KEY`**. Cloudflare and Groq credentials are optional.

> Privacy note: free hosted AI tiers send the selected prompt/evidence to the provider. If you later want chat content to remain entirely on your infrastructure, switch the provider layer to a local model.

## Environment files

- `.env.example` — local/dev template.
- `.env.easypanel.example` — paste into **EasyPanel → threadboss → Environment** and enable **Create .env file**.
- `env/slack-telegram-adapter.env.example` — for the separate Slack/Telegram adapter. The adapter does **not** need Gemini/Cloudflare/Groq keys.

Generate secrets in PowerShell:

```powershell
[guid]::NewGuid().ToString("N")
```

Use separate values for `ADMIN_TOKEN`, `CHANNEL_API_KEY`, and `WAHA_WEBHOOK_HMAC_KEY`.

## Upgrade from V1.2 on EasyPanel

1. Back up your currently working repo.
2. Replace the repo files with V1.3 and push to GitHub.
3. In **threadboss → Environment**, merge the variables from `.env.easypanel.example` with your working WAHA values.
4. Set `GEMINI_API_KEY` using a free Google AI Studio key.
5. Keep **Create .env file = ON**.
6. Deploy **ThreadBoss only**. Do not redeploy WAHA.
7. V1.3 adds a `postgres` container automatically.
8. If EasyPanel leaves the Compose service stopped after deployment, click **Start**.

Verify:

```powershell
$TB="https://YOUR-THREADBOSS-DOMAIN"
Invoke-RestMethod "$TB/health" | ConvertTo-Json
Invoke-RestMethod "$TB/ready" | ConvertTo-Json
```

`/ready` should report both Redis and database as ready.

Your existing WAHA webhook URL is unchanged. You normally do not need to bootstrap again, but it is safe to do so after the upgrade:

```powershell
Invoke-RestMethod `
  -Method POST `
  -Uri "$TB/admin/sessions/YOUR_WAHA_SESSION/bootstrap?configure_webhook=true" `
  -Headers @{"X-ThreadBoss-Admin"=$ADMIN} | ConvertTo-Json -Depth 10
```

## WhatsApp tests

In **Message Yourself**:

```text
/status
/agents
/tasks
/followups
/memory what did professor say about the review?
```

Normal DMs/groups are silent data-plane inputs. ThreadBoss will not reply into them.

Try a normal conversation containing something actionable, for example:

```text
Please submit the report by Friday.
```

After it is ingested, use Message Yourself:

```text
/tasks
```

The Planner Agent only invokes the LLM when a message looks actionable, which reduces free-tier quota usage.

## API for Slack / Telegram

The other channel adapters should contain **no agents**. They only normalize their platform events and call ThreadBoss.

Authenticate with either:

```text
X-ThreadBoss-Key: <CHANNEL_API_KEY>
```

or:

```text
Authorization: Bearer <CHANNEL_API_KEY>
```

### Ingest a message

`POST /v1/messages`

```json
{
  "tenant_id": "tenant_shriram",
  "channel": "slack",
  "session_id": "workspace_T123",
  "chat_id": "C123",
  "chat_type": "channel",
  "sender_id": "U123",
  "message_id": "1712345.100",
  "timestamp": "2026-09-11T14:00:00+05:30",
  "text": "Submit the deck by Friday",
  "from_me": false
}
```

### Ask the agent team

`POST /v1/query`

```json
{
  "tenant_id": "tenant_shriram",
  "channel": "telegram",
  "session_id": "telegram-main",
  "chat_id": "123456",
  "question": "What deadlines do I have?"
}
```

Response:

```json
{
  "answer": "...",
  "agent": "planner",
  "sources": []
}
```

The same `tenant_id` can therefore aggregate a user's WhatsApp + Slack + Telegram memory if that is the behavior you want. Use different tenant IDs when data must remain isolated.

## Agent routing

To save quota, AI routing is OFF by default:

```env
ENABLE_AI_ROUTER=false
```

Deterministic rules handle `/tasks`, `/followups`, `/action`, `/memory`; everything else defaults to the Knowledge Agent. You can enable Cloudflare-powered routing later.

## V1.3 boundaries

- Action Agent is **planning-only** until we implement explicit confirmation/execution.
- Follow-up Agent can report follow-ups; scheduled proactive notifications come in the next scheduler version.
- No automatic history backfill yet.
- Free hosted API quotas can return 429/403 when exhausted; ThreadBoss keeps raw messages even when an embedding/model call temporarily fails.
