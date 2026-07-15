import os
import asyncio
import logging
from zoneinfo import ZoneInfo
from datetime import datetime, timezone, timedelta
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
            return await con.fetchrow(q, str(user_id), currency)
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
            "🏪 お店を開く\n店名・説明・最初の商品を登録します。"
        )
    )

async def casino_embed(guild_id):
    system = await db.fetchrow("SELECT * FROM shop.systems WHERE guild_id=$1", guild_id)
    shop = None
    rows = []
    if system and system["casino_shop_id"]:
        shop = await db.fetchrow("SELECT * FROM shop.shops WHERE shop_id=$1", system["casino_shop_id"])
        rows = await db.fetch("""SELECT * FROM shop.products
                                 WHERE shop_id=$1 AND status='ACTIVE'
                                 ORDER BY product_id DESC LIMIT 10""", system["casino_shop_id"])
    state = "🟢 営業中" if shop and shop["status"] == "ACTIVE" else "🔴 休止中"
    if rows:
        product_text = "\n\n".join(
            f"📦 **{r['name']}**\n{fmt_money(r['price'], r['currency'])}"
            for r in rows
        )
    else:
        product_text = "現在販売中の商品はありません。"
    return discord.Embed(
        title="🎰 PAL CASINO SHOP",
        description=(
            "CHIPで商品を購入できる公式カジノショップです。\n\n"
            f"📌 営業状態\n{state}\n\n"
            "🎰 決済通貨\nCHIP\n\n"
            f"📦 販売中の商品\n{product_text}"
        )
    )

def product_embed(product, shop):
    state = "🟢 販売中" if product["status"] == "ACTIVE" else "⏸️ 販売停止"
    e = discord.Embed(
        title=f"📦 {product['name']}",
        description=product["description"]
    )
    e.add_field(name="🏪 販売店舗", value=shop["name"], inline=False)
    e.add_field(name="💰 価格", value=fmt_money(product["price"], product["currency"]), inline=False)
    e.add_field(name="📌 商品状態", value=state, inline=False)
    return e

async def store_embed(shop,page=0):
    total=await db.fetchval("SELECT COUNT(*) FROM shop.products WHERE shop_id=$1 AND status='ACTIVE'",shop["shop_id"])
    pages=max(1,(total+9)//10); page=max(0,min(page,pages-1))
    rows=await db.fetch("""SELECT * FROM shop.products WHERE shop_id=$1 AND status='ACTIVE'
                           ORDER BY product_id ASC OFFSET $2 LIMIT 10""",shop["shop_id"],page*10)
    products="\n\n".join(f"📦 **{x['name']}**\n💰 {fmt_money(x['price'],x['currency'])}" for x in rows) if rows else "現在販売中の商品はありません。"
    return discord.Embed(title=f"🏪 {shop['name']}",description=(
        f"👤 店主\n<@{shop['owner_id']}>\n\n📝 店舗説明\n{shop['description']}\n\n"
        f"📦 販売中の商品\n{products}\n\n📄 {page+1} / {pages}ページ"))


async def create_system(guild):
    current=await db.fetchrow("SELECT * FROM shop.systems WHERE guild_id=$1",guild.id)
    if current and current["status"]=="ACTIVE": return current

    pal_cat=await guild.create_category("🏪 PAL SHOP")
    pal_open=await guild.create_text_channel("🛍️｜ショップを開く",category=pal_cat)
    pal_announce=await guild.create_text_channel("📢｜商品追加",category=pal_cat)
    pal_forum=await guild.create_forum("🏪｜PALショップ",category=pal_cat)
    auction_channel=await guild.create_text_channel("🔨｜PAL競り市場",category=pal_cat)

    casino_cat=await guild.create_category("🎰 CASINO SHOP")
    casino_channel=await guild.create_text_channel("🛒｜カジノショップ",category=casino_cat)
    casino_announce=await guild.create_text_channel("📢｜カジノ商品追加",category=casino_cat)

    hidden={guild.default_role:discord.PermissionOverwrite(view_channel=False),
            guild.me:discord.PermissionOverwrite(view_channel=True,send_messages=True,manage_channels=True)}
    pal_ticket=await guild.create_category("🎫 PAL SHOP TICKET",overwrites=hidden)
    casino_ticket=await guild.create_category("🎫 CASINO SHOP TICKET",overwrites=hidden)

    casino_shop=await db.fetchrow("""INSERT INTO shop.shops(
        guild_id,shop_type,owner_id,owner_type,is_official,name,description,status)
        VALUES($1,'CASINO',$2,'BOT',TRUE,'PAL CASINO SHOP',
        'CHIPで商品を購入できる公式カジノショップです。','ACTIVE') RETURNING *""",guild.id,bot.user.id)

    await db.execute("""INSERT INTO shop.systems(
        guild_id,status,pal_category_id,pal_open_channel_id,pal_announce_channel_id,
        pal_forum_channel_id,casino_category_id,casino_channel_id,casino_announce_channel_id,
        pal_ticket_category_id,casino_ticket_category_id,casino_shop_id,auction_channel_id)
        VALUES($1,'ACTIVE',$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12)
        ON CONFLICT(guild_id) DO UPDATE SET status='ACTIVE',
        pal_category_id=EXCLUDED.pal_category_id,pal_open_channel_id=EXCLUDED.pal_open_channel_id,
        pal_announce_channel_id=EXCLUDED.pal_announce_channel_id,pal_forum_channel_id=EXCLUDED.pal_forum_channel_id,
        casino_category_id=EXCLUDED.casino_category_id,casino_channel_id=EXCLUDED.casino_channel_id,
        casino_announce_channel_id=EXCLUDED.casino_announce_channel_id,
        pal_ticket_category_id=EXCLUDED.pal_ticket_category_id,casino_ticket_category_id=EXCLUDED.casino_ticket_category_id,
        casino_shop_id=EXCLUDED.casino_shop_id,auction_channel_id=EXCLUDED.auction_channel_id,updated_at=NOW()""",
        guild.id,pal_cat.id,pal_open.id,pal_announce.id,pal_forum.id,casino_cat.id,casino_channel.id,
        casino_announce.id,pal_ticket.id,casino_ticket.id,casino_shop["shop_id"],auction_channel.id)

    await pal_open.send(embed=pal_open_embed(),view=OpenShopPanel())
    await casino_channel.send(embed=await casino_embed(guild.id),view=CasinoShopView())
    am=await auction_channel.send(embed=auction_idle_embed(),view=AuctionIdleView())
    await db.execute("UPDATE shop.systems SET auction_message_id=$2,updated_at=NOW() WHERE guild_id=$1",guild.id,am.id)
    return await db.fetchrow("SELECT * FROM shop.systems WHERE guild_id=$1",guild.id)

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
            # FK順序を固定: transactions -> orphan escrows -> products -> shops -> system
            # 古いDBの products_shop_id_fkey が ON DELETE CASCADE なしでも削除できる。
            await con.execute("DELETE FROM shop.transactions WHERE guild_id=$1", guild.id)
            await con.execute("""DELETE FROM shop.escrows e WHERE NOT EXISTS(
                SELECT 1 FROM shop.transactions t WHERE t.transaction_id=e.transaction_id)""")
            await con.execute("""DELETE FROM shop.products p
                                 USING shop.shops s
                                 WHERE p.shop_id=s.shop_id AND s.guild_id=$1""", guild.id)
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
        shop = await db.fetchrow("SELECT * FROM shop.shops WHERE shop_id=$1", system["casino_shop_id"])
        count = await db.fetchrow("""SELECT COUNT(*) c FROM shop.products
                                     WHERE shop_id=$1 AND status<>'DELETED'""", system["casino_shop_id"])
        e = discord.Embed(
            title="🎰 CASINO SHOP ADMIN",
            description=f"📌 状態\n{'🟢 営業中' if shop and shop['status']=='ACTIVE' else '🔴 休止中'}\n\n📦 登録商品\n{count['c']}件"
        )
        await i.response.send_message(embed=e, view=CasinoAdminView(system["casino_shop_id"]), ephemeral=True)

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


async def ensure_pal_forum(guild):
    system=await db.fetchrow("SELECT * FROM shop.systems WHERE guild_id=$1",guild.id)
    if not system or system["status"]!="ACTIVE": return None
    forum=guild.get_channel(system["pal_forum_channel_id"]) if system["pal_forum_channel_id"] else None
    if forum is None and system["pal_forum_channel_id"]:
        try:
            forum=await guild.fetch_channel(system["pal_forum_channel_id"])
        except (discord.NotFound,discord.Forbidden,discord.HTTPException):
            forum=None
    if isinstance(forum,discord.ForumChannel): return forum
    category=guild.get_channel(system["pal_category_id"]) if system["pal_category_id"] else None
    if category is None and system["pal_category_id"]:
        try:
            category=await guild.fetch_channel(system["pal_category_id"])
        except (discord.NotFound,discord.Forbidden,discord.HTTPException):
            category=None
    forum=await guild.create_forum("🏪｜PALショップ",category=category)
    await db.execute("UPDATE shop.systems SET pal_forum_channel_id=$2,updated_at=NOW() WHERE guild_id=$1",guild.id,forum.id)
    return forum

async def restore_shop_thread(guild,shop):
    """DB上の既存PAL店舗を維持したまま、消えたDiscord店舗スレッドだけ復旧する。"""
    if not shop or shop["shop_type"]!="PAL" or shop["status"]=="DELETED":
        return None

    forum = await ensure_pal_forum(guild)
    if not forum:
        return None

    # 1. 既存スレッドをキャッシュ→APIの順で確認。
    thread = None
    old_thread_id = shop["forum_thread_id"]
    if old_thread_id:
        thread = guild.get_channel(old_thread_id)
        if thread is None:
            try:
                fetched = await guild.fetch_channel(old_thread_id)
                if isinstance(fetched, discord.Thread):
                    thread = fetched
            except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                thread = None

    # 2. スレッドが残っている場合はスターターメッセージを確認。
    if isinstance(thread, discord.Thread):
        panel = None
        if shop["panel_message_id"]:
            try:
                panel = await thread.fetch_message(shop["panel_message_id"])
            except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                panel = None

        total = await db.fetchval(
            "SELECT COUNT(*) FROM shop.products WHERE shop_id=$1 AND status='ACTIVE'",
            shop["shop_id"]
        )
        pages = max(1, (total + 9) // 10)

        # スレッドはあるが店舗パネルだけ消えた場合は、新しいパネルを同じスレッドへ作る。
        if panel is None:
            panel = await thread.send(
                embed=await store_embed(shop, 0),
                view=StoreView(shop["shop_id"], 0, pages)
            )
            await db.execute(
                """UPDATE shop.shops SET panel_message_id=$2,updated_at=NOW()
                   WHERE shop_id=$1""",
                shop["shop_id"], panel.id
            )
        else:
            await panel.edit(
                embed=await store_embed(shop, 0),
                view=StoreView(shop["shop_id"], 0, pages)
            )
        return thread

    # 3. Discord側の店舗スレッドだけ消えている。
    #    shops/productsは一切DELETEせず、古いDiscord IDだけ先に解除して新規スレッドを作る。
    await db.execute(
        """UPDATE shop.shops
           SET forum_thread_id=NULL,panel_message_id=NULL,updated_at=NOW()
           WHERE shop_id=$1""",
        shop["shop_id"]
    )

    total = await db.fetchval(
        "SELECT COUNT(*) FROM shop.products WHERE shop_id=$1 AND status='ACTIVE'",
        shop["shop_id"]
    )
    pages = max(1, (total + 9) // 10)

    try:
        created = await forum.create_thread(
            name=f"🏪 {shop['name']}"[:100],
            content=f"♻️ **{shop['name']}** の店舗を復旧しています..."
        )
        await created.message.edit(
            content=None,
            embed=await store_embed(shop, 0),
            view=StoreView(shop["shop_id"], 0, pages)
        )
    except Exception:
        log.exception(
            "既存店舗スレッド再生成失敗 shop_id=%s old_thread_id=%s",
            shop["shop_id"], old_thread_id
        )
        # 店舗・商品データは維持。Discord IDだけNULLのまま次回復旧可能にする。
        raise

    await db.execute(
        """UPDATE shop.shops
           SET forum_thread_id=$2,panel_message_id=$3,updated_at=NOW()
           WHERE shop_id=$1""",
        shop["shop_id"], created.thread.id, created.message.id
    )
    return created.thread

async def recover_pal_shops(guild):
    system=await db.fetchrow("SELECT * FROM shop.systems WHERE guild_id=$1",guild.id)
    if not system or system["status"]!="ACTIVE": return
    await ensure_pal_forum(guild)
    shops=await db.fetch("""SELECT * FROM shop.shops WHERE guild_id=$1 AND shop_type='PAL'
                            AND status<>'DELETED' ORDER BY shop_id""",guild.id)
    for shop in shops:
        try: await restore_shop_thread(guild,shop)
        except Exception: log.exception("PAL店舗復旧失敗 shop_id=%s",shop["shop_id"])

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
        try:
            thread = await restore_shop_thread(i.guild, shop)
            if not thread:
                raise RuntimeError("SHOP_THREAD_RECOVERY_RETURNED_NONE")
        except Exception as e:
            log.exception("店舗新規作成後のスレッド生成失敗 shop_id=%s",shop["shop_id"])
            return await i.followup.send(
                f"店舗データと商品データは登録しました。店舗スレッド生成でエラーが出ています。\n"
                f"もう一度「お店を開く」を押すと同じ店舗を復旧します。\n"
                f"`{type(e).__name__}: {str(e)[:700]}`",
                ephemeral=True
            )
        v=discord.ui.View(timeout=300)
        v.add_item(discord.ui.Button(label="お店を見る",emoji="🏪",url=thread.jump_url))
        await i.followup.send(
            embed=discord.Embed(
                title="🏪 お店を開店しました",
                description=f"**{shop['name']}**\n\n最初の商品も登録済みです。"
            ),
            view=v,
            ephemeral=True
        )

class OpenShopPanel(discord.ui.View):
    def __init__(self): super().__init__(timeout=None)

    @discord.ui.button(label="お店を開く",emoji="🏪",style=discord.ButtonStyle.success,custom_id="shop:open")
    async def open_shop(self,i,b):
        shop=await db.fetchrow("""SELECT * FROM shop.shops WHERE guild_id=$1 AND owner_id=$2
                                  AND shop_type='PAL' AND status<>'DELETED'
                                  ORDER BY shop_id DESC LIMIT 1""",i.guild_id,i.user.id)
        if not shop:
            return await i.response.send_modal(OpenShopModal())

        await i.response.defer(ephemeral=True)
        try:
            old_thread_id = shop["forum_thread_id"]
            thread=await restore_shop_thread(i.guild,shop)
        except Exception as e:
            log.exception("店舗スレッド復旧失敗 shop_id=%s",shop["shop_id"])
            return await i.followup.send(
                f"既存店舗のスレッド復旧でエラーが出ました。\n"
                f"店舗・商品データはそのまま残っています。\n"
                f"`{type(e).__name__}: {str(e)[:700]}`",
                ephemeral=True
            )

        if not thread:
            return await i.followup.send("店舗を復旧できませんでした。もう一度押してください。",ephemeral=True)

        v=discord.ui.View(timeout=300)
        v.add_item(discord.ui.Button(label="お店を見る",emoji="🏪",url=thread.jump_url))
        await i.followup.send(
            embed=discord.Embed(
                title="♻️ 店舗を復旧しました" if old_thread_id != thread.id else "🏪 自分のお店",
                description=f"**{shop['name']}**\n\n既存の店舗データ・商品データを使って店舗スレッドを表示しています。"
            ),
            view=v,
            ephemeral=True
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
        await i.response.defer(ephemeral=True)
        p = await db.fetchrow("""INSERT INTO shop.products(shop_id,name,description,price,currency)
                                 VALUES($1,$2,$3,$4,$5) RETURNING *""",
                              self.shop_id,self.name.value,self.description.value,price,self.currency)
        shop = await db.fetchrow("SELECT * FROM shop.shops WHERE shop_id=$1", self.shop_id)
        system = await db.fetchrow("SELECT * FROM shop.systems WHERE guild_id=$1", i.guild_id)
        announce_id = system["pal_announce_channel_id"] if self.currency=="PAL" else system["casino_announce_channel_id"]
        ch = i.guild.get_channel(announce_id)
        if ch:
            jump = None
            if shop["shop_type"] == "PAL" and shop["forum_thread_id"]:
                thread = i.guild.get_channel(shop["forum_thread_id"])
                if thread: jump = thread.jump_url
            else:
                casino_ch = i.guild.get_channel(system["casino_channel_id"])
                if casino_ch: jump = casino_ch.jump_url
            e = discord.Embed(
                title="📢 新しい商品が追加されました！",
                description=f"🏪 店舗\n{shop['name']}\n\n📦 商品\n{p['name']}\n\n📝 商品説明\n{p['description']}\n\n💰 価格\n{fmt_money(p['price'],p['currency'])}"
            )
            v = discord.ui.View(timeout=None)
            if jump:
                v.add_item(discord.ui.Button(label="お店を見る", emoji="🏪", url=jump))
            await ch.send(embed=e, view=v)
        await refresh_store(i.guild, self.shop_id)
        if self.currency == "CHIP":
            await refresh_casino_panel(i.guild)
        await i.followup.send(
            f"📦 **{p['name']}** を追加しました。\n💰 {fmt_money(p['price'], p['currency'])}",
            ephemeral=True
        )

class StoreView(discord.ui.View):
    def __init__(self,shop_id,page=0,pages=1):
        super().__init__(timeout=None); self.shop_id=int(shop_id); self.page=page; self.pages=max(1,pages)
        self.prev.disabled=page<=0; self.next.disabled=page>=self.pages-1
    @discord.ui.button(label="前へ",emoji="◀️",style=discord.ButtonStyle.secondary,custom_id="shop:store:prev",row=0)
    async def prev(self,i,b): await edit_store_page(i,self.shop_id,self.page-1)
    @discord.ui.button(label="購入",emoji="🛒",style=discord.ButtonStyle.success,custom_id="shop:store:buy",row=0)
    async def buy(self,i,b): await show_purchase_panel(i,self.shop_id)
    @discord.ui.button(label="次へ",emoji="▶️",style=discord.ButtonStyle.secondary,custom_id="shop:store:next",row=0)
    async def next(self,i,b): await edit_store_page(i,self.shop_id,self.page+1)
    @discord.ui.button(label="店舗情報",emoji="ℹ️",style=discord.ButtonStyle.secondary,custom_id="shop:store:info",row=1)
    async def info(self,i,b):
        s=await db.fetchrow("SELECT * FROM shop.shops WHERE shop_id=$1",self.shop_id)
        await i.response.send_message(embed=discord.Embed(title=f"ℹ️ {s['name']}",description=s["description"]),ephemeral=True)
    @discord.ui.button(label="店舗管理",emoji="⚙️",style=discord.ButtonStyle.primary,custom_id="shop:store:manage",row=1)
    async def manage(self,i,b):
        s=await db.fetchrow("SELECT * FROM shop.shops WHERE shop_id=$1",self.shop_id)
        if not s or (i.user.id!=s["owner_id"] and not is_admin(i.user)): return await i.response.send_message("店主用です。",ephemeral=True)
        await i.response.send_message("⚙️ 店舗管理",view=StoreManageView(self.shop_id),ephemeral=True)

async def edit_store_page(i,shop_id,page):
    s=await db.fetchrow("SELECT * FROM shop.shops WHERE shop_id=$1",shop_id)
    total=await db.fetchval("SELECT COUNT(*) FROM shop.products WHERE shop_id=$1 AND status='ACTIVE'",shop_id)
    pages=max(1,(total+9)//10); page=max(0,min(page,pages-1))
    await i.response.edit_message(embed=await store_embed(s,page),view=StoreView(shop_id,page,pages))

class EditShopModal(discord.ui.Modal, title="✏️ 店舗情報を変更"):
    shop_name = discord.ui.TextInput(label="新しい店名", max_length=80)
    shop_description = discord.ui.TextInput(label="新しい店舗説明", style=discord.TextStyle.paragraph, max_length=1000)

    def __init__(self, shop):
        super().__init__()
        self.shop_id = int(shop["shop_id"])
        self.shop_name.default = shop["name"]
        self.shop_description.default = shop["description"]

    async def on_submit(self, i):
        await db.execute("""UPDATE shop.shops SET name=$2,description=$3,updated_at=NOW()
                            WHERE shop_id=$1""", self.shop_id,
                         self.shop_name.value, self.shop_description.value)
        s = await db.fetchrow("SELECT * FROM shop.shops WHERE shop_id=$1", self.shop_id)
        thread = i.guild.get_channel(s["forum_thread_id"]) if s["forum_thread_id"] else None
        if thread:
            try: await thread.edit(name=f"🏪 {s['name']}"[:100])
            except discord.HTTPException: pass
        await refresh_store(i.guild, self.shop_id)
        await i.response.send_message("✏️ 店名・店舗説明を変更しました。", ephemeral=True)

class DeleteShopConfirmView(discord.ui.View):
    def __init__(self, shop_id):
        super().__init__(timeout=120)
        self.shop_id = int(shop_id)

    @discord.ui.button(label="店舗ごと削除する", emoji="🗑️", style=discord.ButtonStyle.danger)
    async def confirm(self, i, b):
        s = await db.fetchrow("SELECT * FROM shop.shops WHERE shop_id=$1", self.shop_id)
        if not s or (i.user.id != s["owner_id"] and not is_admin(i.user)):
            return await i.response.send_message("店主用です。", ephemeral=True)
        active = await db.fetchrow("""SELECT COUNT(*) c FROM shop.transactions
                                     WHERE shop_id=$1 AND status NOT IN ('COMPLETED','REFUNDED','CANCELLED')""",
                                  self.shop_id)
        if active["c"] > 0:
            return await i.response.send_message(
                f"進行中の取引が{active['c']}件あります。先に取引を完了または返金してください。",
                ephemeral=True)
        thread = i.guild.get_channel(s["forum_thread_id"]) if s["forum_thread_id"] else None
        await db.execute("UPDATE shop.shops SET status='DELETED',updated_at=NOW() WHERE shop_id=$1", self.shop_id)
        await db.execute("UPDATE shop.products SET status='DELETED',updated_at=NOW() WHERE shop_id=$1", self.shop_id)
        if thread:
            try: await thread.delete(reason="PAL SHOP 店舗削除")
            except discord.HTTPException: pass
        await i.response.send_message("🗑️ 店舗と商品をすべて削除しました。", ephemeral=True)

class StoreManageView(discord.ui.View):
    def __init__(self,shop_id):
        super().__init__(timeout=300)
        self.shop_id=int(shop_id)

    async def owner_check(self, i):
        s=await db.fetchrow("SELECT * FROM shop.shops WHERE shop_id=$1",self.shop_id)
        return s and (i.user.id == s["owner_id"] or is_admin(i.user))

    @discord.ui.button(label="商品追加",emoji="➕",style=discord.ButtonStyle.success)
    async def add(self,i,b):
        if not await self.owner_check(i):
            return await i.response.send_message("店主用です。",ephemeral=True)
        await i.response.send_modal(AddProductModal(self.shop_id,"PAL"))

    @discord.ui.button(label="商品管理",emoji="📦",style=discord.ButtonStyle.primary)
    async def products(self,i,b):
        if not await self.owner_check(i):
            return await i.response.send_message("店主用です。",ephemeral=True)
        await show_product_admin(i,self.shop_id)

    @discord.ui.button(label="店舗情報変更",emoji="✏️",style=discord.ButtonStyle.secondary)
    async def edit_shop(self,i,b):
        s=await db.fetchrow("SELECT * FROM shop.shops WHERE shop_id=$1",self.shop_id)
        if not s or (i.user.id != s["owner_id"] and not is_admin(i.user)):
            return await i.response.send_message("店主用です。",ephemeral=True)
        await i.response.send_modal(EditShopModal(s))


    @discord.ui.button(label="店舗を削除",emoji="🗑️",style=discord.ButtonStyle.danger)
    async def delete_shop(self,i,b):
        if not await self.owner_check(i):
            return await i.response.send_message("店主用です。",ephemeral=True)
        await i.response.send_message(
            "🗑️ 店舗・登録商品・フォーラム投稿をまとめて削除します。",
            view=DeleteShopConfirmView(self.shop_id), ephemeral=True)

class CasinoShopView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="購入",emoji="🛒",style=discord.ButtonStyle.success,custom_id="shop:casino:products")
    async def products(self,i,b):
        system=await db.fetchrow("SELECT * FROM shop.systems WHERE guild_id=$1",i.guild_id)
        shop=await db.fetchrow("""SELECT * FROM shop.shops WHERE guild_id=$1 AND shop_type='CASINO'
                                  AND is_official=TRUE AND status<>'DELETED'
                                  ORDER BY shop_id DESC LIMIT 1""",i.guild_id)
        if not shop:
            return await i.response.send_message("CASINO SHOP本体が見つかりません。SHOP SYSTEMを作り直してください。",ephemeral=True)
        if system and system["casino_shop_id"] != shop["shop_id"]:
            await db.execute("UPDATE shop.systems SET casino_shop_id=$2,updated_at=NOW() WHERE guild_id=$1",
                             i.guild_id,shop["shop_id"])
        await show_purchase_panel(i, shop["shop_id"])

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
        system=await db.fetchrow("SELECT * FROM shop.systems WHERE guild_id=$1",i.guild_id)
        ch=i.guild.get_channel(system["casino_channel_id"]) if system else None
        if ch:
            async for m in ch.history(limit=20):
                if m.author.id==bot.user.id and m.embeds and (m.embeds[0].title or "")=="🎰 PAL CASINO SHOP":
                    e=await casino_embed(i.guild_id)
                    await m.edit(embed=e,view=CasinoShopView())
                    break
        await i.response.send_message(f"CASINO SHOP状態: **{new}**",ephemeral=True)

async def show_purchase_panel(i, shop_id):
    shop = await db.fetchrow("SELECT * FROM shop.shops WHERE shop_id=$1", shop_id)
    if not shop or shop["status"] != "ACTIVE":
        return await i.response.send_message("現在このSHOPは休止中です。", ephemeral=True)

    rows = await db.fetch("""SELECT * FROM shop.products
                             WHERE shop_id=$1 AND status='ACTIVE'
                             ORDER BY product_id ASC LIMIT 25""", shop_id)
    if not rows:
        return await i.response.send_message("販売中の商品はありません。", ephemeral=True)

    product_text = "\n\n".join(
        f"📦 **{r['name']}**\n💰 {fmt_money(r['price'], r['currency'])}"
        for r in rows
    )
    e = discord.Embed(
        title=f"🛒 {shop['name']}｜商品購入",
        description=(
            f"購入する商品を下の選択欄から選んでください。\n\n"
            f"📦 販売中の商品\n{product_text}"
        )
    )
    await i.response.send_message(
        embed=e,
        view=ProductSelectView(rows),
        ephemeral=True
    )

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
        p=await db.fetchrow("""SELECT p.*,s.name shop_name,s.description shop_description,
                                      s.owner_id,s.status shop_status,s.shop_type,s.forum_thread_id
                               FROM shop.products p JOIN shop.shops s ON s.shop_id=p.shop_id
                               WHERE p.product_id=$1""",int(self.values[0]))
        if not p:
            return await i.response.send_message("商品が見つかりません。", ephemeral=True)
        shop = {
            "name": p["shop_name"],
            "description": p["shop_description"],
        }
        e = product_embed(p, shop)
        e.add_field(
            name="📝 購入前確認",
            value="商品名・説明・価格を確認してから購入してください。",
            inline=False
        )
        await i.response.send_message(
            embed=e,
            view=BuyView(p["product_id"], p["shop_type"], p["forum_thread_id"]),
            ephemeral=True
        )

class ProductSelectView(discord.ui.View):
    def __init__(self,rows):
        super().__init__(timeout=300)
        self.add_item(ProductSelect(rows))

class BuyView(discord.ui.View):
    def __init__(self,product_id,shop_type=None,forum_thread_id=None):
        super().__init__(timeout=300)
        self.product_id=int(product_id)
        if shop_type == "PAL" and forum_thread_id:
            self.add_item(discord.ui.Button(
                label="お店を見る",
                emoji="🏪",
                style=discord.ButtonStyle.link,
                url=f"https://discord.com/channels/@me/{int(forum_thread_id)}"
            ))

    @discord.ui.button(label="購入内容を確認",emoji="🛒",style=discord.ButtonStyle.success)
    async def buy(self,i,b):
        p=await db.fetchrow("""SELECT p.*,s.guild_id,s.owner_id,s.name shop_name,
                                      s.description shop_description,s.status shop_status
                               FROM shop.products p JOIN shop.shops s ON s.shop_id=p.shop_id
                               WHERE p.product_id=$1""",self.product_id)
        if not p or p["status"]!="ACTIVE" or p["shop_status"]!="ACTIVE":
            return await i.response.send_message("現在購入できません。",ephemeral=True)
        if p["owner_id"] == i.user.id:
            return await i.response.send_message("自分の商品です。",ephemeral=True)
        e = discord.Embed(
            title="🛒 購入内容の確認",
            description=(
                f"🏪 店舗\n{p['shop_name']}\n\n"
                f"📦 商品\n{p['name']}\n\n"
                f"📝 商品説明\n{p['description']}\n\n"
                f"💰 支払額\n{fmt_money(p['price'],p['currency'])}\n\n"
                "この内容で購入しますか？"
            )
        )
        await i.response.send_message(
            embed=e,
            view=PurchaseConfirmView(self.product_id,p['price']),
            ephemeral=True
        )

class PurchaseConfirmView(discord.ui.View):
    def __init__(self, product_id, confirmed_price):
        super().__init__(timeout=300)
        self.product_id = int(product_id)
        self.confirmed_price = int(confirmed_price)

    @discord.ui.button(label="この商品を購入する",emoji="✅",style=discord.ButtonStyle.success)
    async def confirm(self,i,b):
        await i.response.defer(ephemeral=True)
        p=await db.fetchrow("""SELECT p.*,s.guild_id,s.owner_id,s.name shop_name,s.status shop_status
                               FROM shop.products p JOIN shop.shops s ON s.shop_id=p.shop_id
                               WHERE p.product_id=$1""",self.product_id)
        if not p or p["status"]!="ACTIVE" or p["shop_status"]!="ACTIVE":
            return await i.followup.send("現在購入できません。",ephemeral=True)
        if p["owner_id"] == i.user.id:
            return await i.followup.send("自分の商品です。",ephemeral=True)
        if int(p["price"]) != self.confirmed_price:
            return await i.followup.send(f"⚠️ 価格が変更されています。\n確認時: {fmt_money(self.confirmed_price,p['currency'])}\n現在: {fmt_money(p['price'],p['currency'])}\n\n購入ボタンから開き直してください。",ephemeral=True)

        try:
            tx=await db.fetchrow("""INSERT INTO shop.transactions(
                guild_id,shop_id,product_id,buyer_id,seller_id,currency,
                shop_name_snapshot,product_name_snapshot,product_description_snapshot,price_snapshot,status)
                VALUES($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,'PAYMENT_PENDING') RETURNING *""",
                i.guild_id,p["shop_id"],p["product_id"],i.user.id,p["owner_id"],p["currency"],
                p["shop_name"],p["name"],p["description"],p["price"])
            ok,msg=await reserve_funds(
                tx["transaction_id"],i.user.id,p["owner_id"],p["currency"],p["price"]
            )
        except Exception as e:
            log.exception("購入決済開始失敗 product_id=%s buyer_id=%s currency=%s",
                          self.product_id,i.user.id,p["currency"])
            return await i.followup.send(
                f"購入処理エラー\n`{type(e).__name__}: {str(e)[:700]}`",
                ephemeral=True
            )
        if not ok:
            await db.execute("UPDATE shop.transactions SET status='CANCELLED',updated_at=NOW() WHERE transaction_id=$1",tx["transaction_id"])
            labels={
                "INSUFFICIENT_BALANCE":"残高が不足しています。",
                "MAINTENANCE":"BANKがメンテナンス中です。",
            }
            return await i.followup.send(
                f"購入できませんでした。\n{labels.get(msg,msg)}",
                ephemeral=True
            )
        await db.execute("""UPDATE shop.transactions SET status='SELLER_ACTION_REQUIRED',updated_at=NOW()
                            WHERE transaction_id=$1""",tx["transaction_id"])
        tx=await db.fetchrow("SELECT * FROM shop.transactions WHERE transaction_id=$1",tx["transaction_id"])
        try:
            ch=await create_ticket(i.guild,tx)
            await db.execute("UPDATE shop.transactions SET ticket_channel_id=$2 WHERE transaction_id=$1",tx["transaction_id"],ch.id)
        except Exception as e:
            log.exception("購入後チケット作成失敗 transaction_id=%s",tx["transaction_id"])
            ok_refund,refund_msg=await refund_funds(tx["transaction_id"])
            if ok_refund:
                await db.execute("UPDATE shop.transactions SET status='REFUNDED',updated_at=NOW() WHERE transaction_id=$1",tx["transaction_id"])
            else:
                await db.execute("UPDATE shop.transactions SET previous_status=status,status='STAFF_REVIEW',updated_at=NOW() WHERE transaction_id=$1",tx["transaction_id"])
            return await i.followup.send(
                f"取引チケット生成エラー\n"
                f"`{type(e).__name__}: {str(e)[:700]}`\n"
                f"返金処理: `{refund_msg}`",
                ephemeral=True
            )
        await i.followup.send(
            f"✅ **{p['name']}** を購入しました。\n🎫 取引チケット: {ch.mention}",
            ephemeral=True
        )

    @discord.ui.button(label="戻る",emoji="↩️",style=discord.ButtonStyle.secondary)
    async def back(self,i,b):
        p=await db.fetchrow("""SELECT p.*,s.name shop_name,s.description shop_description
                               FROM shop.products p JOIN shop.shops s ON s.shop_id=p.shop_id
                               WHERE p.product_id=$1""",self.product_id)
        if not p:
            return await i.response.send_message("商品が見つかりません。", ephemeral=True)
        await i.response.edit_message(
            embed=product_embed(p, {"name": p["shop_name"], "description": p["shop_description"]}),
            view=BuyView(self.product_id)
        )

async def create_ticket(guild,tx):
    system=await db.fetchrow("SELECT * FROM shop.systems WHERE guild_id=$1 AND status='ACTIVE'",guild.id)
    if not system:
        raise RuntimeError("SHOP_SYSTEM_NOT_FOUND")

    cat_id=system["pal_ticket_category_id"] if tx["currency"]=="PAL" else system["casino_ticket_category_id"]
    if not cat_id:
        raise RuntimeError(f"TICKET_CATEGORY_ID_NOT_SET:{tx['currency']}")

    cat=guild.get_channel(cat_id)
    if cat is None:
        try:
            cat=await guild.fetch_channel(cat_id)
        except (discord.NotFound,discord.Forbidden,discord.HTTPException) as e:
            raise RuntimeError(f"TICKET_CATEGORY_NOT_FOUND:{cat_id}") from e
    if not isinstance(cat,discord.CategoryChannel):
        raise RuntimeError(f"TICKET_CATEGORY_INVALID:{cat_id}")

    overwrites={guild.default_role:discord.PermissionOverwrite(view_channel=False)}

    buyer=guild.get_member(tx["buyer_id"])
    if buyer is None:
        try: buyer=await guild.fetch_member(tx["buyer_id"])
        except (discord.NotFound,discord.Forbidden,discord.HTTPException): buyer=None

    seller=guild.get_member(tx["seller_id"]) if tx["seller_id"] else None
    if seller is None and tx["seller_id"]:
        try: seller=await guild.fetch_member(tx["seller_id"])
        except (discord.NotFound,discord.Forbidden,discord.HTTPException): seller=None

    if buyer:
        overwrites[buyer]=discord.PermissionOverwrite(
            view_channel=True,send_messages=True,read_message_history=True
        )
    if seller:
        overwrites[seller]=discord.PermissionOverwrite(
            view_channel=True,send_messages=True,read_message_history=True
        )
    for role in guild.roles:
        if role.permissions.administrator:
            overwrites[role]=discord.PermissionOverwrite(
                view_channel=True,send_messages=True,read_message_history=True
            )

    me=guild.me
    if me:
        overwrites[me]=discord.PermissionOverwrite(
            view_channel=True,send_messages=True,read_message_history=True,manage_channels=True
        )

    ch=await guild.create_text_channel(
        f"取引-{tx['transaction_id']:06d}",
        category=cat,
        overwrites=overwrites
    )
    try:
        await ch.send(embed=ticket_embed(tx),view=TicketView(tx["transaction_id"]))
    except Exception:
        try: await ch.delete(reason="取引パネル生成失敗")
        except Exception: pass
        raise
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
        await i.response.defer()
        await db.execute("UPDATE shop.transactions SET status='BUYER_CONFIRMATION_REQUIRED',updated_at=NOW() WHERE transaction_id=$1",self.txid)
        await refresh_ticket(i.channel,self.txid)
        await i.followup.send("📦 購入者の受取確認待ちです。")

    @discord.ui.button(label="受け取りました",emoji="✅",style=discord.ButtonStyle.success,custom_id="shop:tx:received")
    async def received(self,i,b):
        tx=await db.fetchrow("SELECT * FROM shop.transactions WHERE transaction_id=$1",self.txid)
        if i.user.id!=tx["buyer_id"]:return await i.response.send_message("購入者用です。",ephemeral=True)
        if tx["status"]!="BUYER_CONFIRMATION_REQUIRED":return await i.response.send_message("店主の対応待ちです。",ephemeral=True)
        await i.response.defer()
        ok,msg=await release_funds(self.txid)
        if not ok:return await i.followup.send(f"処理結果: `{msg}`",ephemeral=True)
        await db.execute("UPDATE shop.transactions SET status='COMPLETED',updated_at=NOW() WHERE transaction_id=$1",self.txid)
        await refresh_ticket(i.channel,self.txid)
        await i.followup.send("✅ 取引完了！10分後にチケットを削除します。")
        await asyncio.sleep(600)
        try:await i.channel.delete(reason="SHOP取引完了")
        except discord.NotFound:pass

    @discord.ui.button(label="問題があります",emoji="⚠️",style=discord.ButtonStyle.danger,custom_id="shop:tx:problem")
    async def problem(self,i,b):
        tx=await db.fetchrow("SELECT * FROM shop.transactions WHERE transaction_id=$1",self.txid)
        if i.user.id not in (tx["buyer_id"],tx["seller_id"]):return await i.response.send_message("取引参加者用です。",ephemeral=True)
        await i.response.defer()
        await db.execute("UPDATE shop.transactions SET previous_status=status,status='STAFF_REVIEW',updated_at=NOW() WHERE transaction_id=$1",self.txid)
        await refresh_ticket(i.channel,self.txid)
        await i.followup.send("🚨 STAFF REVIEWへ移行しました。")

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
        await i.response.defer()
        ok,msg=await refund_funds(self.txid)
        if not ok:return await i.followup.send(f"処理結果: `{msg}`",ephemeral=True)
        await db.execute("UPDATE shop.transactions SET status='REFUNDED',updated_at=NOW() WHERE transaction_id=$1",self.txid)
        await refresh_ticket(i.channel,self.txid)
        await i.followup.send("↩️ 全額返金しました。10分後にチケットを削除します。")
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

class EditProductModal(discord.ui.Modal,title="✏️ 商品情報を変更"):
    product_name=discord.ui.TextInput(label="商品名",max_length=100)
    product_description=discord.ui.TextInput(label="商品説明",style=discord.TextStyle.paragraph,max_length=1500)
    product_price=discord.ui.TextInput(label="価格",max_length=18)
    def __init__(self,p):
        super().__init__(); self.product_id=int(p["product_id"])
        self.product_name.default=p["name"]; self.product_description.default=p["description"]; self.product_price.default=str(p["price"])
    async def on_submit(self,i):
        try:
            price=int(self.product_price.value.replace(",",""))
            if price < 1:
                raise ValueError
        except ValueError:
            return await i.response.send_message("価格は1以上の整数で入力してください。",ephemeral=True)
        await i.response.defer(ephemeral=True)
        await db.execute("UPDATE shop.products SET name=$2,description=$3,price=$4,updated_at=NOW() WHERE product_id=$1",self.product_id,self.product_name.value,self.product_description.value,price)
        p=await db.fetchrow("SELECT * FROM shop.products WHERE product_id=$1",self.product_id)
        await refresh_store(i.guild,p["shop_id"]); s=await db.fetchrow("SELECT * FROM shop.shops WHERE shop_id=$1",p["shop_id"])
        if s and s["shop_type"]=="CASINO": await refresh_casino_panel(i.guild)
        await i.followup.send("✏️ 商品情報を更新しました。",ephemeral=True)

class ProductActionView(discord.ui.View):
    def __init__(self,product_id): super().__init__(timeout=300); self.product_id=int(product_id)
    @discord.ui.button(label="商品情報・価格変更",emoji="✏️",style=discord.ButtonStyle.primary)
    async def edit(self,i,b):
        p=await db.fetchrow("SELECT * FROM shop.products WHERE product_id=$1",self.product_id)
        await i.response.send_modal(EditProductModal(p))
    @discord.ui.button(label="販売停止 / 再開",emoji="⏯️",style=discord.ButtonStyle.secondary)
    async def toggle(self,i,b):
        await i.response.defer(ephemeral=True)
        p=await db.fetchrow("SELECT * FROM shop.products WHERE product_id=$1",self.product_id)
        if not p:
            return await i.followup.send("商品が見つかりません。",ephemeral=True)
        new="PAUSED" if p["status"]=="ACTIVE" else "ACTIVE"
        await db.execute("UPDATE shop.products SET status=$2,updated_at=NOW() WHERE product_id=$1",self.product_id,new)
        await refresh_store(i.guild,p["shop_id"]); s=await db.fetchrow("SELECT * FROM shop.shops WHERE shop_id=$1",p["shop_id"])
        if s and s["shop_type"]=="CASINO": await refresh_casino_panel(i.guild)
        await i.followup.send(f"商品状態: **{new}**",ephemeral=True)
    @discord.ui.button(label="商品削除",emoji="🗑️",style=discord.ButtonStyle.danger)
    async def delete(self,i,b):
        await i.response.defer(ephemeral=True)
        p=await db.fetchrow("SELECT * FROM shop.products WHERE product_id=$1",self.product_id)
        if not p:
            return await i.followup.send("商品が見つかりません。",ephemeral=True)
        await db.execute("UPDATE shop.products SET status='DELETED',updated_at=NOW() WHERE product_id=$1",self.product_id)
        await refresh_store(i.guild,p["shop_id"]); s=await db.fetchrow("SELECT * FROM shop.shops WHERE shop_id=$1",p["shop_id"])
        if s and s["shop_type"]=="CASINO": await refresh_casino_panel(i.guild)
        await i.followup.send("🗑️ 商品を削除しました。",ephemeral=True)


async def show_product_admin(i,shop_id):
    rows=await db.fetch("""SELECT * FROM shop.products WHERE shop_id=$1 AND status<>'DELETED'
                           ORDER BY product_id LIMIT 25""",shop_id)
    if not rows:return await i.response.send_message("商品はありません。",ephemeral=True)
    await i.response.send_message("📦 管理する商品を選択してください。",view=ProductAdminView(rows),ephemeral=True)

async def refresh_casino_panel(guild):
    system = await db.fetchrow("SELECT * FROM shop.systems WHERE guild_id=$1", guild.id)
    if not system or not system["casino_channel_id"]:
        return
    ch = guild.get_channel(system["casino_channel_id"])
    if not ch:
        return
    async for m in ch.history(limit=30):
        if m.author.id == bot.user.id and m.embeds and (m.embeds[0].title or "") == "🎰 PAL CASINO SHOP":
            await m.edit(embed=await casino_embed(guild.id), view=CasinoShopView())
            return

async def refresh_store(guild,shop_id):
    s=await db.fetchrow("SELECT * FROM shop.shops WHERE shop_id=$1",shop_id)
    if not s or s["status"]=="DELETED": return
    if s["shop_type"]=="PAL":
        thread=await restore_shop_thread(guild,s)
        if not thread: return
        s=await db.fetchrow("SELECT * FROM shop.shops WHERE shop_id=$1",shop_id)
    else:
        thread=guild.get_channel(s["forum_thread_id"]) if s["forum_thread_id"] else None
        if not thread: return
    try:
        m=await thread.fetch_message(s["panel_message_id"])
        total=await db.fetchval("SELECT COUNT(*) FROM shop.products WHERE shop_id=$1 AND status='ACTIVE'",shop_id)
        await m.edit(embed=await store_embed(s,0),view=StoreView(shop_id,0,max(1,(total+9)//10)))
    except (discord.NotFound,discord.Forbidden,discord.HTTPException):
        if s["shop_type"]=="PAL":
            await db.execute("UPDATE shop.shops SET panel_message_id=NULL,forum_thread_id=NULL WHERE shop_id=$1",shop_id)
            await restore_shop_thread(guild,await db.fetchrow("SELECT * FROM shop.shops WHERE shop_id=$1",shop_id))


async def refresh_ticket(channel,txid):
    tx=await db.fetchrow("SELECT * FROM shop.transactions WHERE transaction_id=$1",txid)
    async for m in channel.history(limit=30,oldest_first=True):
        if m.author.id==bot.user.id and m.embeds and (m.embeds[0].title or "").startswith("🎫 取引チケット"):
            await m.edit(embed=ticket_embed(tx),view=TicketView(txid))
            break


JST=ZoneInfo("Asia/Tokyo")
auction_tasks={}

def auction_idle_embed():
    return discord.Embed(title="🔨 PAL 競り市場",description="現在開催中の競りはありません。\n\n商品を競りに出品できます。")

def parse_jst_datetime(v):
    for f in ("%Y/%m/%d %H:%M","%Y-%m-%d %H:%M"):
        try:return datetime.strptime(v.strip(),f).replace(tzinfo=JST).astimezone(timezone.utc)
        except ValueError:pass
    raise ValueError

async def auction_embed(a):
    sec=max(0,int((a["ends_at"]-datetime.now(timezone.utc)).total_seconds())); h,sec=divmod(sec,3600); m=sec//60
    bidder=f"<@{a['highest_bidder_id']}>" if a["highest_bidder_id"] else "まだいません"
    return discord.Embed(title="🔨 PAL 競り市場",description=f"📦 商品\n{a['product_name']}\n\n📝 商品説明\n{a['product_description']}\n\n👤 出品者\n<@{a['seller_id']}>\n\n💰 現在価格\n{fmt_money(a['current_price'],'PAL')}\n\n👑 最高入札者\n{bidder}\n\n⏰ 終了\n{a['ends_at'].astimezone(JST).strftime('%Y年%m月%d日 %H:%M')}\n\n⏳ 残り\n{h}時間{m}分")

async def refresh_auction_panel(guild):
    s=await db.fetchrow("SELECT * FROM shop.systems WHERE guild_id=$1",guild.id)
    if not s or not s["auction_channel_id"]:return
    ch=guild.get_channel(s["auction_channel_id"])
    if not ch:return
    a=await db.fetchrow("SELECT * FROM shop.auctions WHERE guild_id=$1 AND status='ACTIVE'",guild.id)
    embed=await auction_embed(a) if a else auction_idle_embed(); view=AuctionActiveView(a["auction_id"]) if a else AuctionIdleView()
    msg=None
    if s["auction_message_id"]:
        try:msg=await ch.fetch_message(s["auction_message_id"])
        except discord.HTTPException:pass
    if msg:await msg.edit(embed=embed,view=view)
    else:
        msg=await ch.send(embed=embed,view=view)
        await db.execute("UPDATE shop.systems SET auction_message_id=$2,updated_at=NOW() WHERE guild_id=$1",guild.id,msg.id)

class AuctionCreateModal(discord.ui.Modal,title="🔨 競りを開始"):
    product_name=discord.ui.TextInput(label="商品名",max_length=100)
    product_description=discord.ui.TextInput(label="商品説明",style=discord.TextStyle.paragraph,max_length=1500)
    start_price=discord.ui.TextInput(label="開始価格 PAL",max_length=18)
    async def on_submit(self,i):
        try:
            price=int(self.start_price.value.replace(",","").strip())
            if price<1: raise ValueError
        except ValueError: return await i.response.send_message("開始価格は1 PAL以上の整数で入力してください。",ephemeral=True)
        if await db.fetchrow("SELECT 1 FROM shop.auctions WHERE guild_id=$1 AND status='ACTIVE'",i.guild_id):
            return await i.response.send_message("現在競りが開催されています。",ephemeral=True)
        await i.response.send_message(embed=discord.Embed(title="⏰ 競り時間を選択",description=f"📦 商品\n{self.product_name.value}\n\n📝 商品説明\n{self.product_description.value}\n\n💰 開始価格\n{fmt_money(price,'PAL')}\n\nこの競りを何時間開催しますか？"),view=AuctionDurationView(self.product_name.value,self.product_description.value,price),ephemeral=True)

class AuctionDurationView(discord.ui.View):
    def __init__(self,n,d,p): super().__init__(timeout=300);self.n=n;self.d=d;self.p=int(p)
    async def choose(self,i,hours):
        ends=datetime.now(timezone.utc)+timedelta(hours=hours)
        await i.response.edit_message(embed=discord.Embed(title="🔨 競りを開始しますか？",description=f"📦 商品\n{self.n}\n\n📝 商品説明\n{self.d}\n\n💰 開始価格\n{fmt_money(self.p,'PAL')}\n\n⏳ 開催時間\n{hours}時間\n\n⏰ 終了予定\n{ends.astimezone(JST).strftime('%Y年%m月%d日 %H:%M')}"),view=AuctionConfirmView(self.n,self.d,self.p,ends))
    @discord.ui.button(label="1時間",emoji="1️⃣",style=discord.ButtonStyle.primary)
    async def one(self,i,b): await self.choose(i,1)
    @discord.ui.button(label="6時間",emoji="6️⃣",style=discord.ButtonStyle.primary)
    async def six(self,i,b): await self.choose(i,6)
    @discord.ui.button(label="24時間",emoji="🕛",style=discord.ButtonStyle.primary)
    async def day(self,i,b): await self.choose(i,24)

class AuctionConfirmView(discord.ui.View):
    def __init__(self,n,d,p,e): super().__init__(timeout=300);self.n=n;self.d=d;self.p=p;self.e=e
    @discord.ui.button(label="競り開始",emoji="🔨",style=discord.ButtonStyle.success)
    async def confirm(self,i,b):
        try:
            a=await db.fetchrow("""INSERT INTO shop.auctions(guild_id,seller_id,product_name,product_description,
                start_price,current_price,ends_at,status) VALUES($1,$2,$3,$4,$5,$5,$6,'ACTIVE') RETURNING *""",
                i.guild_id,i.user.id,self.n,self.d,self.p,self.e)
        except Exception: return await i.response.send_message("現在競りが開催されています。",ephemeral=True)
        schedule_auction(i.guild,a["auction_id"],a["ends_at"]);await refresh_auction_panel(i.guild)
        await i.response.edit_message(content="🔨 競りを開始しました。",embed=None,view=None)
    @discord.ui.button(label="キャンセル",emoji="✖️",style=discord.ButtonStyle.secondary)
    async def cancel(self,i,b): await i.response.edit_message(content="競りの開始をキャンセルしました。",embed=None,view=None)

class AuctionIdleView(discord.ui.View):
    def __init__(self):super().__init__(timeout=None)
    @discord.ui.button(label="競りを開始する",emoji="🔨",style=discord.ButtonStyle.success,custom_id="shop:auction:create")
    async def create(self,i,b):await i.response.send_modal(AuctionCreateModal())

class AuctionBidModal(discord.ui.Modal,title="💰 入札"):
    amount=discord.ui.TextInput(label="入札額 PAL",max_length=18)
    def __init__(self,aid):super().__init__();self.aid=int(aid)
    async def on_submit(self,i):
        try:amount=int(self.amount.value.replace(",",""))
        except ValueError:return await i.response.send_message("整数PALで入力してください。",ephemeral=True)
        await i.response.defer(ephemeral=True)
        async with db.acquire() as con:
            async with con.transaction():
                a=await con.fetchrow("SELECT * FROM shop.auctions WHERE auction_id=$1 FOR UPDATE",self.aid)
                if not a or a["status"]!="ACTIVE" or a["ends_at"]<=datetime.now(timezone.utc):return await i.followup.send("競りは終了しています。",ephemeral=True)
                if a["seller_id"]==i.user.id:return await i.followup.send("自分の競りには入札できません。",ephemeral=True)
                if amount<=a["current_price"]:return await i.followup.send("現在価格より1 PAL以上高く入力してください。",ephemeral=True)
                acc=await account_row(con,i.user.id,"PAL",True)
                if not acc or acc["balance"]<amount:return await i.followup.send("PAL残高が不足しています。",ephemeral=True)
                old=None
                if a["highest_bidder_id"]:old=await account_row(con,a["highest_bidder_id"],"PAL",True)
                await change_balance(con,acc["account_id"],-amount)
                if old: await change_balance(con,old["account_id"],a["current_price"])
                await con.execute("UPDATE shop.auctions SET current_price=$2,highest_bidder_id=$3 WHERE auction_id=$1",self.aid,amount,i.user.id)
                await con.execute("INSERT INTO shop.auction_bids(auction_id,bidder_id,amount) VALUES($1,$2,$3)",self.aid,i.user.id,amount)
        await refresh_auction_panel(i.guild);await i.followup.send(f"💰 {fmt_money(amount,'PAL')} で入札しました。",ephemeral=True)

class AuctionActiveView(discord.ui.View):
    def __init__(self,aid):super().__init__(timeout=None);self.aid=int(aid)
    @discord.ui.button(label="入札する",emoji="💰",style=discord.ButtonStyle.success,custom_id="shop:auction:bid")
    async def bid(self,i,b):await i.response.send_modal(AuctionBidModal(self.aid))

async def finish_auction(guild,aid):
    tx=None
    async with db.acquire() as con:
        async with con.transaction():
            a=await con.fetchrow("SELECT * FROM shop.auctions WHERE auction_id=$1 FOR UPDATE",aid)
            if not a or a["status"]!="ACTIVE":
                return
            if a["ends_at"]>datetime.now(timezone.utc):
                return
            if not a["highest_bidder_id"]:
                await con.execute("UPDATE shop.auctions SET status='NO_BIDS',completed_at=NOW() WHERE auction_id=$1",aid)
            else:
                tx=await con.fetchrow("""INSERT INTO shop.transactions(
                    guild_id,shop_id,product_id,buyer_id,seller_id,currency,
                    shop_name_snapshot,product_name_snapshot,product_description_snapshot,price_snapshot,status)
                    VALUES($1,NULL,NULL,$2,$3,'PAL','PAL 競り市場',$4,$5,$6,'SELLER_ACTION_REQUIRED')
                    RETURNING *""",a["guild_id"],a["highest_bidder_id"],a["seller_id"],
                    a["product_name"],a["product_description"],a["current_price"])
                await con.execute("""INSERT INTO shop.escrows(
                    transaction_id,buyer_id,seller_id,currency,amount,status)
                    VALUES($1,$2,$3,'PAL',$4,'HELD')""",
                    tx["transaction_id"],a["highest_bidder_id"],a["seller_id"],a["current_price"])
                await con.execute("""UPDATE shop.auctions SET status='COMPLETED',
                    transaction_id=$2,completed_at=NOW() WHERE auction_id=$1""",aid,tx["transaction_id"])
    if tx:
        try:
            ch=await create_ticket(guild,tx)
            await db.execute("UPDATE shop.transactions SET ticket_channel_id=$2 WHERE transaction_id=$1",tx["transaction_id"],ch.id)
        except Exception:
            log.exception("競り取引チケット作成失敗 transaction_id=%s",tx["transaction_id"])
    await refresh_auction_panel(guild)

def schedule_auction(guild,aid,ends):
    old=auction_tasks.pop(aid,None)
    if old and not old.done():
        old.cancel()
    async def run():
        try:
            await asyncio.sleep(max(0,(ends-datetime.now(timezone.utc)).total_seconds()))
            await finish_auction(guild,aid)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("競り終了処理失敗 auction_id=%s",aid)
        finally:
            auction_tasks.pop(aid,None)
    auction_tasks[aid]=asyncio.create_task(run())


@bot.event
async def setup_hook():
    await db.init_db()
    bot.add_view(SetupView())
    bot.add_view(OpenShopPanel())
    bot.add_view(CasinoShopView())

    shops=await db.fetch("""SELECT shop_id,panel_message_id FROM shop.shops
                            WHERE shop_type='PAL' AND status<>'DELETED'
                            AND panel_message_id IS NOT NULL""")
    for s in shops:
        bot.add_view(StoreView(s["shop_id"]),message_id=s["panel_message_id"])

    txs=await db.fetch("""SELECT transaction_id,ticket_channel_id FROM shop.transactions
                          WHERE status NOT IN ('COMPLETED','REFUNDED','CANCELLED')
                          AND ticket_channel_id IS NOT NULL""")
    for tx in txs:
        bot.add_view(TicketView(tx["transaction_id"]))

    systems=await db.fetch("""SELECT guild_id,auction_message_id FROM shop.systems
                              WHERE status='ACTIVE' AND auction_message_id IS NOT NULL""")
    for system in systems:
        a=await db.fetchrow("SELECT auction_id FROM shop.auctions WHERE guild_id=$1 AND status='ACTIVE'",system["guild_id"])
        view=AuctionActiveView(a["auction_id"]) if a else AuctionIdleView()
        bot.add_view(view,message_id=system["auction_message_id"])
    log.info("DB・SHOP SYSTEM永続View登録完了")

@bot.event
async def on_ready():
    log.info("PAL SHOP起動完了: %s",bot.user)
    for guild in bot.guilds:
        try:
            await recover_pal_shops(guild)
            a=await db.fetchrow("SELECT * FROM shop.auctions WHERE guild_id=$1 AND status='ACTIVE'",guild.id)
            if a:
                if a["ends_at"]<=datetime.now(timezone.utc):
                    await finish_auction(guild,a["auction_id"])
                else:
                    schedule_auction(guild,a["auction_id"],a["ends_at"])
            await refresh_auction_panel(guild)
        except Exception:
            log.exception("起動復旧失敗 guild=%s",guild.id)

@bot.event
async def on_error(event_method,*args,**kwargs):
    log.exception("Discord event error: %s",event_method)


@bot.command()
@commands.has_permissions(administrator=True)
async def shopsetup(ctx):
    system=await db.fetchrow("SELECT * FROM shop.systems WHERE guild_id=$1",ctx.guild.id)
    await ctx.send(embed=system_embed(system),view=SetupView())

bot.run(os.environ["DISCORD_TOKEN"])
