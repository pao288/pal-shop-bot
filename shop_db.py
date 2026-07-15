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

ALTER TABLE shop.shops ADD COLUMN IF NOT EXISTS shop_type TEXT;
ALTER TABLE shop.shops ADD COLUMN IF NOT EXISTS owner_type TEXT;
ALTER TABLE shop.shops ADD COLUMN IF NOT EXISTS is_official BOOLEAN;
ALTER TABLE shop.shops ADD COLUMN IF NOT EXISTS forum_thread_id BIGINT;
ALTER TABLE shop.shops ADD COLUMN IF NOT EXISTS panel_message_id BIGINT;
ALTER TABLE shop.shops ADD COLUMN IF NOT EXISTS updated_at TIMESTAMPTZ;

UPDATE shop.shops SET shop_type='PAL' WHERE shop_type IS NULL;
UPDATE shop.shops SET owner_type='USER' WHERE owner_type IS NULL;
UPDATE shop.shops SET is_official=FALSE WHERE is_official IS NULL;
UPDATE shop.shops SET updated_at=NOW() WHERE updated_at IS NULL;

ALTER TABLE shop.shops ALTER COLUMN shop_type SET DEFAULT 'PAL';
ALTER TABLE shop.shops ALTER COLUMN owner_type SET DEFAULT 'USER';
ALTER TABLE shop.shops ALTER COLUMN is_official SET DEFAULT FALSE;
ALTER TABLE shop.shops ALTER COLUMN updated_at SET DEFAULT NOW();

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

ALTER TABLE shop.products ADD COLUMN IF NOT EXISTS currency TEXT;
ALTER TABLE shop.products ADD COLUMN IF NOT EXISTS updated_at TIMESTAMPTZ;
UPDATE shop.products SET currency='PAL' WHERE currency IS NULL;
UPDATE shop.products SET updated_at=NOW() WHERE updated_at IS NULL;
ALTER TABLE shop.products ALTER COLUMN currency SET DEFAULT 'PAL';
ALTER TABLE shop.products ALTER COLUMN updated_at SET DEFAULT NOW();

DO $$
BEGIN
    IF EXISTS (
        SELECT 1
        FROM pg_constraint
        WHERE conname = 'products_shop_id_fkey'
          AND conrelid = 'shop.products'::regclass
    ) THEN
        ALTER TABLE shop.products DROP CONSTRAINT products_shop_id_fkey;
    END IF;
END $$;

ALTER TABLE shop.products
ADD CONSTRAINT products_shop_id_fkey
FOREIGN KEY (shop_id) REFERENCES shop.shops(shop_id) ON DELETE CASCADE;

ALTER TABLE shop.transactions ADD COLUMN IF NOT EXISTS currency TEXT;
ALTER TABLE shop.transactions ADD COLUMN IF NOT EXISTS previous_status TEXT;
ALTER TABLE shop.transactions ADD COLUMN IF NOT EXISTS ticket_channel_id BIGINT;
ALTER TABLE shop.transactions ADD COLUMN IF NOT EXISTS shop_name_snapshot TEXT;
ALTER TABLE shop.transactions ADD COLUMN IF NOT EXISTS product_name_snapshot TEXT;
ALTER TABLE shop.transactions ADD COLUMN IF NOT EXISTS product_description_snapshot TEXT;
ALTER TABLE shop.transactions ADD COLUMN IF NOT EXISTS price_snapshot BIGINT;
ALTER TABLE shop.transactions ADD COLUMN IF NOT EXISTS updated_at TIMESTAMPTZ;
UPDATE shop.transactions SET currency='PAL' WHERE currency IS NULL;
UPDATE shop.transactions SET updated_at=NOW() WHERE updated_at IS NULL;
ALTER TABLE shop.transactions ALTER COLUMN currency SET DEFAULT 'PAL';
ALTER TABLE shop.transactions ALTER COLUMN updated_at SET DEFAULT NOW();

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
