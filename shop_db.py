import os
import asyncpg

POOL = None

SCHEMA = """
CREATE SCHEMA IF NOT EXISTS shop;

CREATE TABLE IF NOT EXISTS shop.systems (
    guild_id BIGINT PRIMARY KEY,
    status TEXT NOT NULL DEFAULT 'ACTIVE',
    pal_category_id BIGINT,
    pal_open_channel_id BIGINT,
    pal_announce_channel_id BIGINT,
    pal_forum_channel_id BIGINT,
    casino_category_id BIGINT,
    casino_channel_id BIGINT,
    casino_announce_channel_id BIGINT,
    pal_ticket_category_id BIGINT,
    casino_ticket_category_id BIGINT,
    casino_shop_id BIGINT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS shop.shops (
    shop_id BIGSERIAL PRIMARY KEY,
    guild_id BIGINT NOT NULL,
    shop_type TEXT NOT NULL,
    owner_id BIGINT,
    owner_type TEXT NOT NULL DEFAULT 'USER',
    is_official BOOLEAN NOT NULL DEFAULT FALSE,
    name TEXT NOT NULL,
    description TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'ACTIVE',
    forum_thread_id BIGINT,
    panel_message_id BIGINT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

DROP INDEX IF EXISTS shop.uq_shop_user_owner;
CREATE UNIQUE INDEX IF NOT EXISTS uq_shop_user_owner_type
ON shop.shops(guild_id, owner_id, shop_type)
WHERE owner_type='USER' AND status <> 'DELETED';

CREATE TABLE IF NOT EXISTS shop.products (
    product_id BIGSERIAL PRIMARY KEY,
    shop_id BIGINT NOT NULL REFERENCES shop.shops(shop_id) ON DELETE CASCADE,
    name TEXT NOT NULL,
    description TEXT NOT NULL,
    price BIGINT NOT NULL CHECK(price > 0),
    currency TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'ACTIVE',
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS shop.transactions (
    transaction_id BIGSERIAL PRIMARY KEY,
    guild_id BIGINT NOT NULL,
    shop_id BIGINT NOT NULL,
    product_id BIGINT NOT NULL,
    buyer_id BIGINT NOT NULL,
    seller_id BIGINT,
    currency TEXT NOT NULL,
    shop_name_snapshot TEXT NOT NULL,
    product_name_snapshot TEXT NOT NULL,
    product_description_snapshot TEXT NOT NULL,
    price_snapshot BIGINT NOT NULL,
    status TEXT NOT NULL DEFAULT 'PAYMENT_PENDING',
    previous_status TEXT,
    ticket_channel_id BIGINT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS shop.escrows (
    transaction_id BIGINT PRIMARY KEY,
    buyer_id BIGINT NOT NULL,
    seller_id BIGINT,
    currency TEXT NOT NULL,
    amount BIGINT NOT NULL,
    status TEXT NOT NULL DEFAULT 'HELD',
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    released_at TIMESTAMPTZ,
    refunded_at TIMESTAMPTZ
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
