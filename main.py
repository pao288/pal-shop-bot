import os
import asyncio
import logging
import discord
from discord.ext import commands
import shop_db as db

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("pal_shop")

intents = discord.Intents.default()
intents.message_content = True
intents.members = True

bot = commands.Bot(command_prefix="!", intents=intents, help_command=None)

def fmt_money(amount, currency):
    return f"{int(amount):,} {currency}"

def is_admin(member):
    return member.guild_permissions.administrator

async def account_row(con, user_id, currency, lock=False):
    suffix = " FOR UPDATE" if lock else ""
    candidates = [
        f"""SELECT account_id,balance FROM bank.accounts
            WHERE owner_id=$1 AND currency=$2 AND account_type='USER'{suffix}""",
        f"""SELECT account_id,balance FROM bank.bank_accounts
            WHERE owner_id=$1 AND currency=$2 AND account_type='USER'{suffix}""",
    ]
    last = None
    for q in candidates:
        try:
            return await con.fetchrow(q, user_id, currency)
        except Exception as e:
            last = e
    raise last

async def change_balance(con, account_id, delta):
    candidates = [
        "UPDATE bank.accounts SET balance=balance+$2,updated_at=NOW() WHERE account_id=$1",
        "UPDATE bank.bank_accounts SET balance=balance+$2,updated_at=NOW() WHERE account_id=$1",
    ]
    last = None
    for q in candidates:
        try:
            await con.execute(q, account_id, delta)
            return
        except Exception as e:
            last = e
    raise last

async def reserve_funds(txid, buyer_id, seller_id, currency, amount):
    async with db.POOL.acquire() as con:
        async with con.transaction():
            escrow = await con.fetchrow("SELECT * FROM shop.escrows WHERE transaction_id=$1 FOR UPDATE", txid)
            if escrow:
                return escrow["status"] == "HELD", escrow["status"]
            acct = await account_row(con, buyer_id, currency, True)
            if not acct or acct["balance"] < amount:
                return False, "INSUFFICIENT_BALANCE"
            await change_balance(con, acct["account_id"], -amount)
            await con.execute("""INSERT INTO shop.escrows(transaction_id,buyer_id,seller_id,currency,amount)
                                 VALUES($1,$2,$3,$4,$5)""",
                              txid,buyer_id,seller_id,currency,amount)
    return True, "SUCCESS"

async def release_funds(txid):
    async with db.POOL.acquire() as con:
        async with con.transaction():
            e = await con.fetchrow("SELECT * FROM shop.escrows WHERE transaction_id=$1 FOR UPDATE", txid)
            if not e: return False, "ESCROW_NOT_FOUND"
            if e["status"] == "RELEASED": return True, "ALREADY_PROCESSED"
            if e["status"] != "HELD": return False, "INVALID_STATE"
            if e["seller_id"] is None: return False, "SELLER_NOT_FOUND"
            acct = await account_row(con, e["seller_id"], e["currency"], True)
            if not acct: return False, "SELLER_ACCOUNT_NOT_FOUND"
            await change_balance(con, acct["account_id"], e["amount"])
            await con.execute("""UPDATE shop.escrows SET status='RELEASED',released_at=NOW()
                                 WHERE transaction_id=$1""", txid)
    return True, "SUCCESS"

async def refund_funds(txid):
    async with db.POOL.acquire() as con:
        async with con.transaction():
            e = await con.fetchrow("SELECT * FROM shop.escrows WHERE transaction_id=$1 FOR UPDATE", txid)
            if not e: return False, "ESCROW_NOT_FOUND"
            if e["status"] == "REFUNDED": return True, "ALREADY_PROCESSED"
            if e["status"] != "HELD": return False, "INVALID_STATE"
            acct = await account_row(con, e["buyer_id"], e["currency"], True)
            if not acct: return False, "BUYER_ACCOUNT_NOT_FOUND"
            await change_balance(con, acct["account_id"], e["amount"])
            await con.execute("""UPDATE shop.escrows SET status='REFUNDED',refunded_at=NOW()
                                 WHERE transaction_id=$1""", txid)
    return True, "SUCCESS"

def system_embed(system):
    installed = bool(system and system["status"] == "ACTIVE")
    return discord.Embed(
        title="🏪 PAL SHOP SYSTEM",
        description=(
            "PAL SHOP / CASINO SHOP の設置・管理を行います。\n\n"
            f"🏪 PAL SHOP\n{'🟢 稼働中' if installed else '🔴 未設置'}\n\n"
            f"🎰 CASINO SHOP\n{'🟢 稼働中' if installed else '🔴 未設置'}\n\n"
            f"🎫 TICKET SYSTEM\n{'🟢 稼働中' if installed else '🔴 未設置'}"
        )
    )

def pal_open_embed():
    return discord.Embed(
        title="🏪 PAL SHOP",
        description=(
            "PALで商品を販売できるユーザーマーケットです。\n\n"
            "🏪 お店を開く\n店名・説明・最初の商品を登録します。\n\n"
            "📦 自分のお店\n自分の店舗を確認・管理します。"
        )
    )

def casino_embed():
    return discord.Embed(
        title="🎰 PAL CASINO SHOP",
        description=(
            "CHIPで商品を購入できる公式カジノショップです。\n\n"
            "🎰 決済通貨\nCHIP\n\n"
            "📦 商品を見る\n販売中の商品を確認できます。"
        )
    )

def store_embed(shop, count=0):
    state = "🟢 営業中" if shop["status"] == "ACTIVE" else "🔴 閉店中"
    return discord.Embed(
        title=f"🏪 {shop['name']}",
        description=(
            f"👤 店主\n<@{shop['owner_id']}>\n\n"
            f"📝 店舗説明\n{shop['description']}\n\n"
            f"📦 商品\n{count}件\n\n{state}"
        )
    )

async def create_system(guild):
    current = await db.fetchrow("SELECT * FROM shop.systems WHERE guild_id=$1", guild.id)
    if current and current["status"] == "ACTIVE":
        return current

    pal_cat = await guild.create_category("🏪 PAL SHOP")
    pal_open = await guild.create_text_channel("🛍️｜ショップを開く", category=pal_cat)
    pal_announce = await guild.create_text_channel("📢｜商品追加", category=pal_cat)
    pal_forum = await guild.create_forum("🏪｜PALショップ", category=pal_cat)

    casino_cat = await guild.create_category("🎰 CASINO SHOP")
    casino_channel = await guild.create_text_channel("🛒｜カジノショップ", category=casino_cat)
    casino_announce = await guild.create_text_channel("📢｜カジノ商品追加", category=casino_cat)

    hidden = {
        guild.default_role: discord.PermissionOverwrite(view_channel=False),
        guild.me: discord.PermissionOverwrite(view_channel=True, send_messages=True, manage_channels=True)
    }
    pal_ticket = await guild.create_category("🎫 PAL SHOP TICKET", overwrites=hidden)
    casino_ticket = await guild.create_category("🎫 CASINO SHOP TICKET", overwrites=hidden)

    casino_shop = await db.fetchrow("""INSERT INTO shop.shops(
        guild_id,shop_type,owner_id,owner_type,is_official,name,description,status)
        VALUES($1,'CASINO',$2,'BOT',TRUE,'PAL CASINO SHOP',
        'CHIPで商品を購入できる公式カジノショップです。','ACTIVE')
        RETURNING *""", guild.id, bot.user.id)

    await db.execute("""INSERT INTO shop.systems(
        guild_id,status,pal_category_id,pal_open_channel_id,pal_announce_channel_id,
        pal_forum_channel_id,casino_category_id,casino_channel_id,casino_announce_channel_id,
        pal_ticket_category_id,casino_ticket_category_id,casino_shop_id)
        VALUES($1,'ACTIVE',$2,$3,$4,$5,$6,$7,$8,$9,$10,$11)
        ON CONFLICT(guild_id) DO UPDATE SET
        status='ACTIVE',pal_category_id=EXCLUDED.pal_category_id,
        pal_open_channel_id=EXCLUDED.pal_open_channel_id,
        pal_announce_channel_id=EXCLUDED.pal_announce_channel_id,
        pal_forum_channel_id=EXCLUDED.pal_forum_channel_id,
        casino_category_id=EXCLUDED.casino_category_id,
        casino_channel_id=EXCLUDED.casino_channel_id,
        casino_announce_channel_id=EXCLUDED.casino_announce_channel_id,
        pal_ticket_category_id=EXCLUDED.pal_ticket_category_id,
        casino_ticket_category_id=EXCLUDED.casino_ticket_category_id,
        casino_shop_id=EXCLUDED.casino_shop_id,updated_at=NOW()""",
        guild.id,pal_cat.id,pal_open.id,pal_announce.id,pal_forum.id,
        casino_cat.id,casino_channel.id,casino_announce.id,pal_ticket.id,
        casino_ticket.id,casino_shop["shop_id"])

    await pal_open.send(embed=pal_open_embed(), view=OpenShopPanel())
    await casino_channel.send(embed=casino_embed(), view=CasinoShopView())
    return await db.fetchrow("SELECT * FROM shop.systems WHERE guild_id=$1", guild.id)

async def delete_system(guild):
    system = await db.fetchrow("SELECT * FROM shop.systems WHERE guild_id=$1", guild.id)
    if system:
        ids = [
            system["pal_open_channel_id"], system["pal_announce_channel_id"],
            system["pal_forum_channel_id"], system["casino_channel_id"],
            system["casino_announce_channel_id"], system["pal_ticket_category_id"],
            system["casino_ticket_category_id"], system["pal_category_id"],
            system["casino_category_id"]
        ]
        for cid in ids:
            if cid:
                ch = guild.get_channel(cid)
                if ch:
                    try: await ch.delete(reason="PAL SHOP SYSTEM 全削除")
                    except discord.HTTPException: pass

    async with db.POOL.acquire() as con:
        async with con.transaction():
            txs = await con.fetch("""SELECT transaction_id FROM shop.transactions
                                     WHERE guild_id=$1 AND status NOT IN ('COMPLETED','REFUNDED','CANCELLED')""", guild.id)
            for tx in txs:
                e = await con.fetchrow("SELECT * FROM shop.escrows WHERE transaction_id=$1 FOR UPDATE", tx["transaction_id"])
                if e and e["status"] == "HELD":
                    acct = await account_row(con, e["buyer_id"], e["currency"], True)
                    if acct:
                        await change_balance(con, acct["account_id"], e["amount"])
                        await con.execute("""UPDATE shop.escrows SET status='REFUNDED',refunded_at=NOW()
                                             WHERE transaction_id=$1""", tx["transaction_id"])
            await con.execute("DELETE FROM shop.transactions WHERE guild_id=$1", guild.id)
            await con.execute("""DELETE FROM shop.escrows e WHERE NOT EXISTS(
                SELECT 1 FROM shop.transactions t WHERE t.transaction_id=e.transaction_id)""")
            await con.execute("DELETE FROM shop.shops WHERE guild_id=$1", guild.id)
            await con.execute("DELETE FROM shop.systems WHERE guild_id=$1", guild.id)

class SetupView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    async def interaction_check(self, i):
        if not is_admin(i.user):
            await i.response.send_message("管理者専用です。", ephemeral=True)
            return False
        return True

    @discord.ui.button(label="SHOP SYSTEMを作成", emoji="🏗️", style=discord.ButtonStyle.success, custom_id="shop:system:create")
    async def create(self, i, b):
        await i.response.defer(ephemeral=True)
        try:
            await create_system(i.guild)
            await i.message.edit(embed=system_embed(await db.fetchrow("SELECT * FROM shop.systems WHERE guild_id=$1", i.guild_id)), view=self)
            await i.followup.send("🏗️ PAL SHOP / CASINO SHOP / TICKET SYSTEMを作成しました。", ephemeral=True)
        except Exception as e:
            await i.followup.send(f"作成エラー: `{type(e).__name__}: {e}`", ephemeral=True)

    @discord.ui.button(label="CASINO SHOP管理", emoji="🎰", style=discord.ButtonStyle.primary, custom_id="shop:casino:admin")
    async def casino_admin(self, i, b):
        system = await db.fetchrow("SELECT * FROM shop.systems WHERE guild_id=$1 AND status='ACTIVE'", i.guild_id)
        if not system:
            return await i.response.send_message("先にSHOP SYSTEMを作成してください。", ephemeral=True)
        await i.response.send_message("🎰 CASINO SHOP ADMIN", view=CasinoAdminView(system["casino_shop_id"]), ephemeral=True)

    @discord.ui.button(label="SHOP SYSTEMを削除", emoji="🗑️", style=discord.ButtonStyle.danger, custom_id="shop:system:delete")
    async def delete(self, i, b):
        await i.response.send_message(
            "⚠️ SHOP SYSTEMを全削除します。\nPAL店舗・商品・SHOP投稿・CASINO SHOP・取引チケットも対象です。",
            view=DeleteConfirmView(), ephemeral=True
        )

class DeleteConfirmView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=120)

    @discord.ui.button(label="全削除する", emoji="🗑️", style=discord.ButtonStyle.danger)
    async def confirm(self, i, b):
        await i.response.defer(ephemeral=True)
        try:
            await delete_system(i.guild)
            await i.followup.send("🗑️ SHOP SYSTEMを全削除しました。", ephemeral=True)
        except Exception as e:
            await i.followup.send(f"削除エラー: `{type(e).__name__}: {e}`", ephemeral=True)

class OpenShopModal(discord.ui.Modal, title="🏪 お店を開く"):
    shop_name = discord.ui.TextInput(label="店名", max_length=80)
    shop_description = discord.ui.TextInput(label="店の説明", style=discord.TextStyle.paragraph, max_length=1000)
    product_name = discord.ui.TextInput(label="最初の商品名", max_length=100)
    product_description = discord.ui.TextInput(label="商品説明", style=discord.TextStyle.paragraph, max_length=1000)
    product_price = discord.ui.TextInput(label="価格 PAL", placeholder="50000", max_length=18)

    async def on_submit(self, i):
        try:
            price = int(self.product_price.value.replace(",", "").strip())
            if price < 1: raise ValueError
        except ValueError:
            return await i.response.send_message("価格は1 PAL以上の整数で入力してください。", ephemeral=True)
        await i.response.defer(ephemeral=True)
        exists = await db.fetchrow("""SELECT 1 FROM shop.shops WHERE guild_id=$1 AND owner_id=$2
                                      AND shop_type='PAL' AND status<>'DELETED'""", i.guild_id, i.user.id)
        if exists:
            return await i.followup.send("すでにPAL店舗を持っています。", ephemeral=True)
        system = await db.fetchrow("SELECT * FROM shop.systems WHERE guild_id=$1 AND status='ACTIVE'", i.guild_id)
        if not system:
            return await i.followup.send("SHOP SYSTEMが未設置です。", ephemeral=True)
        shop = await db.fetchrow("""INSERT INTO shop.shops(
            guild_id,shop_type,owner_id,owner_type,is_official,name,description,status)
            VALUES($1,'PAL',$2,'USER',FALSE,$3,$4,'ACTIVE') RETURNING *""",
            i.guild_id,i.user.id,self.shop_name.value,self.shop_description.value)
        await db.execute("""INSERT INTO shop.products(shop_id,name,description,price,currency)
                            VALUES($1,$2,$3,$4,'PAL')""",
                         shop["shop_id"],self.product_name.value,self.product_description.value,price)
        forum = i.guild.get_channel(system["pal_forum_channel_id"])
        count = 1
        thread, msg = await forum.create_thread(
            name=f"🏪 {shop['name']}",
            embed=store_embed(shop,count),
            view=StoreView(shop["shop_id"])
        )
        await db.execute("""UPDATE shop.shops SET forum_thread_id=$2,panel_message_id=$3,updated_at=NOW()
                            WHERE shop_id=$1""", shop["shop_id"],thread.id,msg.id)
        await i.followup.send(f"🏪 **{shop['name']}** を開店しました！", ephemeral=True)

class OpenShopPanel(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="お店を開く", emoji="🏪", style=discord.ButtonStyle.primary, custom_id="shop:pal:open")
    async def open(self, i, b):
        await i.response.send_modal(OpenShopModal())

    @discord.ui.button(label="自分のお店", emoji="📦", style=discord.ButtonStyle.secondary, custom_id="shop:pal:mine")
    async def mine(self, i, b):
        shop = await db.fetchrow("""SELECT * FROM shop.shops WHERE guild_id=$1 AND owner_id=$2
                                    AND shop_type='PAL' AND status<>'DELETED'""",i.guild_id,i.user.id)
        if not shop:
            return await i.response.send_message("まだPAL店舗を持っていません。", ephemeral=True)
        thread = i.guild.get_channel(shop["forum_thread_id"]) if shop["forum_thread_id"] else None
        await i.response.send_message(
            f"🏪 **{shop['name']}**\n状態: **{shop['status']}**" + (f"\n{thread.mention}" if thread else ""),
            view=StoreManageView(shop["shop_id"]), ephemeral=True
        )

class AddProductModal(discord.ui.Modal):
    name = discord.ui.TextInput(label="商品名", max_length=100)
    description = discord.ui.TextInput(label="商品説明", style=discord.TextStyle.paragraph, max_length=1000)
    price = discord.ui.TextInput(label="価格", placeholder="50000", max_length=18)

    def __init__(self, shop_id, currency):
        super().__init__(title=f"📦 {currency}商品追加")
        self.shop_id = int(shop_id)
        self.currency = currency

    async def on_submit(self, i):
        try:
            price = int(self.price.value.replace(",", "").strip())
            if price < 1: raise ValueError
        except ValueError:
            return await i.response.send_message("価格は1以上の整数で入力してください。", ephemeral=True)
        p = await db.fetchrow("""INSERT INTO shop.products(shop_id,name,description,price,currency)
                                 VALUES($1,$2,$3,$4,$5) RETURNING *""",
                              self.shop_id,self.name.value,self.description.value,price,self.currency)
        shop = await db.fetchrow("SELECT * FROM shop.shops WHERE shop_id=$1", self.shop_id)
        system = await db.fetchrow("SELECT * FROM shop.systems WHERE guild_id=$1", i.guild_id)
        announce_id = system["pal_announce_channel_id"] if self.currency=="PAL" else system["casino_announce_channel_id"]
        ch = i.guild.get_channel(announce_id)
        if ch:
            await ch.send(embed=discord.Embed(
                title="📢 新しい商品が追加されました！",
                description=f"🏪 {shop['name']}\n\n📦 {p['name']}\n\n📝\n{p['description']}\n\n💰 {fmt_money(p['price'],p['currency'])}"
            ))
        await refresh_store(i.guild, self.shop_id)
        await i.response.send_message(f"📦 **{p['name']}** を追加しました。", ephemeral=True)

class StoreView(discord.ui.View):
    def __init__(self, shop_id):
        super().__init__(timeout=None)
        self.shop_id = int(shop_id)
        self.children[0].custom_id=f"shop:store:products:{self.shop_id}"
        self.children[1].custom_id=f"shop:store:info:{self.shop_id}"
        self.children[2].custom_id=f"shop:store:manage:{self.shop_id}"

    @discord.ui.button(label="商品一覧", emoji="📦", style=discord.ButtonStyle.primary, custom_id="shop:store:products")
    async def products(self, i, b):
        await show_products(i,self.shop_id)

    @discord.ui.button(label="店舗情報", emoji="ℹ️", style=discord.ButtonStyle.secondary, custom_id="shop:store:info")
    async def info(self, i, b):
        s=await db.fetchrow("SELECT * FROM shop.shops WHERE shop_id=$1",self.shop_id)
        await i.response.send_message(f"🏪 **{s['name']}**\n\n{s['description']}\n\n👤 <@{s['owner_id']}>",ephemeral=True)

    @discord.ui.button(label="店舗管理", emoji="⚙️", style=discord.ButtonStyle.secondary, custom_id="shop:store:manage")
    async def manage(self, i, b):
        s=await db.fetchrow("SELECT * FROM shop.shops WHERE shop_id=$1",self.shop_id)
        if i.user.id != s["owner_id"] and not is_admin(i.user):
            return await i.response.send_message("店主用です。",ephemeral=True)
        await i.response.send_message("⚙️ 店舗管理",view=StoreManageView(self.shop_id),ephemeral=True)

class StoreManageView(discord.ui.View):
    def __init__(self,shop_id):
        super().__init__(timeout=300)
        self.shop_id=int(shop_id)

    @discord.ui.button(label="商品追加",emoji="➕",style=discord.ButtonStyle.success)
    async def add(self,i,b):
        s=await db.fetchrow("SELECT * FROM shop.shops WHERE shop_id=$1",self.shop_id)
        if i.user.id != s["owner_id"] and not is_admin(i.user):
            return await i.response.send_message("店主用です。",ephemeral=True)
        await i.response.send_modal(AddProductModal(self.shop_id,"PAL"))

    @discord.ui.button(label="商品管理",emoji="📦",style=discord.ButtonStyle.primary)
    async def products(self,i,b):
        await show_product_admin(i,self.shop_id)

    @discord.ui.button(label="閉店 / 営業再開",emoji="🔁",style=discord.ButtonStyle.secondary)
    async def toggle(self,i,b):
        s=await db.fetchrow("SELECT * FROM shop.shops WHERE shop_id=$1",self.shop_id)
        if i.user.id != s["owner_id"] and not is_admin(i.user):
            return await i.response.send_message("店主用です。",ephemeral=True)
        new="CLOSED" if s["status"]=="ACTIVE" else "ACTIVE"
        await db.execute("UPDATE shop.shops SET status=$2,updated_at=NOW() WHERE shop_id=$1",self.shop_id,new)
        await refresh_store(i.guild,self.shop_id)
        await i.response.send_message(f"店舗状態: **{new}**",ephemeral=True)

class CasinoShopView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="商品を見る",emoji="📦",style=discord.ButtonStyle.primary,custom_id="shop:casino:products")
    async def products(self,i,b):
        system=await db.fetchrow("SELECT * FROM shop.systems WHERE guild_id=$1",i.guild_id)
        await show_products(i,system["casino_shop_id"])

class CasinoAdminView(discord.ui.View):
    def __init__(self,shop_id):
        super().__init__(timeout=300)
        self.shop_id=int(shop_id)

    @discord.ui.button(label="商品追加",emoji="➕",style=discord.ButtonStyle.success)
    async def add(self,i,b):
        await i.response.send_modal(AddProductModal(self.shop_id,"CHIP"))

    @discord.ui.button(label="商品管理",emoji="📦",style=discord.ButtonStyle.primary)
    async def products(self,i,b):
        await show_product_admin(i,self.shop_id)

    @discord.ui.button(label="SHOP休止 / 営業",emoji="🔁",style=discord.ButtonStyle.secondary)
    async def toggle(self,i,b):
        s=await db.fetchrow("SELECT * FROM shop.shops WHERE shop_id=$1",self.shop_id)
        new="CLOSED" if s["status"]=="ACTIVE" else "ACTIVE"
        await db.execute("UPDATE shop.shops SET status=$2,updated_at=NOW() WHERE shop_id=$1",self.shop_id,new)
        await i.response.send_message(f"CASINO SHOP状態: **{new}**",ephemeral=True)

async def show_products(i,shop_id):
    shop=await db.fetchrow("SELECT * FROM shop.shops WHERE shop_id=$1",shop_id)
    if not shop or shop["status"]!="ACTIVE":
        return await i.response.send_message("現在このSHOPは休止中です。",ephemeral=True)
    rows=await db.fetch("""SELECT * FROM shop.products WHERE shop_id=$1 AND status='ACTIVE'
                           ORDER BY product_id LIMIT 25""",shop_id)
    if not rows:return await i.response.send_message("販売中の商品はありません。",ephemeral=True)
    await i.response.send_message("📦 商品を選択してください。",view=ProductSelectView(rows),ephemeral=True)

class ProductSelect(discord.ui.Select):
    def __init__(self,rows):
        opts=[discord.SelectOption(label=r["name"][:100],description=fmt_money(r["price"],r["currency"]),value=str(r["product_id"])) for r in rows]
        super().__init__(placeholder="商品を選択",options=opts)

    async def callback(self,i):
        p=await db.fetchrow("""SELECT p.*,s.name shop_name,s.owner_id,s.status shop_status
                               FROM shop.products p JOIN shop.shops s ON s.shop_id=p.shop_id
                               WHERE p.product_id=$1""",int(self.values[0]))
        e=discord.Embed(title=f"📦 {p['name']}",description=p["description"])
        e.add_field(name="💰 価格",value=fmt_money(p["price"],p["currency"]))
        await i.response.send_message(embed=e,view=BuyView(p["product_id"]),ephemeral=True)

class ProductSelectView(discord.ui.View):
    def __init__(self,rows):
        super().__init__(timeout=300)
        self.add_item(ProductSelect(rows))

class BuyView(discord.ui.View):
    def __init__(self,product_id):
        super().__init__(timeout=300)
        self.product_id=int(product_id)

    @discord.ui.button(label="購入する",emoji="🛒",style=discord.ButtonStyle.success)
    async def buy(self,i,b):
        await i.response.defer(ephemeral=True)
        p=await db.fetchrow("""SELECT p.*,s.guild_id,s.owner_id,s.name shop_name,s.status shop_status
                               FROM shop.products p JOIN shop.shops s ON s.shop_id=p.shop_id
                               WHERE p.product_id=$1""",self.product_id)
        if not p or p["status"]!="ACTIVE" or p["shop_status"]!="ACTIVE":
            return await i.followup.send("現在購入できません。",ephemeral=True)
        if p["owner_id"] == i.user.id:
            return await i.followup.send("自分の商品です。",ephemeral=True)
        tx=await db.fetchrow("""INSERT INTO shop.transactions(
            guild_id,shop_id,product_id,buyer_id,seller_id,currency,
            shop_name_snapshot,product_name_snapshot,product_description_snapshot,price_snapshot,status)
            VALUES($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,'PAYMENT_PENDING') RETURNING *""",
            i.guild_id,p["shop_id"],p["product_id"],i.user.id,p["owner_id"],p["currency"],
            p["shop_name"],p["name"],p["description"],p["price"])
        ok,msg=await reserve_funds(tx["transaction_id"],i.user.id,p["owner_id"],p["currency"],p["price"])
        if not ok:
            await db.execute("UPDATE shop.transactions SET status='CANCELLED' WHERE transaction_id=$1",tx["transaction_id"])
            return await i.followup.send(f"購入結果: `{msg}`",ephemeral=True)
        await db.execute("""UPDATE shop.transactions SET status='SELLER_ACTION_REQUIRED',updated_at=NOW()
                            WHERE transaction_id=$1""",tx["transaction_id"])
        tx=await db.fetchrow("SELECT * FROM shop.transactions WHERE transaction_id=$1",tx["transaction_id"])
        ch=await create_ticket(i.guild,tx)
        await db.execute("UPDATE shop.transactions SET ticket_channel_id=$2 WHERE transaction_id=$1",tx["transaction_id"],ch.id)
        await i.followup.send(f"🎫 取引チケット: {ch.mention}",ephemeral=True)

async def create_ticket(guild,tx):
    system=await db.fetchrow("SELECT * FROM shop.systems WHERE guild_id=$1",guild.id)
    cat_id=system["pal_ticket_category_id"] if tx["currency"]=="PAL" else system["casino_ticket_category_id"]
    cat=guild.get_channel(cat_id)
    overwrites={guild.default_role:discord.PermissionOverwrite(view_channel=False)}
    buyer=guild.get_member(tx["buyer_id"])
    seller=guild.get_member(tx["seller_id"]) if tx["seller_id"] else None
    if buyer:overwrites[buyer]=discord.PermissionOverwrite(view_channel=True,send_messages=True,read_message_history=True)
    if seller:overwrites[seller]=discord.PermissionOverwrite(view_channel=True,send_messages=True,read_message_history=True)
    for role in guild.roles:
        if role.permissions.administrator:
            overwrites[role]=discord.PermissionOverwrite(view_channel=True,send_messages=True,read_message_history=True)
    overwrites[guild.me]=discord.PermissionOverwrite(view_channel=True,send_messages=True,manage_channels=True)
    ch=await guild.create_text_channel(f"取引-{tx['transaction_id']:06d}",category=cat,overwrites=overwrites)
    await ch.send(embed=ticket_embed(tx),view=TicketView(tx["transaction_id"]))
    return ch

def ticket_embed(tx):
    labels={"SELLER_ACTION_REQUIRED":"🟡 店主の対応待ち","BUYER_CONFIRMATION_REQUIRED":"🔵 購入者の確認待ち",
            "STAFF_REVIEW":"🚨 STAFF REVIEW","COMPLETED":"✅ 完了","REFUNDED":"↩️ 返金済み"}
    return discord.Embed(title=f"🎫 取引チケット #{tx['transaction_id']:06d}",description=(
        f"🏪 店舗\n{tx['shop_name_snapshot']}\n\n"
        f"👤 店主\n<@{tx['seller_id']}>\n\n"
        f"🛒 購入者\n<@{tx['buyer_id']}>\n\n"
        f"📦 商品\n{tx['product_name_snapshot']}\n\n"
        f"💰 価格\n{fmt_money(tx['price_snapshot'],tx['currency'])}\n\n"
        f"📌 取引状態\n{labels.get(tx['status'],tx['status'])}"
    ))

class TicketView(discord.ui.View):
    def __init__(self,txid):
        super().__init__(timeout=None)
        self.txid=int(txid)
        for n,c in enumerate(self.children):c.custom_id=f"shop:tx:{n}:{self.txid}"

    @discord.ui.button(label="商品を渡しました",emoji="📦",style=discord.ButtonStyle.primary,custom_id="shop:tx:delivered")
    async def delivered(self,i,b):
        tx=await db.fetchrow("SELECT * FROM shop.transactions WHERE transaction_id=$1",self.txid)
        if i.user.id!=tx["seller_id"]:return await i.response.send_message("店主用です。",ephemeral=True)
        if tx["status"]!="SELLER_ACTION_REQUIRED":return await i.response.send_message("現在この操作はできません。",ephemeral=True)
        await db.execute("UPDATE shop.transactions SET status='BUYER_CONFIRMATION_REQUIRED',updated_at=NOW() WHERE transaction_id=$1",self.txid)
        await refresh_ticket(i.channel,self.txid)
        await i.response.send_message("📦 購入者の受取確認待ちです。")

    @discord.ui.button(label="受け取りました",emoji="✅",style=discord.ButtonStyle.success,custom_id="shop:tx:received")
    async def received(self,i,b):
        tx=await db.fetchrow("SELECT * FROM shop.transactions WHERE transaction_id=$1",self.txid)
        if i.user.id!=tx["buyer_id"]:return await i.response.send_message("購入者用です。",ephemeral=True)
        if tx["status"]!="BUYER_CONFIRMATION_REQUIRED":return await i.response.send_message("店主の対応待ちです。",ephemeral=True)
        ok,msg=await release_funds(self.txid)
        if not ok:return await i.response.send_message(f"処理結果: `{msg}`",ephemeral=True)
        await db.execute("UPDATE shop.transactions SET status='COMPLETED',updated_at=NOW() WHERE transaction_id=$1",self.txid)
        await refresh_ticket(i.channel,self.txid)
        await i.response.send_message("✅ 取引完了！10分後にチケットを削除します。")
        await asyncio.sleep(600)
        try:await i.channel.delete(reason="SHOP取引完了")
        except discord.NotFound:pass

    @discord.ui.button(label="問題があります",emoji="⚠️",style=discord.ButtonStyle.danger,custom_id="shop:tx:problem")
    async def problem(self,i,b):
        tx=await db.fetchrow("SELECT * FROM shop.transactions WHERE transaction_id=$1",self.txid)
        if i.user.id not in (tx["buyer_id"],tx["seller_id"]):return await i.response.send_message("取引参加者用です。",ephemeral=True)
        await db.execute("UPDATE shop.transactions SET previous_status=status,status='STAFF_REVIEW',updated_at=NOW() WHERE transaction_id=$1",self.txid)
        await refresh_ticket(i.channel,self.txid)
        await i.response.send_message("🚨 STAFF REVIEWへ移行しました。")

    @discord.ui.button(label="取引キャンセル",emoji="❌",style=discord.ButtonStyle.secondary,custom_id="shop:tx:cancel")
    async def cancel(self,i,b):
        tx=await db.fetchrow("SELECT * FROM shop.transactions WHERE transaction_id=$1",self.txid)
        if i.user.id not in (tx["buyer_id"],tx["seller_id"]):return await i.response.send_message("取引参加者用です。",ephemeral=True)
        await i.response.send_message("キャンセル確認",view=CancelView(self.txid,i.user.id))

class CancelView(discord.ui.View):
    def __init__(self,txid,requester):
        super().__init__(timeout=300)
        self.txid,self.requester=int(txid),int(requester)

    @discord.ui.button(label="キャンセルに同意",style=discord.ButtonStyle.danger)
    async def agree(self,i,b):
        tx=await db.fetchrow("SELECT * FROM shop.transactions WHERE transaction_id=$1",self.txid)
        if i.user.id==self.requester or i.user.id not in (tx["buyer_id"],tx["seller_id"]):
            return await i.response.send_message("相手側が押してください。",ephemeral=True)
        ok,msg=await refund_funds(self.txid)
        if not ok:return await i.response.send_message(f"処理結果: `{msg}`",ephemeral=True)
        await db.execute("UPDATE shop.transactions SET status='REFUNDED',updated_at=NOW() WHERE transaction_id=$1",self.txid)
        await refresh_ticket(i.channel,self.txid)
        await i.response.send_message("↩️ 全額返金しました。10分後にチケットを削除します。")
        await asyncio.sleep(600)
        try:await i.channel.delete(reason="SHOP返金完了")
        except discord.NotFound:pass

    @discord.ui.button(label="取引を続ける",style=discord.ButtonStyle.primary)
    async def continue_(self,i,b):
        await i.response.send_message("🔁 取引を継続します。")

class ProductAdminSelect(discord.ui.Select):
    def __init__(self,rows):
        opts=[discord.SelectOption(label=r["name"][:100],description=f"{fmt_money(r['price'],r['currency'])} / {r['status']}",value=str(r["product_id"])) for r in rows]
        super().__init__(placeholder="管理する商品を選択",options=opts)

    async def callback(self,i):
        await i.response.send_message("📦 商品操作",view=ProductActionView(int(self.values[0])),ephemeral=True)

class ProductAdminView(discord.ui.View):
    def __init__(self,rows):
        super().__init__(timeout=300)
        self.add_item(ProductAdminSelect(rows))

class ProductActionView(discord.ui.View):
    def __init__(self,pid):
        super().__init__(timeout=300)
        self.pid=int(pid)

    @discord.ui.button(label="販売停止 / 再開",emoji="🔁",style=discord.ButtonStyle.secondary)
    async def toggle(self,i,b):
        p=await db.fetchrow("SELECT * FROM shop.products WHERE product_id=$1",self.pid)
        new="PAUSED" if p["status"]=="ACTIVE" else "ACTIVE"
        await db.execute("UPDATE shop.products SET status=$2,updated_at=NOW() WHERE product_id=$1",self.pid,new)
        await refresh_store(i.guild,p["shop_id"])
        await i.response.send_message(f"商品状態: **{new}**",ephemeral=True)

    @discord.ui.button(label="商品削除",emoji="🗑️",style=discord.ButtonStyle.danger)
    async def delete(self,i,b):
        p=await db.fetchrow("SELECT * FROM shop.products WHERE product_id=$1",self.pid)
        await db.execute("UPDATE shop.products SET status='DELETED',updated_at=NOW() WHERE product_id=$1",self.pid)
        await refresh_store(i.guild,p["shop_id"])
        await i.response.send_message("🗑️ 商品を削除しました。",ephemeral=True)

async def show_product_admin(i,shop_id):
    rows=await db.fetch("""SELECT * FROM shop.products WHERE shop_id=$1 AND status<>'DELETED'
                           ORDER BY product_id LIMIT 25""",shop_id)
    if not rows:return await i.response.send_message("商品はありません。",ephemeral=True)
    await i.response.send_message("📦 管理する商品を選択してください。",view=ProductAdminView(rows),ephemeral=True)

async def refresh_store(guild,shop_id):
    s=await db.fetchrow("SELECT * FROM shop.shops WHERE shop_id=$1",shop_id)
    if not s or s["shop_type"]!="PAL" or not s["forum_thread_id"] or not s["panel_message_id"]:return
    count=await db.fetchrow("""SELECT COUNT(*) c FROM shop.products WHERE shop_id=$1 AND status<>'DELETED'""",shop_id)
    thread=guild.get_channel(s["forum_thread_id"])
    if thread:
        try:
            m=await thread.fetch_message(s["panel_message_id"])
            await m.edit(embed=store_embed(s,count["c"]),view=StoreView(shop_id))
        except discord.NotFound:pass

async def refresh_ticket(channel,txid):
    tx=await db.fetchrow("SELECT * FROM shop.transactions WHERE transaction_id=$1",txid)
    async for m in channel.history(limit=30,oldest_first=True):
        if m.author.id==bot.user.id and m.embeds and (m.embeds[0].title or "").startswith("🎫 取引チケット"):
            await m.edit(embed=ticket_embed(tx),view=TicketView(txid))
            break

@bot.event
async def setup_hook():
    await db.init_db()
    bot.add_view(SetupView())
    bot.add_view(OpenShopPanel())
    bot.add_view(CasinoShopView())
    shops=await db.fetch("SELECT shop_id FROM shop.shops WHERE shop_type='PAL' AND status<>'DELETED'")
    for s in shops:bot.add_view(StoreView(s["shop_id"]))
    txs=await db.fetch("""SELECT transaction_id FROM shop.transactions
                          WHERE status NOT IN ('COMPLETED','REFUNDED','CANCELLED')""")
    for t in txs:bot.add_view(TicketView(t["transaction_id"]))
    log.info("DB・SHOP SYSTEM復旧完了")

@bot.event
async def on_ready():
    log.info("PAL SHOP起動完了: %s",bot.user)

@bot.command()
@commands.has_permissions(administrator=True)
async def shopsetup(ctx):
    system=await db.fetchrow("SELECT * FROM shop.systems WHERE guild_id=$1",ctx.guild.id)
    await ctx.send(embed=system_embed(system),view=SetupView())

bot.run(os.environ["DISCORD_TOKEN"])
