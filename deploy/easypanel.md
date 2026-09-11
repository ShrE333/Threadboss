# EasyPanel V1.3

Deploy only the `threadboss` Compose service. WAHA stays untouched.

Compose services:

```text
postgres (pgvector)
redis
gateway :8000
worker
```

Expose only `gateway:8000` through the existing ThreadBoss domain.

Paste `.env.easypanel.example` values into **threadboss -> Environment** and enable **Create .env file**.

After deploy:

```text
/health -> gateway alive
/ready  -> redis + database ready
```

Then test WhatsApp Message Yourself with `/status`, `/agents`, and `/tasks`.
