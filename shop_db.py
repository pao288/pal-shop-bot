import os
import asyncpg

POOL = None

SCHEMA = """
CREATE SCHEMA IF NOT EXISTS shop;

CREATE TABLE IF NOT EXISTS shop.shops (
    shop_id BIGSERIAL PRIMARY KEY,
    guild_id BIGINT NOT NULL,
    owner_id BIGINT NOT NULL,
    owner_type TEXT NOT NULL DEFAULT 'USER',
    is_official BOOLEAN NOT NULL DEFAULT FALSE,
    name TEXT NOT NULL,
    description TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'active',
    forum_thread_id BIGINT,
    panel_message_id BIGINT,
    delete_reason TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE UNIQUE INDEX IF NOT EXISTS uq_shop_user_owner
ON shop.shops(guild_id, owner_id)
WHERE owner_type='USER' AND delete_reason IS NULL;

CREATE TABLE IF NOT EXISTS shop.products (
    product_id BIGSERIAL PRIMARY KEY,
    shop_id BIGINT NOT NULL REFERENCES shop.shops(shop_id),
    name TEXT NOT NULL,
    description TEXT NOT NULL,
    price BIGINT NOT NULL CHECK(price > 0),
    product_type TEXT NOT NULL DEFAULT 'CUSTOM',
    role_id BIGINT,
    status TEXT NOT NULL DEFAULT 'active',
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS shop.transactions (
    transaction_id BIGSERIAL PRIMARY KEY,
    guild_id BIGINT NOT NULL,
    shop_id BIGINT NOT NULL,
    product_id BIGINT NOT NULL,
    buyer_id BIGINT NOT NULL,
    seller_id BIGINT NOT NULL,
    shop_name_snapshot TEXT NOT NULL,
    product_name_snapshot TEXT NOT NULL,
    product_description_snapshot TEXT NOT NULL,
    price_snapshot BIGINT NOT NULL,
    status TEXT NOT NULL DEFAULT 'PAYMENT_PENDING',
    previous_status TEXT,
    ticket_channel_id BIGINT,
    deadline_at TIMESTAMPTZ,
    staff_called BOOLEAN NOT NULL DEFAULT FALSE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS shop.logs (
    log_id BIGSERIAL PRIMARY KEY,
    guild_id BIGINT NOT NULL,
    actor_id BIGINT,
    action TEXT NOT NULL,
    reference_id TEXT,
    metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
"""

async def init_db():
    global POOL
    POOL = await asyncpg.create_pool(os.environ["DATABASE_URL"])
    async with POOL.acquire() as con:
        await con.execute(SCHEMA)

async def fetchrow(q, *args):
    async with POOL.acquire() as con:
        return await con.fetchrow(q, *args)

async def fetch(q, *args):
    async with POOL.acquire() as con:
        return await con.fetch(q, *args)

async def execute(q, *args):
    async with POOL.acquire() as con:
        return await con.execute(q, *args)
