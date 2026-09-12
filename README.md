# ThreadBoss V1.5 — Reliable Memory

V1.5 fixes the two production issues seen in V1.4 and makes the Knowledge Agent useful for time-based questions and media-heavy WhatsApp chats.

## What V1.5 fixes

1. **Gemini generation 404s**
   - Defaults move from `gemini-2.5-flash-lite` to `gemini-3.1-flash-lite`.
   - If an old EasyPanel env still points at a 2.5 model and Google returns 404, the AI client automatically retries with `GEMINI_FALLBACK_MODEL`.
   - `gemini-embedding-001` remains unchanged.

2. **Transient WAHA DNS failures**
   - WAHA requests retry with exponential backoff for connection/DNS/timeouts and 502/503/504 responses.
   - Worker event dedupe claims are released on processing failure, so a retry actually retries instead of being discarded as a duplicate.

3. **Weak RAG fallback**
   - If text generation is temporarily unavailable, ThreadBoss now returns several real evidence snippets with sender and timestamp instead of a blank `I found this relevant message` response.

## New memory behavior

- `yesterday`, `today`, `this week`, and `last N days` are interpreted as real time ranges using `DEFAULT_TIMEZONE`.
- Questions such as `What messages did I receive yesterday that are important?` search the full time window, not just semantic top-K.
- `received` / `incoming` questions exclude messages sent by the owner.
- Normal-chat images and documents are indexed locally using OCR/text extraction by default.
- Normal-chat audio transcription stays opt-in because Whisper is CPU-heavy.
- If the user selected **Ask Memory** and refers to a recent self-chat image/poster/file, ThreadBoss can use the recent attachment as context.

## History backfill

WAHA GOWS supports fetching messages across all chats with `chatId=all`.

V1.5 adds:

```text
/sync 24h
/sync 7d
```

On the first successful session bootstrap, ThreadBoss also schedules a one-time backfill using:

```env
INITIAL_BACKFILL_HOURS=24
INITIAL_BACKFILL_MAX_MESSAGES=1500
```

This prevents a newly connected ThreadBoss account from starting with an empty memory.

## Important EasyPanel model settings

Use:

```env
KNOWLEDGE_PROVIDER=gemini
KNOWLEDGE_MODEL=gemini-3.1-flash-lite

PLANNER_PROVIDER=gemini
PLANNER_MODEL=gemini-3.1-flash-lite

ACTION_PROVIDER=gemini
ACTION_MODEL=gemini-3.1-flash-lite

VISION_PROVIDER=gemini
VISION_MODEL=gemini-3.1-flash-lite

GEMINI_FALLBACK_MODEL=gemini-3.1-flash-lite

EMBEDDING_PROVIDER=gemini
EMBEDDING_MODEL=gemini-embedding-001
EMBEDDING_DIM=768
```

Additional V1.5 settings:

```env
DEFAULT_TIMEZONE=Asia/Kolkata
KNOWLEDGE_SUMMARY_LIMIT=120

INITIAL_BACKFILL_HOURS=24
INITIAL_BACKFILL_MAX_MESSAGES=1500
HISTORY_SYNC_PAGE_SIZE=100

INDEX_NORMAL_CHAT_IMAGES=true
INDEX_NORMAL_CHAT_DOCUMENTS=true
INDEX_NORMAL_CHAT_AUDIO=false

WAHA_REQUEST_RETRIES=4
WAHA_RETRY_BASE_SECONDS=0.75
```

## Upgrade from V1.4

Copy/replace the V1.5 project files over the V1.4 repo. Keep the existing real EasyPanel secrets and persistent Postgres/Redis volumes.

Then:

```powershell
git add .
git commit -m "Upgrade ThreadBoss to V1.5 reliable memory"
git push
```

Redeploy ThreadBoss only.

Update the model env values shown above, then bootstrap once so the webhook is confirmed and the initial history sync is scheduled.

## Test order

In Message Yourself:

```text
hi
```

Use **Ask Memory**, then test:

```text
What messages did I receive yesterday that you think are important?
```

Then:

```text
/sync 24h
```

After sync, test a known fact from a recent chat or event poster.

## Validation

- All Python modules compile successfully.
- The existing identity, normalizer, interactive-menu, and media tests pass: `15 passed` in the available test environment.
- Full test collection also requires the Redis Python package; package installation was unavailable in the build sandbox because DNS access to PyPI was temporarily unavailable.
