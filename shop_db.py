import asyncpg

SCHEMA = """
CREATE SCHEMA IF NOT EXISTS shop;

CREATE TABLE IF NOT EXISTS shop.shops (
  shop_id BIGSERIAL PRIMARY KEY,
  guild_id BIGINT NOT NULL,
  owner_id BIGINT,
  owner_type TEXT NOT NULL DEFAULT 'USER',
  name TEXT NOT NULL,
  description TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'active',
  is_official BOOLEAN NOT NULL DEFAULT FALSE,
  forum_thread_id BIGINT,
  panel_message_id BIGINT,
  delete_reason TEXT,
  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE UNIQUE INDEX IF NOT EXISTS uq_shop_owner
ON shop.shops(guild_id, owner_id)
WHERE owner_type='USER' AND status <> 'deleted';

CREATE TABLE IF NOT EXISTS shop.products (
  product_id BIGSERIAL PRIMARY KEY,
  shop_id BIGINT NOT NULL REFERENCES shop.shops(shop_id),
  name TEXT NOT NULL,
  description TEXT NOT NULL,
  price BIGINT NOT NULL CHECK(price >= 1),
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
  seller_id BIGINT,
  shop_name_snapshot TEXT NOT NULL,
  product_name_snapshot TEXT NOT NULL,
  product_description_snapshot TEXT NOT NULL,
  price_snapshot BIGINT NOT NULL,
  status TEXT NOT NULL DEFAULT 'PAYMENT_PENDING',
  previous_status TEXT,
  ticket_channel_id BIGINT,
  bank_reference_id TEXT,
  deadline TIMESTAMPTZ,
  staff_called BOOLEAN NOT NULL DEFAULT FALSE,
  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS shop.logs (
  log_id BIGSERIAL PRIMARY KEY,
  guild_id BIGINT NOT NULL,
  event_type TEXT NOT NULL,
  reference_id TEXT,
  actor_id BIGINT,
  metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS shop.settings (
  guild_id BIGINT PRIMARY KEY,
  forum_channel_id BIGINT,
  ticket_category_id BIGINT,
  announce_channel_id BIGINT,
  staff_role_id BIGINT,
  official_shop_id BIGINT
);
"""

async def init_db(url: str):
    pool = await asyncpg.create_pool(url)
    async with pool.acquire() as con:
        await con.execute(SCHEMA)
    return pool
