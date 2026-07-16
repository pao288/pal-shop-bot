import os
import logging
import asyncpg

POOL = None
log = logging.getLogger("pal_shop_db")

# スキーマは「独立して実行できるまとまり」ごとに分割しておく。
# 1つのブロックが失敗しても（例: 既存データの都合でDOブロックがエラーになる等）、
# 他のブロック（特に新しいカラム追加）が確実に反映されるようにするため。
SCHEMA_STEPS = [
("base_tables", """
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
"""),

("shops_columns", """
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
"""),

("products_table", """
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
"""),

("products_columns", """
ALTER TABLE shop.products ADD COLUMN IF NOT EXISTS currency TEXT;
ALTER TABLE shop.products ADD COLUMN IF NOT EXISTS updated_at TIMESTAMPTZ;
ALTER TABLE shop.products ADD COLUMN IF NOT EXISTS stock INTEGER;
UPDATE shop.products SET currency='PAL' WHERE currency IS NULL;
UPDATE shop.products SET updated_at=NOW() WHERE updated_at IS NULL;
UPDATE shop.products SET stock=999999 WHERE stock IS NULL;
ALTER TABLE shop.products ALTER COLUMN currency SET DEFAULT 'PAL';
ALTER TABLE shop.products ALTER COLUMN updated_at SET DEFAULT NOW();
ALTER TABLE shop.products ALTER COLUMN stock SET DEFAULT 0;
ALTER TABLE shop.products ALTER COLUMN stock SET NOT NULL;
"""),

# 過去バグ対策マイグレーション: !shopsetup再実行等で重複してしまった公式CASINO SHOPを1つに統合する。
# 統合先は shop.systems.casino_shop_id が指しているもの（無ければ一番古いもの）。
# 統合される側の商品は統合先へ付け替えてから DELETED にするため、迷子になっていた商品もここで復旧する。
# ここが万一失敗しても、他のステップ（カラム追加等）には影響しない。
("casino_shop_dedup_migration", """
DO $$
DECLARE
    g RECORD;
    keep_id BIGINT;
BEGIN
    FOR g IN SELECT DISTINCT guild_id FROM shop.shops
             WHERE shop_type='CASINO' AND is_official=TRUE AND status<>'DELETED'
    LOOP
        SELECT casino_shop_id INTO keep_id FROM shop.systems WHERE guild_id=g.guild_id;
        IF keep_id IS NULL OR NOT EXISTS(
            SELECT 1 FROM shop.shops WHERE shop_id=keep_id AND guild_id=g.guild_id
                AND shop_type='CASINO' AND is_official=TRUE AND status<>'DELETED'
        ) THEN
            SELECT shop_id INTO keep_id FROM shop.shops
            WHERE guild_id=g.guild_id AND shop_type='CASINO' AND is_official=TRUE AND status<>'DELETED'
            ORDER BY shop_id LIMIT 1;
        END IF;

        UPDATE shop.products SET shop_id=keep_id, updated_at=NOW()
        WHERE shop_id IN (
            SELECT shop_id FROM shop.shops
            WHERE guild_id=g.guild_id AND shop_type='CASINO' AND is_official=TRUE
              AND status<>'DELETED' AND shop_id<>keep_id
        );

        UPDATE shop.shops SET status='DELETED', updated_at=NOW()
        WHERE guild_id=g.guild_id AND shop_type='CASINO' AND is_official=TRUE
          AND status<>'DELETED' AND shop_id<>keep_id;

        UPDATE shop.systems SET casino_shop_id=keep_id, updated_at=NOW() WHERE guild_id=g.guild_id;
    END LOOP;
END $$;
"""),

# 公式CASINO SHOPはギルドごとに1つだけに制限する（!shopsetup再実行時の商品迷子バグの再発防止）。
("casino_shop_unique_index", """
CREATE UNIQUE INDEX IF NOT EXISTS uq_casino_shop_guild
ON shop.shops(guild_id) WHERE shop_type='CASINO' AND is_official=TRUE AND status<>'DELETED';
"""),

# 店舗評価（購入者が取引完了後に1～5で評価）。
("ratings_table", """
CREATE TABLE IF NOT EXISTS shop.ratings (
    rating_id BIGSERIAL PRIMARY KEY,
    shop_id BIGINT NOT NULL REFERENCES shop.shops(shop_id) ON DELETE CASCADE,
    transaction_id BIGINT NOT NULL UNIQUE,
    rater_id BIGINT NOT NULL,
    score SMALLINT NOT NULL CHECK(score BETWEEN 1 AND 5),
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
"""),

# ログ／管理／オークションカテゴリ用の永続チャンネルID。
("systems_new_columns", """
ALTER TABLE shop.systems ADD COLUMN IF NOT EXISTS log_channel_id BIGINT;
ALTER TABLE shop.systems ADD COLUMN IF NOT EXISTS admin_channel_id BIGINT;
ALTER TABLE shop.systems ADD COLUMN IF NOT EXISTS admin_message_id BIGINT;
ALTER TABLE shop.systems ADD COLUMN IF NOT EXISTS auction_category_id BIGINT;
ALTER TABLE shop.systems ADD COLUMN IF NOT EXISTS pal_open_message_id BIGINT;
ALTER TABLE shop.systems ADD COLUMN IF NOT EXISTS casino_message_id BIGINT;
ALTER TABLE shop.systems ADD COLUMN IF NOT EXISTS auction_channel_id BIGINT;
ALTER TABLE shop.systems ADD COLUMN IF NOT EXISTS auction_message_id BIGINT;
"""),

("products_fk_fix", """
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
"""),

("transactions_columns", """
ALTER TABLE shop.transactions ADD COLUMN IF NOT EXISTS currency TEXT;
ALTER TABLE shop.transactions ADD COLUMN IF NOT EXISTS previous_status TEXT;
ALTER TABLE shop.transactions ADD COLUMN IF NOT EXISTS ticket_channel_id BIGINT;
ALTER TABLE shop.transactions ADD COLUMN IF NOT EXISTS shop_name_snapshot TEXT;
ALTER TABLE shop.transactions ADD COLUMN IF NOT EXISTS product_name_snapshot TEXT;
ALTER TABLE shop.transactions ADD COLUMN IF NOT EXISTS product_description_snapshot TEXT;
ALTER TABLE shop.transactions ADD COLUMN IF NOT EXISTS price_snapshot BIGINT;
ALTER TABLE shop.transactions ADD COLUMN IF NOT EXISTS updated_at TIMESTAMPTZ;
ALTER TABLE shop.transactions ADD COLUMN IF NOT EXISTS quantity INTEGER;
ALTER TABLE shop.transactions ALTER COLUMN shop_id DROP NOT NULL;
ALTER TABLE shop.transactions ALTER COLUMN product_id DROP NOT NULL;
UPDATE shop.transactions SET currency='PAL' WHERE currency IS NULL;
UPDATE shop.transactions SET updated_at=NOW() WHERE updated_at IS NULL;
UPDATE shop.transactions SET quantity=1 WHERE quantity IS NULL;
ALTER TABLE shop.transactions ALTER COLUMN currency SET DEFAULT 'PAL';
ALTER TABLE shop.transactions ALTER COLUMN updated_at SET DEFAULT NOW();
ALTER TABLE shop.transactions ALTER COLUMN quantity SET DEFAULT 1;
"""),

("escrows_table", """
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
"""),

("auctions_tables", """
CREATE TABLE IF NOT EXISTS shop.auctions (
    auction_id BIGSERIAL PRIMARY KEY,
    guild_id BIGINT NOT NULL,
    seller_id BIGINT NOT NULL,
    product_name TEXT NOT NULL,
    product_description TEXT NOT NULL,
    start_price BIGINT NOT NULL,
    current_price BIGINT NOT NULL,
    highest_bidder_id BIGINT,
    ends_at TIMESTAMPTZ NOT NULL,
    status TEXT NOT NULL DEFAULT 'ACTIVE',
    transaction_id BIGINT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    completed_at TIMESTAMPTZ
);
CREATE UNIQUE INDEX IF NOT EXISTS uq_active_auction_guild
ON shop.auctions(guild_id) WHERE status='ACTIVE';

CREATE TABLE IF NOT EXISTS shop.auction_bids (
    bid_id BIGSERIAL PRIMARY KEY,
    auction_id BIGINT NOT NULL REFERENCES shop.auctions(auction_id) ON DELETE CASCADE,
    bidder_id BIGINT NOT NULL,
    amount BIGINT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
"""),
]

async def init_db():
    global POOL
    POOL = await asyncpg.create_pool(os.environ["DATABASE_URL"])
    async with POOL.acquire() as con:
        for step_name, stmt in SCHEMA_STEPS:
            try:
                await con.execute(stmt)
            except Exception:
                # 1ステップが失敗しても他のステップ（特に新カラム追加）は必ず実行する。
                log.exception("shop_db schema migration step failed: %s", step_name)

async def fetchrow(q, *args):
    async with POOL.acquire() as con:
        return await con.fetchrow(q, *args)

async def fetch(q, *args):
    async with POOL.acquire() as con:
        return await con.fetch(q, *args)

async def fetchval(q, *args):
    async with POOL.acquire() as con:
        return await con.fetchval(q, *args)

async def execute(q, *args):
    async with POOL.acquire() as con:
        return await con.execute(q, *args)


def acquire():
    if POOL is None:
        raise RuntimeError("SHOP_DB_POOL_NOT_INITIALIZED")
    return POOL.acquire()
