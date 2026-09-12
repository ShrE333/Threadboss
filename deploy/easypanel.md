# EasyPanel — ThreadBoss V1.7 Neon Multi-Tenant

V1.7 uses Neon Postgres instead of a local Postgres container.

Compose services:

```text
redis
gateway :8000
worker
```

Expose only `gateway:8000` through the ThreadBoss public domain.

In **threadboss -> Environment**, enable **Create .env file** and set the values from `.env.easypanel.example`.

Critical new values:

```env
DATABASE_URL=<Neon pooled postgres URL with sslmode=require>
ONBOARDING_API_KEY=<long random server-side secret>
SESSION_TENANTS_JSON={}
ALLOW_LEGACY_GLOBAL_CHANNEL_KEY=false
```

Do not expose Neon credentials or `ONBOARDING_API_KEY` to browser JavaScript. The website backend calls ThreadBoss onboarding endpoints.

After deployment:

```text
GET /health
GET /ready
```

`/ready` should show `database_mode: neon/external` and `multi_tenant: true`.
