import shop_db as db

async def get_user_shop(guild_id, owner_id):
    return await db.fetchrow("""
        SELECT * FROM shop.shops
        WHERE guild_id=$1 AND owner_id=$2 AND owner_type='USER'
          AND delete_reason IS NULL
        LIMIT 1
    """, guild_id, owner_id)

async def create_shop(guild_id, owner_id, name, description):
    return await db.fetchrow("""
        INSERT INTO shop.shops(guild_id,owner_id,name,description)
        VALUES($1,$2,$3,$4)
        RETURNING *
    """, guild_id, owner_id, name, description)

async def add_product(shop_id, name, description, price, product_type="CUSTOM", role_id=None):
    return await db.fetchrow("""
        INSERT INTO shop.products(shop_id,name,description,price,product_type,role_id)
        VALUES($1,$2,$3,$4,$5,$6)
        RETURNING *
    """, shop_id, name, description, price, product_type, role_id)

async def products(shop_id):
    return await db.fetch("""
        SELECT * FROM shop.products
        WHERE shop_id=$1 AND status <> 'deleted'
        ORDER BY product_id
    """, shop_id)

async def get_product(product_id):
    return await db.fetchrow("""
        SELECT p.*, s.guild_id, s.owner_id, s.name AS shop_name,
               s.description AS shop_description, s.status AS shop_status
        FROM shop.products p
        JOIN shop.shops s ON s.shop_id=p.shop_id
        WHERE p.product_id=$1
    """, product_id)

async def create_transaction(guild_id, product, buyer_id):
    return await db.fetchrow("""
        INSERT INTO shop.transactions(
            guild_id,shop_id,product_id,buyer_id,seller_id,
            shop_name_snapshot,product_name_snapshot,
            product_description_snapshot,price_snapshot,status
        )
        VALUES($1,$2,$3,$4,$5,$6,$7,$8,$9,'SELLER_ACTION_REQUIRED')
        RETURNING *
    """, guild_id, product["shop_id"], product["product_id"], buyer_id,
         product["owner_id"], product["shop_name"], product["name"],
         product["description"], product["price"])

async def transaction(transaction_id):
    return await db.fetchrow(
        "SELECT * FROM shop.transactions WHERE transaction_id=$1", transaction_id
    )

async def set_transaction_status(transaction_id, status, previous_status=None):
    return await db.fetchrow("""
        UPDATE shop.transactions
        SET status=$2, previous_status=COALESCE($3,previous_status), updated_at=NOW()
        WHERE transaction_id=$1
        RETURNING *
    """, transaction_id, status, previous_status)

async def set_ticket(transaction_id, channel_id):
    await db.execute("""
        UPDATE shop.transactions SET ticket_channel_id=$2, updated_at=NOW()
        WHERE transaction_id=$1
    """, transaction_id, channel_id)

async def set_shop_panel(shop_id, thread_id, message_id):
    await db.execute("""
        UPDATE shop.shops
        SET forum_thread_id=$2,panel_message_id=$3,updated_at=NOW()
        WHERE shop_id=$1
    """, shop_id, thread_id, message_id)
