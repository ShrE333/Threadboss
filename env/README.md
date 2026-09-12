# ThreadBoss V1.6 — Stable Memory + Agents

V1.6 keeps the V1.4 native WhatsApp menu and the V1.5 agent/memory stack, then fixes history backfill and temporal retrieval so questions like “what important messages did I receive yesterday?” work from the exact calendar window instead of depending on a rolling 24-hour cache.

## Main fixes

- `hi` / `menu` -> native WhatsApp List, Poll fallback, then text fallback.
- Exact temporal auto-sync for `yesterday`, `today`, `this week`, and `last N days`.
- `/sync yesterday`, `/sync 24h`, `/sync 7d`.
- History pagination follows WAHA's documented offset behavior even when a filtered page is shorter than `limit`.
- GOWS history outgoing messages recover the chat JID from the full WAHA message id when `chatId`/`to` are absent.
- Backfill can repair existing incomplete DB rows instead of `ON CONFLICT DO NOTHING` blocking better text/embeddings/timestamps.
- Sync diagnostics split `Inserted`, `Repaired`, `Already indexed`, `Skipped self-chat`, `Empty/system`, `Media extracted`, and `Failed rows`.
- `/memory stats` shows total local memory, text coverage, embedding coverage, and earliest/latest timestamps.
- Free default generation model: `gemini-3.5-flash-lite`; embeddings remain `gemini-embedding-001`.
- WAHA network retries and worker retry/dead-letter behavior from V1.5 are preserved.
- OCR/PDF/QR/resize/merge/compress/transcribe tools are preserved.
- Planner, Follow-up, Action safe-planning, Knowledge RAG, Slack/Telegram `/v1/messages` + `/v1/query` APIs are preserved.

## Recommended EasyPanel additions/changes

```env
KNOWLEDGE_MODEL=gemini-3.5-flash-lite
PLANNER_MODEL=gemini-3.5-flash-lite
ACTION_MODEL=gemini-3.5-flash-lite
VISION_MODEL=gemini-3.5-flash-lite
GEMINI_FALLBACK_MODEL=gemini-3.1-flash-lite
GEMINI_MODEL_FALLBACKS_CSV=gemini-3.5-flash-lite,gemini-3.1-flash-lite

EMBEDDING_MODEL=gemini-embedding-001
EMBEDDING_DIM=768

DEFAULT_TIMEZONE=Asia/Kolkata
AUTO_SYNC_TEMPORAL_QUERIES=true
TEMPORAL_SYNC_CACHE_SECONDS=900
INITIAL_BACKFILL_HOURS=48
INITIAL_BACKFILL_MAX_MESSAGES=3000
HISTORY_SYNC_PAGE_SIZE=100
```

Keep all existing WAHA, Redis, Postgres, Gemini API key, admin token and channel API key values.

## Upgrade

Copy V1.6 over the existing repo, but do not replace your real `.env` or delete persistent Postgres/Redis volumes.

```powershell
git add .
git commit -m "Upgrade ThreadBoss to V1.6 stable memory"
git push
```

Deploy ThreadBoss only, then bootstrap the existing WAHA session once so the webhook remains current.

## Test sequence

In Message Yourself:

```text
/status
/memory stats
/sync yesterday
```

Then ask:

```text
What important messages did I receive yesterday?
```

V1.6 will auto-sync that exact calendar window before querying it.

## Validation performed

- Python compilation check passed for all application modules.
- 22 targeted unit tests passed using dependency stubs for unavailable local `asyncpg`/`redis` packages, covering existing identity/menu/media/tool tests plus V1.6 history-JID recovery, string boolean normalization, temporal-window parsing and short-page history pagination.
- Live WAHA/Gemini integration still depends on the user's deployed credentials/network and must be smoke-tested after deployment.
