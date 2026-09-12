CREATE EXTENSION IF NOT EXISTS vector;
CREATE EXTENSION IF NOT EXISTS pgcrypto;

CREATE TABLE IF NOT EXISTS public.users (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    auth_provider VARCHAR(50) NOT NULL,
    auth_subject VARCHAR(255) NOT NULL,
    email VARCHAR(255),
    display_name VARCHAR(120),
    avatar_url TEXT,
    status VARCHAR(30) NOT NULL DEFAULT 'active',
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE(auth_provider, auth_subject)
);

CREATE TABLE IF NOT EXISTS public.tenants (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    owner_user_id UUID NOT NULL REFERENCES public.users(id) ON DELETE CASCADE,
    name VARCHAR(120),
    status VARCHAR(30) NOT NULL DEFAULT 'active',
    onboarding_completed BOOLEAN NOT NULL DEFAULT false,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS public.tenant_members (
    tenant_id UUID NOT NULL REFERENCES public.tenants(id) ON DELETE CASCADE,
    user_id UUID NOT NULL REFERENCES public.users(id) ON DELETE CASCADE,
    role VARCHAR(30) NOT NULL DEFAULT 'owner',
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (tenant_id, user_id)
);

CREATE TABLE IF NOT EXISTS public.tenant_permissions (
    tenant_id UUID PRIMARY KEY REFERENCES public.tenants(id) ON DELETE CASCADE,
    read_direct_messages BOOLEAN NOT NULL DEFAULT true,
    read_group_messages BOOLEAN NOT NULL DEFAULT true,
    process_images BOOLEAN NOT NULL DEFAULT true,
    process_voice_notes BOOLEAN NOT NULL DEFAULT true,
    process_documents BOOLEAN NOT NULL DEFAULT true,
    create_reminders BOOLEAN NOT NULL DEFAULT true,
    calendar_read BOOLEAN NOT NULL DEFAULT false,
    calendar_write BOOLEAN NOT NULL DEFAULT false,
    send_messages BOOLEAN NOT NULL DEFAULT false,
    auto_followups BOOLEAN NOT NULL DEFAULT false,
    allow_actions BOOLEAN NOT NULL DEFAULT false,
    history_import_hours INTEGER NOT NULL DEFAULT 24,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS public.whatsapp_connections (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id UUID NOT NULL REFERENCES public.tenants(id) ON DELETE CASCADE,
    waha_session_name VARCHAR(150) NOT NULL UNIQUE,
    phone_number_hash TEXT,
    owner_jid TEXT,
    owner_lid TEXT,
    engine VARCHAR(30) NOT NULL DEFAULT 'GOWS',
    status VARCHAR(30) NOT NULL DEFAULT 'pending',
    connected_at TIMESTAMPTZ,
    disconnected_at TIMESTAMPTZ,
    last_seen_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_whatsapp_connections_tenant
ON public.whatsapp_connections(tenant_id);

CREATE TABLE IF NOT EXISTS public.onboarding_sessions (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id UUID NOT NULL REFERENCES public.tenants(id) ON DELETE CASCADE,
    whatsapp_connection_id UUID REFERENCES public.whatsapp_connections(id) ON DELETE CASCADE,
    status VARCHAR(30) NOT NULL DEFAULT 'created',
    qr_reference TEXT,
    expires_at TIMESTAMPTZ,
    error_message TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    completed_at TIMESTAMPTZ
);

CREATE TABLE IF NOT EXISTS public.channel_connections (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id UUID NOT NULL REFERENCES public.tenants(id) ON DELETE CASCADE,
    channel_type VARCHAR(30) NOT NULL,
    external_account_id TEXT,
    status VARCHAR(30) NOT NULL DEFAULT 'connected',
    metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE SCHEMA IF NOT EXISTS threadboss;

CREATE TABLE IF NOT EXISTS threadboss.messages (
    id BIGSERIAL PRIMARY KEY,
    tenant_id TEXT NOT NULL,
    channel TEXT NOT NULL DEFAULT 'whatsapp',
    session_id TEXT NOT NULL,
    chat_id TEXT NOT NULL,
    chat_type TEXT NOT NULL DEFAULT 'unknown',
    sender_id TEXT,
    recipient_id TEXT,
    participant_id TEXT,
    message_id TEXT NOT NULL,
    ts TIMESTAMPTZ NOT NULL,
    text TEXT NOT NULL DEFAULT '',
    from_me BOOLEAN NOT NULL DEFAULT FALSE,
    metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
    embedding vector(768),
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (tenant_id, channel, session_id, message_id)
);

CREATE INDEX IF NOT EXISTS idx_tb_messages_tenant_ts
ON threadboss.messages(tenant_id, ts DESC);
CREATE INDEX IF NOT EXISTS idx_tb_messages_chat
ON threadboss.messages(tenant_id, channel, chat_id, ts DESC);

CREATE TABLE IF NOT EXISTS threadboss.tasks (
    id BIGSERIAL PRIMARY KEY,
    tenant_id TEXT NOT NULL,
    channel TEXT NOT NULL DEFAULT 'whatsapp',
    title TEXT NOT NULL,
    owner TEXT,
    due_at TIMESTAMPTZ,
    status TEXT NOT NULL DEFAULT 'open',
    source_message_id TEXT,
    source_chat_id TEXT,
    evidence TEXT,
    confidence REAL NOT NULL DEFAULT 0.5,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_tb_tasks_tenant_status_due
ON threadboss.tasks(tenant_id, status, due_at);

CREATE TABLE IF NOT EXISTS threadboss.channel_api_tokens (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id TEXT NOT NULL,
    channel TEXT NOT NULL DEFAULT 'any',
    token_sha256 TEXT NOT NULL UNIQUE,
    label TEXT,
    status TEXT NOT NULL DEFAULT 'active',
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_used_at TIMESTAMPTZ
);

CREATE TABLE IF NOT EXISTS threadboss.audit_logs (
    id BIGSERIAL PRIMARY KEY,
    tenant_id TEXT,
    actor_type TEXT NOT NULL,
    actor_id TEXT,
    action TEXT NOT NULL,
    resource_type TEXT,
    resource_id TEXT,
    success BOOLEAN NOT NULL DEFAULT true,
    metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

ALTER TABLE threadboss.messages ENABLE ROW LEVEL SECURITY;
ALTER TABLE threadboss.messages FORCE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS tenant_isolation ON threadboss.messages;
CREATE POLICY tenant_isolation ON threadboss.messages
USING (tenant_id = current_setting('threadboss.tenant_id', true))
WITH CHECK (tenant_id = current_setting('threadboss.tenant_id', true));

ALTER TABLE threadboss.tasks ENABLE ROW LEVEL SECURITY;
ALTER TABLE threadboss.tasks FORCE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS tenant_isolation ON threadboss.tasks;
CREATE POLICY tenant_isolation ON threadboss.tasks
USING (tenant_id = current_setting('threadboss.tenant_id', true))
WITH CHECK (tenant_id = current_setting('threadboss.tenant_id', true));
