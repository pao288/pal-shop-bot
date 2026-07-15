import asyncio
import discord
from datetime import datetime, timezone, timedelta
from bank_gateway import BankGateway

class ShopService:
    def __init__(self, pool, bot):
        self.pool = pool
        self.bot = bot
        self.bank = BankGateway()
        self.locks: dict[str, asyncio.Lock] = {}

    def lock(self, key):
        return self.locks.setdefault(key, asyncio.Lock())

    async def get_settings(self, guild_id):
        async with self.pool.acquire() as con:
            return await con.fetchrow("SELECT * FROM shop.settings WHERE guild_id=$1", guild_id)

    async def create_shop(self, guild, owner, name, description, product_name, product_description, price):
        async with self.lock(f"shop:{guild.id}:{owner.id}"):
            settings = await self.get_settings(guild.id)
            if not settings or not settings["forum_channel_id"]:
                return False, "SHOP_FORUM_NOT_CONFIGURED"
            forum = guild.get_channel(settings["forum_channel_id"])
            async with self.pool.acquire() as con:
                async with con.transaction():
                    exists = await con.fetchval(
                        "SELECT 1 FROM shop.shops WHERE guild_id=$1 AND owner_id=$2 AND owner_type='USER' AND status<>'deleted'",
                        guild.id, owner.id
                    )
                    if exists:
                        return False, "ALREADY_HAS_SHOP"
                    shop_id = await con.fetchval(
                        """INSERT INTO shop.shops(guild_id,owner_id,name,description)
                           VALUES($1,$2,$3,$4) RETURNING shop_id""",
                        guild.id, owner.id, name, description
                    )
                    product_id = await con.fetchval(
                        """INSERT INTO shop.products(shop_id,name,description,price)
                           VALUES($1,$2,$3,$4) RETURNING product_id""",
                        shop_id, product_name, product_description, price
                    )
            embed = self.shop_embed(name, description, owner)
            thread, message = await forum.create_thread(name=f"🏪 {name}", embed=embed)
            from views import StoreView
            await message.edit(view=StoreView(shop_id))
            async with self.pool.acquire() as con:
                await con.execute(
                    "UPDATE shop.shops SET forum_thread_id=$1,panel_message_id=$2 WHERE shop_id=$3",
                    thread.id, message.id, shop_id
                )
            return True, shop_id

    def shop_embed(self, name, description, owner):
        return discord.Embed(
            title=f"🏪 {name}",
            description=f"👤 店主\n{owner.mention}\n\n{description}"
        )

    async def products(self, shop_id, include_paused=False):
        q = "SELECT * FROM shop.products WHERE shop_id=$1 AND status='active' ORDER BY product_id"
        if include_paused:
            q = "SELECT * FROM shop.products WHERE shop_id=$1 AND status IN ('active','paused') ORDER BY product_id"
        async with self.pool.acquire() as con:
            return await con.fetch(q, shop_id)

    async def add_product(self, guild, shop_id, actor_id, name, description, price):
        async with self.pool.acquire() as con:
            shop = await con.fetchrow("SELECT * FROM shop.shops WHERE shop_id=$1", shop_id)
            if not shop or shop["owner_id"] != actor_id:
                return False, "NOT_OWNER"
            product_id = await con.fetchval(
                "INSERT INTO shop.products(shop_id,name,description,price) VALUES($1,$2,$3,$4) RETURNING product_id",
                shop_id, name, description, price
            )
        settings = await self.get_settings(guild.id)
        if settings and settings["announce_channel_id"]:
            ch = guild.get_channel(settings["announce_channel_id"])
            if ch:
                embed = discord.Embed(
                    title="📢 新しい商品が追加されました！",
                    description=f"🏪 {shop['name']}\n\n📦 {name}\n\n📝\n{description}\n\n💰 {price:,} PAL"
                )
                await ch.send(embed=embed)
        return True, product_id

    async def start_purchase(self, interaction, product_id):
        async with self.lock(f"purchase:{interaction.user.id}:{product_id}"):
            async with self.pool.acquire() as con:
                row = await con.fetchrow(
                    """SELECT p.*,s.guild_id,s.owner_id,s.name shop_name,s.status shop_status,
                              s.forum_thread_id
                       FROM shop.products p JOIN shop.shops s ON s.shop_id=p.shop_id
                       WHERE p.product_id=$1""", product_id
                )
                if not row or row["status"] != "active" or row["shop_status"] != "active":
                    return False, "INVALID_STATE"
                seller = interaction.guild.get_member(row["owner_id"]) if row["owner_id"] else None
                if row["owner_id"] and not seller:
                    return False, "SELLER_NOT_FOUND"
                txid = await con.fetchval(
                    """INSERT INTO shop.transactions(
                       guild_id,shop_id,product_id,buyer_id,seller_id,
                       shop_name_snapshot,product_name_snapshot,product_description_snapshot,price_snapshot)
                       VALUES($1,$2,$3,$4,$5,$6,$7,$8,$9) RETURNING transaction_id""",
                    interaction.guild.id,row["shop_id"],product_id,interaction.user.id,row["owner_id"],
                    row["shop_name"],row["name"],row["description"],row["price"]
                )
            bank = await self.bank.reserve_purchase(txid, interaction.user.id, row["owner_id"], row["price"])
            if bank.get("status") not in ("SUCCESS", "ALREADY_PROCESSED"):
                async with self.pool.acquire() as con:
                    await con.execute("UPDATE shop.transactions SET status='CANCELLED' WHERE transaction_id=$1", txid)
                return False, bank.get("status", "FAILED")
            return await self.create_ticket(interaction.guild, txid)

    async def create_ticket(self, guild, txid):
        settings = await self.get_settings(guild.id)
        category = guild.get_channel(settings["ticket_category_id"]) if settings and settings["ticket_category_id"] else None
        async with self.pool.acquire() as con:
            tx = await con.fetchrow("SELECT * FROM shop.transactions WHERE transaction_id=$1", txid)
        buyer = guild.get_member(tx["buyer_id"])
        seller = guild.get_member(tx["seller_id"]) if tx["seller_id"] else None
        overwrites = {guild.default_role: discord.PermissionOverwrite(view_channel=False)}
        overwrites[buyer] = discord.PermissionOverwrite(view_channel=True, send_messages=True)
        if seller:
            overwrites[seller] = discord.PermissionOverwrite(view_channel=True, send_messages=True)
        if settings and settings["staff_role_id"]:
            role = guild.get_role(settings["staff_role_id"])
            if role:
                overwrites[role] = discord.PermissionOverwrite(view_channel=True, send_messages=True)
        channel = await guild.create_text_channel(
            f"ticket-{txid:06d}", category=category, overwrites=overwrites
        )
        from views import TransactionView
        embed = self.transaction_embed(tx)
        msg = await channel.send(embed=embed, view=TransactionView(txid))
        async with self.pool.acquire() as con:
            await con.execute(
                """UPDATE shop.transactions SET status='SELLER_ACTION_REQUIRED',
                   ticket_channel_id=$1,deadline=$2,updated_at=NOW() WHERE transaction_id=$3""",
                channel.id, datetime.now(timezone.utc)+timedelta(hours=72), txid
            )
        return True, txid

    def transaction_embed(self, tx):
        return discord.Embed(
            title=f"🎫 取引チケット #{tx['transaction_id']:06d}",
            description=(
                f"🏪 店舗\n{tx['shop_name_snapshot']}\n\n"
                f"👤 店主\n<@{tx['seller_id']}>\n\n"
                f"🛒 購入者\n<@{tx['buyer_id']}>\n\n"
                f"📦 商品\n{tx['product_name_snapshot']}\n\n"
                f"💰 価格\n{tx['price_snapshot']:,} PAL\n\n"
                f"📌 取引状態\n🟡 店主の対応待ち"
            )
        )

    async def seller_delivered(self, txid, user_id):
        async with self.lock(f"tx:{txid}"):
            async with self.pool.acquire() as con:
                tx = await con.fetchrow("SELECT * FROM shop.transactions WHERE transaction_id=$1 FOR UPDATE", txid)
                if not tx or tx["seller_id"] != user_id or tx["status"] != "SELLER_ACTION_REQUIRED":
                    return False
                await con.execute(
                    "UPDATE shop.transactions SET status='BUYER_CONFIRMATION_REQUIRED',updated_at=NOW() WHERE transaction_id=$1", txid
                )
            return True

    async def buyer_received(self, txid, user_id):
        async with self.lock(f"tx:{txid}"):
            async with self.pool.acquire() as con:
                tx = await con.fetchrow("SELECT * FROM shop.transactions WHERE transaction_id=$1", txid)
                if not tx or tx["buyer_id"] != user_id or tx["status"] != "BUYER_CONFIRMATION_REQUIRED":
                    return False, "INVALID_STATE"
            bank = await self.bank.complete_transaction(txid)
            if bank.get("status") not in ("SUCCESS","ALREADY_PROCESSED"):
                return False, bank.get("status","FAILED")
            async with self.pool.acquire() as con:
                await con.execute("UPDATE shop.transactions SET status='COMPLETED',updated_at=NOW() WHERE transaction_id=$1", txid)
            await self.delete_ticket_later(tx["ticket_channel_id"])
            return True, "COMPLETED"

    async def report_problem(self, txid, user_id):
        async with self.pool.acquire() as con:
            tx = await con.fetchrow("SELECT * FROM shop.transactions WHERE transaction_id=$1", txid)
            if not tx or user_id not in (tx["buyer_id"], tx["seller_id"]):
                return False
            await con.execute("UPDATE shop.transactions SET status='STAFF_REVIEW',updated_at=NOW() WHERE transaction_id=$1", txid)
        return True

    async def delete_ticket_later(self, channel_id):
        await asyncio.sleep(600)
        ch = self.bot.get_channel(channel_id)
        if ch:
            await ch.delete(reason="SHOP transaction completed")

    async def handle_owner_left(self, guild_id, owner_id):
        async with self.pool.acquire() as con:
            shops = await con.fetch(
                "SELECT * FROM shop.shops WHERE guild_id=$1 AND owner_id=$2 AND status<>'deleted'", guild_id, owner_id
            )
            for shop in shops:
                await con.execute("UPDATE shop.shops SET status='deleted',delete_reason='OWNER_LEFT_GUILD' WHERE shop_id=$1", shop["shop_id"])
                await con.execute("UPDATE shop.products SET status='deleted' WHERE shop_id=$1", shop["shop_id"])
                await con.execute(
                    """UPDATE shop.transactions SET status='STAFF_REVIEW'
                       WHERE shop_id=$1 AND status NOT IN ('COMPLETED','REFUNDED','CANCELLED')""", shop["shop_id"]
                )
                guild = self.bot.get_guild(guild_id)
                thread = guild.get_thread(shop["forum_thread_id"]) if guild and shop["forum_thread_id"] else None
                if thread:
                    await thread.delete()

    async def restore_persistent_views(self):
        from views import StoreView, TransactionView
        async with self.pool.acquire() as con:
            shops = await con.fetch("SELECT shop_id FROM shop.shops WHERE status IN ('active','paused')")
            txs = await con.fetch(
                """SELECT transaction_id FROM shop.transactions
                   WHERE status NOT IN ('COMPLETED','REFUNDED','CANCELLED')"""
            )
        for row in shops:
            self.bot.add_view(StoreView(row["shop_id"]))
        for row in txs:
            self.bot.add_view(TransactionView(row["transaction_id"]))
