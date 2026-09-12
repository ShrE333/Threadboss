# ThreadBoss V1.7 — Neon Multi-Tenant

V1.7 moves ThreadBoss from the single local Postgres prototype to a shared Neon Postgres database and adds first-class multi-user / multi-tenant onboarding.

## What changed

- `DATABASE_URL` now points to the shared Neon Postgres database.
- The EasyPanel Compose stack no longer runs a local Postgres container; only `redis`, `gateway`, and `worker` remain.
- Website users are mapped to a tenant in Neon.
- Every tenant gets its own WAHA session (`tb_<tenant uuid prefix>`).
- WAHA webhook events resolve the tenant from `public.whatsapp_connections`, not from a hard-coded session map.
- Website backend can create users/tenants, start WhatsApp onboarding, fetch QR, poll connection status, and update permissions.
- Slack/Telegram adapters can use tenant-scoped API tokens instead of one global key.
- Agent memory/tasks use PostgreSQL Row Level Security (RLS) and every DB transaction sets the tenant context.
- Existing V1.6 WAHA sessions can be bound to a newly created Neon tenant without rescanning the QR.
- V1.6 memory, agents, native WhatsApp menu, history sync, OCR/STT/PDF/image tools, planner/follow-up/action/knowledge agents remain.

## Deployment architecture

```text
Website login
    |
    v
Website backend ---- X-ThreadBoss-Onboarding-Key ----> ThreadBoss gateway
                                                       |
                                                       +--> Neon Postgres
                                                       +--> WAHA multi-session
                                                       +--> Redis

Each user
  -> one Neon tenant
  -> one WhatsApp connection
  -> one WAHA session
  -> isolated messages/tasks/vector memory
```

## 1. Neon database

Use the pooled Neon connection string for production. Put it only in EasyPanel/server secrets; do not commit it and do not expose it to browser JavaScript.

```env
DATABASE_URL=postgresql://USER:PASSWORD@YOUR-ENDPOINT-pooler.REGION.aws.neon.tech/neondb?sslmode=require
DATABASE_POOL_MIN_SIZE=1
DATABASE_POOL_MAX_SIZE=5
DATABASE_COMMAND_TIMEOUT_SECONDS=60
```

ThreadBoss runs idempotent `CREATE TABLE IF NOT EXISTS` / extension initialization on first `/ready`. The same schema is also included in:

```text
database/neon_schema.sql
```

If the website team prefers migrations managed outside ThreadBoss, run that SQL in the Neon SQL editor once.

## 2. Required new EasyPanel environment values

Keep all existing WAHA, Gemini, Redis, admin and media settings. Add/change:

```env
# Shared Neon DB
DATABASE_URL=YOUR_NEON_POOLED_POSTGRES_URL
DATABASE_POOL_MIN_SIZE=1
DATABASE_POOL_MAX_SIZE=5
DATABASE_COMMAND_TIMEOUT_SECONDS=60

# Website backend -> ThreadBoss. Never put this in browser JS.
ONBOARDING_API_KEY=GENERATE_A_LONG_RANDOM_SECRET

# New Slack/Telegram model: tenant-scoped tokens.
ALLOW_LEGACY_GLOBAL_CHANNEL_KEY=false

# New users come from Neon instead of a static map.
SESSION_TENANTS_JSON={}
```

Use `.env.easypanel.example` as the complete reference.

## 3. EasyPanel

V1.7 Compose services:

```text
redis
worker
gateway :8000
```

There is intentionally **no Postgres container** in `docker-compose.easypanel.yml`; Neon is the database.

Expose only:

```text
gateway -> HTTP -> 8000
```

Do not expose Redis.

## 4. Website onboarding API

The browser should never call WAHA directly and must never receive the WAHA API key or `ONBOARDING_API_KEY`.

The website backend calls ThreadBoss with:

```http
X-ThreadBoss-Onboarding-Key: <ONBOARDING_API_KEY>
```

### A. Register/login user into ThreadBoss

```http
POST /v1/onboarding/register
Content-Type: application/json

{
  "auth_provider": "clerk",
  "auth_subject": "user_abc123",
  "email": "person@example.com",
  "display_name": "Person Name",
  "tenant_name": "Person's ThreadBoss"
}
```

Response includes:

```json
{
  "user_id": "...",
  "tenant_id": "...",
  "tenant_name": "...",
  "onboarding_completed": false,
  "whatsapp_connection": null
}
```

Call this after website authentication. `auth_subject` should be the immutable user ID from Clerk/Auth.js/Supabase Auth/etc., not the user's email address.

### B. User clicks Connect WhatsApp

```http
POST /v1/onboarding/whatsapp/start

{
  "tenant_id": "<tenant_id>"
}
```

ThreadBoss creates/reuses a WAHA session and returns `qr_endpoint` and `status_endpoint`.

### C. Website fetches QR

```http
GET /v1/onboarding/whatsapp/<connection_id>/qr
```

Response contains `mimetype` + base64 `data`. The website renders that as an image. Do not persist the QR in browser storage.

### D. Website polls status

```http
GET /v1/onboarding/whatsapp/<connection_id>/status
```

When WAHA becomes `WORKING`, ThreadBoss stores the owner's `@c.us` / `@lid`, marks the tenant onboarded, configures the webhook, and schedules the initial history backfill.

### E. Permissions

```http
PUT /v1/onboarding/permissions

{
  "tenant_id": "<tenant_id>",
  "read_direct_messages": true,
  "read_group_messages": true,
  "process_images": true,
  "process_voice_notes": true,
  "process_documents": true,
  "create_reminders": true,
  "send_messages": false,
  "allow_actions": false,
  "history_import_hours": 48
}
```

## 5. Existing V1.6 user migration

Moving to Neon does not automatically copy the old local Postgres volume. For the current connected WhatsApp account, create a tenant in Neon first, then bind the existing WAHA session so the user does **not** need to scan QR again.

First call `/v1/onboarding/register` and note its `tenant_id`.

Then as admin:

```http
POST /admin/tenants/<tenant_id>/bind-session/<existing_waha_session>
X-ThreadBoss-Admin: <ADMIN_TOKEN>
```

That resolves `@c.us` / `@lid`, reconfigures the webhook, stores the connection in Neon, and schedules history backfill.

## 6. Slack / Telegram multi-user adapters

Create a tenant-scoped token:

```http
POST /v1/onboarding/channel-token
X-ThreadBoss-Onboarding-Key: <ONBOARDING_API_KEY>

{
  "tenant_id": "<tenant_id>",
  "channel": "telegram",
  "label": "main telegram bot"
}
```

ThreadBoss returns a token once. Only the SHA-256 digest is stored in Neon.

The adapter then calls `/v1/messages` and `/v1/query` using:

```http
X-ThreadBoss-Key: tbk_...
```

The token itself determines the tenant. If an adapter supplies another `tenant_id`, ThreadBoss rejects it.

## 7. Tenant isolation

Agent memory is stored in:

```text
threadboss.messages
threadboss.tasks
```

Both tables have PostgreSQL RLS enabled + forced. Every tenant-owned DB operation opens a transaction and runs:

```sql
SELECT set_config('threadboss.tenant_id', '<tenant>', true);
```

Policies only allow rows where:

```sql
tenant_id = current_setting('threadboss.tenant_id', true)
```

Vector retrieval also includes the tenant ID explicitly.

This is the V1.7 tenant fence. The planned AES-256-GCM / HKDF envelope-encryption layer is **not yet included in V1.7**; V1.7 focuses on shared Neon + identity + tenant isolation.

## 8. Upgrade from V1.6

Copy V1.7 over the repo, but do not overwrite your real `.env` and do not delete Redis data.

```powershell
git add .
git commit -m "Upgrade ThreadBoss to V1.7 Neon multi-tenant"
git push
```

Update EasyPanel environment with the Neon URL + onboarding key, then deploy ThreadBoss only.

Test:

```powershell
$TB = "https://YOUR-THREADBOSS-DOMAIN"
Invoke-RestMethod "$TB/health" | ConvertTo-Json -Depth 10
Invoke-RestMethod "$TB/ready"  | ConvertTo-Json -Depth 10
```

Expected `/ready` includes:

```json
{
  "ok": true,
  "redis": true,
  "database": true,
  "database_mode": "neon/external",
  "multi_tenant": true
}
```

## Files for the website team

Give them:

```text
database/neon_schema.sql
README.md (section 4)
```

They do not need the WAHA key, Gemini key, DB password, or ThreadBoss admin token in the frontend.
