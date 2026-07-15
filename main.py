import os, asyncio, logging
import discord
from discord.ext import commands, tasks
import shop_db as db
import shop_service as svc
from views import ShopSetupView, ShopPanelView, TicketView

logging.basicConfig(level=logging.INFO)
log=logging.getLogger("pal_shop")

intents=discord.Intents.default()
intents.message_content=True
intents.members=True
bot=commands.Bot(command_prefix="!",intents=intents,help_command=commands.DefaultHelpCommand())

def money(n): return f"{int(n):,} PAL"

async def find_config(guild_id, key):
    # PAL共通DB側の設定テーブルが存在する場合に読む。無い場合は環境変数へフォールバック。
    try:
        row=await db.fetchrow("SELECT value FROM shop.bot_settings WHERE guild_id=$1 AND key=$2",guild_id,key)
        if row:return row["value"]
    except Exception:
        pass
    return os.getenv(key)

@bot.event
async def setup_hook():
    await db.init_db()
    # 設定テーブル
    await db.execute("""CREATE TABLE IF NOT EXISTS shop.bot_settings(
        guild_id BIGINT NOT NULL,key TEXT NOT NULL,value TEXT NOT NULL,
        PRIMARY KEY(guild_id,key))""")
    bot.add_view(ShopSetupView(bot))
    shops=await db.fetch("SELECT shop_id FROM shop.shops WHERE delete_reason IS NULL")
    for s in shops: bot.add_view(ShopPanelView(bot,s["shop_id"]))
    txs=await db.fetch("""SELECT transaction_id FROM shop.transactions
                         WHERE status NOT IN ('COMPLETED','REFUNDED','CANCELLED')""")
    for t in txs: bot.add_view(TicketView(bot,t["transaction_id"]))
    deadline_watch.start()
    log.info("DB・SHOP復旧完了")

@bot.event
async def on_ready():
    log.info("PAL SHOP起動完了: %s",bot.user)

async def save_config(guild_id, key, value):
    await db.execute("""INSERT INTO shop.bot_settings(guild_id,key,value) VALUES($1,$2,$3)
                        ON CONFLICT(guild_id,key) DO UPDATE SET value=EXCLUDED.value""",
                     guild_id, key, str(value))

async def ensure_shop_channels(guild):
    me = guild.me
    if me is None:
        raise RuntimeError("BOTメンバー情報を取得できません")

    category = discord.utils.get(guild.categories, name="🏪 PAL SHOP")
    if category is None:
        category = await guild.create_category("🏪 PAL SHOP", reason="PAL SHOP 自動セットアップ")

    forum = discord.utils.get(guild.forums, name="🏪｜ショップ")
    if forum is None:
        forum = await guild.create_forum(
            "🏪｜ショップ",
            category=category,
            reason="PAL SHOP 自動セットアップ"
        )

    announce = discord.utils.get(guild.text_channels, name="📢｜商品追加")
    if announce is None:
        announce = await guild.create_text_channel(
            "📢｜商品追加",
            category=category,
            reason="PAL SHOP 自動セットアップ"
        )

    ticket_category = discord.utils.get(guild.categories, name="🎫 PAL SHOP 取引")
    if ticket_category is None:
        ticket_category = await guild.create_category(
            "🎫 PAL SHOP 取引",
            overwrites={
                guild.default_role: discord.PermissionOverwrite(view_channel=False),
                me: discord.PermissionOverwrite(
                    view_channel=True,
                    send_messages=True,
                    read_message_history=True,
                    manage_channels=True
                )
            },
            reason="PAL SHOP 自動セットアップ"
        )

    await save_config(guild.id, "SHOP_FORUM_CHANNEL_ID", forum.id)
    await save_config(guild.id, "SHOP_TICKET_CATEGORY_ID", ticket_category.id)
    await save_config(guild.id, "SHOP_ANNOUNCE_CHANNEL_ID", announce.id)

    return category, forum, announce, ticket_category

@bot.command()
@commands.has_permissions(administrator=True)
async def shopsetup(ctx):
    try:
        category, forum, announce, ticket_category = await ensure_shop_channels(ctx.guild)
    except discord.Forbidden:
        return await ctx.send("❌ PAL SHOPに「チャンネルの管理」権限を付けてから、もう一度 `!shopsetup` を実行してください。")
    except Exception as e:
        return await ctx.send(f"❌ SHOPセットアップエラー: `{type(e).__name__}: {e}`")

    e=discord.Embed(
        title="🏪 PAL SHOP",
        description="PALサーバーのマーケット。\n\n自分のお店を開き、商品をPALで販売できます。"
    )
    e.add_field(name="🏪 お店を開く",value="店名・説明・最初の商品を登録",inline=False)
    e.add_field(name="📦 自分のお店",value="店舗状態を確認",inline=False)
    e.add_field(
        name="✅ SHOP SYSTEM",
        value=f"店舗: {forum.mention}\n商品追加: {announce.mention}\n取引チケット: **{ticket_category.name}**",
        inline=False
    )
    await ctx.send(embed=e,view=ShopSetupView(bot))

@bot.command()
@commands.has_permissions(administrator=True)
async def shopconfig(ctx, key:str, value:str):
    allowed={"SHOP_FORUM_CHANNEL_ID","SHOP_TICKET_CATEGORY_ID","SHOP_ANNOUNCE_CHANNEL_ID","SHOP_STAFF_ROLE_ID"}
    if key not in allowed:
        return await ctx.send("設定名: "+", ".join(sorted(allowed)))
    await db.execute("""INSERT INTO shop.bot_settings(guild_id,key,value) VALUES($1,$2,$3)
                        ON CONFLICT(guild_id,key) DO UPDATE SET value=EXCLUDED.value""",ctx.guild.id,key,value)
    await ctx.send(f"⚙️ `{key}` を保存しました。")

async def publish_shop(guild, shop):
    forum_id=await find_config(guild.id,"SHOP_FORUM_CHANNEL_ID")
    if not forum_id: raise RuntimeError("SHOP_FORUM_CHANNEL_ID が未設定")
    forum=guild.get_channel(int(forum_id))
    if not isinstance(forum,discord.ForumChannel): raise RuntimeError("設定先がフォーラムではありません")
    e=shop_embed(shop)
    thread,msg=await forum.create_thread(name=f"🏪 {shop['name']}",embed=e,view=ShopPanelView(bot,shop["shop_id"]))
    await svc.set_shop_panel(shop["shop_id"],thread.id,msg.id)
bot.publish_shop=publish_shop

def shop_embed(shop):
    e=discord.Embed(title=f"🏪 {shop['name']}",description=shop["description"])
    e.add_field(name="👤 店主",value=f"<@{shop['owner_id']}>",inline=False)
    e.add_field(name="📌 状態",value="🟢 営業中" if shop["status"]=="active" else "🔴 閉店中",inline=False)
    return e

async def refresh_shop(shop_id):
    s=await db.fetchrow("SELECT * FROM shop.shops WHERE shop_id=$1",shop_id)
    if not s or not s["forum_thread_id"] or not s["panel_message_id"]:return
    g=bot.get_guild(s["guild_id"]); ch=g.get_channel(s["forum_thread_id"]) if g else None
    if ch:
        try:
            m=await ch.fetch_message(s["panel_message_id"])
            await m.edit(embed=shop_embed(s),view=ShopPanelView(bot,shop_id))
        except discord.NotFound: pass
bot.refresh_shop=refresh_shop

async def announce_product(guild,shop_id,p):
    cid=await find_config(guild.id,"SHOP_ANNOUNCE_CHANNEL_ID")
    if not cid:return
    ch=guild.get_channel(int(cid)); s=await db.fetchrow("SELECT * FROM shop.shops WHERE shop_id=$1",shop_id)
    if ch:
        e=discord.Embed(title="📢 新しい商品が追加されました！")
        e.add_field(name="🏪 店舗",value=s["name"],inline=False)
        e.add_field(name="📦 商品",value=p["name"],inline=False)
        e.add_field(name="📝",value=p["description"],inline=False)
        e.add_field(name="💰 価格",value=money(p["price"]),inline=False)
        await ch.send(embed=e)
bot.announce_product=announce_product

async def reserve_pal(guild_id,buyer_id,seller_id,amount):
    # MASTER SPECのSHOP_PURCHASE_RESERVE相当。共通bank.accountsへtransaction付きで接続。
    try:
        async with db.POOL.acquire() as con:
            async with con.transaction():
                buyer=await con.fetchrow("""SELECT account_id,balance FROM bank.accounts
                    WHERE owner_id=$1 AND currency='PAL' AND account_type='USER' FOR UPDATE""",buyer_id)
                if not buyer or buyer["balance"]<amount:return False,"💰 PAL残高が足りません。"
                await con.execute("UPDATE bank.accounts SET balance=balance-$2,updated_at=NOW() WHERE account_id=$1",buyer["account_id"],amount)
        return True,"SUCCESS"
    except Exception as e:return False,f"BANK処理エラー: `{type(e).__name__}: {e}`"
bot.reserve_pal=reserve_pal

async def release_pal(tx):
    try:
        async with db.POOL.acquire() as con:
            async with con.transaction():
                seller=await con.fetchrow("""SELECT account_id FROM bank.accounts
                    WHERE owner_id=$1 AND currency='PAL' AND account_type='USER' FOR UPDATE""",tx["seller_id"])
                if not seller:return False,"店主PAL口座が見つかりません。"
                await con.execute("UPDATE bank.accounts SET balance=balance+$2,updated_at=NOW() WHERE account_id=$1",seller["account_id"],tx["price_snapshot"])
        return True,"SUCCESS"
    except Exception as e:return False,f"BANK処理エラー: `{type(e).__name__}: {e}`"
bot.release_pal=release_pal

async def refund_pal(tx):
    try:
        async with db.POOL.acquire() as con:
            async with con.transaction():
                buyer=await con.fetchrow("""SELECT account_id FROM bank.accounts
                    WHERE owner_id=$1 AND currency='PAL' AND account_type='USER' FOR UPDATE""",tx["buyer_id"])
                if not buyer:return False,"購入者PAL口座が見つかりません。"
                await con.execute("UPDATE bank.accounts SET balance=balance+$2,updated_at=NOW() WHERE account_id=$1",buyer["account_id"],tx["price_snapshot"])
        return True,"SUCCESS"
    except Exception as e:return False,f"BANK処理エラー: `{type(e).__name__}: {e}`"
bot.refund_pal=refund_pal

def ticket_embed(tx):
    status={"SELLER_ACTION_REQUIRED":"🟡 店主の対応待ち","BUYER_CONFIRMATION_REQUIRED":"🔵 購入者の確認待ち",
            "CANCEL_PENDING":"🟠 キャンセル確認待ち","STAFF_REVIEW":"🚨 STAFF REVIEW",
            "COMPLETED":"✅ 完了","REFUNDED":"↩️ 返金済み"}.get(tx["status"],tx["status"])
    e=discord.Embed(title=f"🎫 取引チケット #{tx['transaction_id']:06d}")
    e.add_field(name="🏪 店舗",value=tx["shop_name_snapshot"],inline=False)
    e.add_field(name="👤 店主",value=f"<@{tx['seller_id']}>",inline=True)
    e.add_field(name="🛒 購入者",value=f"<@{tx['buyer_id']}>",inline=True)
    e.add_field(name="📦 商品",value=tx["product_name_snapshot"],inline=False)
    e.add_field(name="💰 価格",value=money(tx["price_snapshot"]),inline=False)
    e.add_field(name="📌 取引状態",value=status,inline=False)
    return e

async def create_ticket(guild,tx):
    cat_id=await find_config(guild.id,"SHOP_TICKET_CATEGORY_ID")
    if not cat_id:raise RuntimeError("SHOP_TICKET_CATEGORY_ID が未設定")
    cat=guild.get_channel(int(cat_id))
    overwrites={guild.default_role:discord.PermissionOverwrite(view_channel=False)}
    for uid in (tx["buyer_id"],tx["seller_id"]):
        m=guild.get_member(uid)
        if m:overwrites[m]=discord.PermissionOverwrite(view_channel=True,send_messages=True,read_message_history=True)
    staff_id=await find_config(guild.id,"SHOP_STAFF_ROLE_ID")
    if staff_id:
        r=guild.get_role(int(staff_id))
        if r:overwrites[r]=discord.PermissionOverwrite(view_channel=True,send_messages=True,read_message_history=True)
    overwrites[guild.me]=discord.PermissionOverwrite(view_channel=True,send_messages=True,manage_channels=True)
    ch=await guild.create_text_channel(f"取引-{tx['transaction_id']:06d}",category=cat,overwrites=overwrites)
    await ch.send(embed=ticket_embed(tx),view=TicketView(bot,tx["transaction_id"]))
    return ch
bot.create_ticket=create_ticket

async def refresh_ticket(ch,transaction_id):
    tx=await svc.transaction(transaction_id)
    async for m in ch.history(limit=20,oldest_first=True):
        if m.author.id==bot.user.id and m.embeds and m.embeds[0].title and m.embeds[0].title.startswith("🎫"):
            await m.edit(embed=ticket_embed(tx),view=TicketView(bot,transaction_id));break
bot.refresh_ticket=refresh_ticket

async def finish_ticket(ch,transaction_id):
    await refresh_ticket(ch,transaction_id)
    await asyncio.sleep(600)
    try:await ch.delete(reason="SHOP取引終了10分経過")
    except discord.NotFound:pass
bot.finish_ticket=finish_ticket

@tasks.loop(minutes=5)
async def deadline_watch():
    # 72時間未対応をDB時刻基準でSTAFF_REVIEWへ。
    rows=await db.fetch("""UPDATE shop.transactions
        SET status='STAFF_REVIEW',previous_status='SELLER_ACTION_REQUIRED',updated_at=NOW()
        WHERE status='SELLER_ACTION_REQUIRED' AND created_at <= NOW()-INTERVAL '72 hours'
        RETURNING *""")
    for tx in rows:
        g=bot.get_guild(tx["guild_id"]); ch=g.get_channel(tx["ticket_channel_id"]) if g and tx["ticket_channel_id"] else None
        if ch:
            await ch.send("🚨 店主未対応72時間のためSTAFF REVIEWへ移行しました。")
            await refresh_ticket(ch,tx["transaction_id"])

@deadline_watch.before_loop
async def before_deadline(): await bot.wait_until_ready()

bot.run(os.environ["DISCORD_TOKEN"])
