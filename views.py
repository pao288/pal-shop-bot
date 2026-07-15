import discord
import shop_service as svc

def money(n): return f"{int(n):,} PAL"

class OpenShopModal(discord.ui.Modal, title="🏪 お店を開く"):
    shop_name = discord.ui.TextInput(label="店名", max_length=80)
    shop_description = discord.ui.TextInput(label="店の説明", style=discord.TextStyle.paragraph, max_length=1000)
    product_name = discord.ui.TextInput(label="最初の商品名", max_length=100)
    product_description = discord.ui.TextInput(label="商品説明", style=discord.TextStyle.paragraph, max_length=1000)
    product_price = discord.ui.TextInput(label="商品価格 PAL", placeholder="50000", max_length=18)

    def __init__(self, bot):
        super().__init__()
        self.bot = bot

    async def on_submit(self, i):
        if await svc.get_user_shop(i.guild_id, i.user.id):
            return await i.response.send_message("🏪 あなたはすでに店舗を持っています。", ephemeral=True)
        try:
            price = int(self.product_price.value.replace(",", "").strip())
            if price < 1: raise ValueError
        except ValueError:
            return await i.response.send_message("💰 商品価格は1 PAL以上の整数で入力してください。", ephemeral=True)
        await i.response.defer(ephemeral=True)
        shop = await svc.create_shop(i.guild_id, i.user.id, self.shop_name.value, self.shop_description.value)
        await svc.add_product(shop["shop_id"], self.product_name.value, self.product_description.value, price)
        try:
            await self.bot.publish_shop(i.guild, shop)
        except Exception as e:
            return await i.followup.send(f"店舗DBは作成済みです。フォーラム投稿でエラー: `{type(e).__name__}: {e}`", ephemeral=True)
        await i.followup.send("🏪 開店しました！", ephemeral=True)

class AddProductModal(discord.ui.Modal, title="📦 商品を追加"):
    name = discord.ui.TextInput(label="商品名", max_length=100)
    description = discord.ui.TextInput(label="商品説明", style=discord.TextStyle.paragraph, max_length=1000)
    price = discord.ui.TextInput(label="価格 PAL", placeholder="50000", max_length=18)

    def __init__(self, bot, shop_id):
        super().__init__()
        self.bot, self.shop_id = bot, shop_id

    async def on_submit(self, i):
        try:
            price = int(self.price.value.replace(",", "").strip())
            if price < 1: raise ValueError
        except ValueError:
            return await i.response.send_message("💰 価格は1 PAL以上の整数で入力してください。", ephemeral=True)
        p = await svc.add_product(self.shop_id, self.name.value, self.description.value, price)
        await i.response.send_message(f"📦 **{p['name']}** を追加しました。", ephemeral=True)
        await self.bot.refresh_shop(self.shop_id)
        await self.bot.announce_product(i.guild, self.shop_id, p)

class ShopSetupView(discord.ui.View):
    def __init__(self, bot):
        super().__init__(timeout=None)
        self.bot = bot

    @discord.ui.button(label="お店を開く", emoji="🏪", style=discord.ButtonStyle.primary, custom_id="shop:open")
    async def open_shop(self, i, b):
        await i.response.send_modal(OpenShopModal(self.bot))

    @discord.ui.button(label="自分のお店", emoji="📦", style=discord.ButtonStyle.secondary, custom_id="shop:mine")
    async def mine(self, i, b):
        shop = await svc.get_user_shop(i.guild_id, i.user.id)
        if not shop:
            return await i.response.send_message("🏪 まだ店舗を持っていません。", ephemeral=True)

        if not shop["forum_thread_id"] or not shop["panel_message_id"]:
            await i.response.defer(ephemeral=True)
            try:
                await self.bot.publish_shop(i.guild, shop)
            except Exception as e:
                return await i.followup.send(
                    f"❌ フォーラム投稿エラー: `{type(e).__name__}: {e}`",
                    ephemeral=True
                )
            return await i.followup.send(
                f"🏪 **{shop['name']}** をショップフォーラムへ公開しました！",
                ephemeral=True
            )

        thread = i.guild.get_channel(shop["forum_thread_id"])
        if thread:
            return await i.response.send_message(
                f"🏪 **{shop['name']}**\n状態: **{shop['status']}**\n\n{thread.mention}",
                ephemeral=True
            )

        await i.response.defer(ephemeral=True)
        try:
            await self.bot.publish_shop(i.guild, shop)
        except Exception as e:
            return await i.followup.send(
                f"❌ フォーラム再投稿エラー: `{type(e).__name__}: {e}`",
                ephemeral=True
            )
        await i.followup.send(
            f"🏪 **{shop['name']}** をショップフォーラムへ再公開しました！",
            ephemeral=True
        )

class ShopPanelView(discord.ui.View):
    def __init__(self, bot, shop_id):
        super().__init__(timeout=None)
        self.bot, self.shop_id = bot, int(shop_id)
        self.add_item(discord.ui.Button(label="商品一覧", emoji="📦", style=discord.ButtonStyle.primary,
                                        custom_id=f"shop:products:{self.shop_id}"))
        self.add_item(discord.ui.Button(label="店舗情報", emoji="ℹ️", style=discord.ButtonStyle.secondary,
                                        custom_id=f"shop:info:{self.shop_id}"))
        self.add_item(discord.ui.Button(label="店舗管理", emoji="⚙️", style=discord.ButtonStyle.secondary,
                                        custom_id=f"shop:manage:{self.shop_id}"))

    async def interaction_check(self, i):
        cid = i.data.get("custom_id", "")
        if cid.startswith("shop:products:"):
            ps = await svc.products(self.shop_id)
            if not ps: return await i.response.send_message("📦 商品はありません。", ephemeral=True) or False
            options=[discord.SelectOption(label=p["name"][:100], description=f"{money(p['price'])} / {p['status']}", value=str(p["product_id"])) for p in ps[:25]]
            return await i.response.send_message("📦 購入する商品を選択してください。", view=ProductSelectView(self.bot, options), ephemeral=True) or False
        if cid.startswith("shop:info:"):
            row = await svc.db.fetchrow("SELECT * FROM shop.shops WHERE shop_id=$1", self.shop_id)
            return await i.response.send_message(f"🏪 **{row['name']}**\n\n{row['description']}\n\n状態: **{row['status']}**", ephemeral=True) or False
        if cid.startswith("shop:manage:"):
            row = await svc.db.fetchrow("SELECT * FROM shop.shops WHERE shop_id=$1", self.shop_id)
            if i.user.id != row["owner_id"] and not i.user.guild_permissions.administrator:
                return await i.response.send_message("⚙️ 店主用メニューです。", ephemeral=True) or False
            return await i.response.send_message("⚙️ 店舗管理", view=ManageView(self.bot, self.shop_id), ephemeral=True) or False
        return True

class ProductSelect(discord.ui.Select):
    def __init__(self, bot, options):
        super().__init__(placeholder="商品を選択", options=options)
        self.bot = bot
    async def callback(self, i):
        p = await svc.get_product(int(self.values[0]))
        if not p or p["status"] != "active" or p["shop_status"] != "active":
            return await i.response.send_message("現在購入できません。", ephemeral=True)
        e=discord.Embed(title=f"📦 {p['name']}", description=p["description"])
        e.add_field(name="💰 価格", value=money(p["price"]))
        await i.response.send_message(embed=e, view=BuyView(self.bot, p["product_id"]), ephemeral=True)

class ProductSelectView(discord.ui.View):
    def __init__(self, bot, options):
        super().__init__(timeout=300)
        self.add_item(ProductSelect(bot, options))

class BuyView(discord.ui.View):
    def __init__(self, bot, product_id):
        super().__init__(timeout=300)
        self.bot, self.product_id = bot, int(product_id)
    @discord.ui.button(label="購入する", emoji="🛒", style=discord.ButtonStyle.success)
    async def buy(self, i, b):
        await i.response.defer(ephemeral=True)
        p = await svc.get_product(self.product_id)
        if not p or p["status"] != "active" or p["shop_status"] != "active":
            return await i.followup.send("現在購入できません。", ephemeral=True)
        if i.user.id == p["owner_id"]:
            return await i.followup.send("自分の商品です。", ephemeral=True)
        ok, msg = await self.bot.reserve_pal(i.guild_id, i.user.id, p["owner_id"], p["price"])
        if not ok:
            return await i.followup.send(msg, ephemeral=True)
        tx = await svc.create_transaction(i.guild_id, p, i.user.id)
        ch = await self.bot.create_ticket(i.guild, tx)
        await svc.set_ticket(tx["transaction_id"], ch.id)
        await i.followup.send(f"🎫 取引チケットを作成しました: {ch.mention}", ephemeral=True)

class ManageView(discord.ui.View):
    def __init__(self, bot, shop_id):
        super().__init__(timeout=300)
        self.bot, self.shop_id = bot, shop_id
    @discord.ui.button(label="商品追加", emoji="➕", style=discord.ButtonStyle.primary)
    async def add(self, i, b):
        await i.response.send_modal(AddProductModal(self.bot, self.shop_id))
    @discord.ui.button(label="閉店 / 営業再開", emoji="🔁", style=discord.ButtonStyle.secondary)
    async def toggle(self, i, b):
        row=await svc.db.fetchrow("SELECT status FROM shop.shops WHERE shop_id=$1", self.shop_id)
        new="closed" if row["status"]=="active" else "active"
        await svc.db.execute("UPDATE shop.shops SET status=$2,updated_at=NOW() WHERE shop_id=$1",self.shop_id,new)
        await self.bot.refresh_shop(self.shop_id)
        await i.response.send_message(f"状態を **{new}** に変更しました。", ephemeral=True)

class TicketView(discord.ui.View):
    def __init__(self, bot, transaction_id):
        super().__init__(timeout=None)
        self.bot, self.transaction_id = bot, int(transaction_id)

    @discord.ui.button(label="商品を渡しました", emoji="📦", style=discord.ButtonStyle.primary, custom_id="shop:ticket:delivered")
    async def delivered(self, i, b):
        tx=await svc.transaction(self.transaction_id)
        if i.user.id != tx["seller_id"]: return await i.response.send_message("店主用です。", ephemeral=True)
        if tx["status"]!="SELLER_ACTION_REQUIRED": return await i.response.send_message("現在この操作はできません。", ephemeral=True)
        await svc.set_transaction_status(self.transaction_id,"BUYER_CONFIRMATION_REQUIRED")
        await i.response.send_message("📦 商品を渡した状態にしました。購入者は受取確認してください。")
        await self.bot.refresh_ticket(i.channel, self.transaction_id)

    @discord.ui.button(label="受け取りました", emoji="✅", style=discord.ButtonStyle.success, custom_id="shop:ticket:received")
    async def received(self, i, b):
        tx=await svc.transaction(self.transaction_id)
        if i.user.id != tx["buyer_id"]: return await i.response.send_message("購入者用です。", ephemeral=True)
        if tx["status"]!="BUYER_CONFIRMATION_REQUIRED": return await i.response.send_message("店主の受け渡し待ちです。", ephemeral=True)
        ok,msg=await self.bot.release_pal(tx)
        if not ok: return await i.response.send_message(msg, ephemeral=True)
        await svc.set_transaction_status(self.transaction_id,"COMPLETED")
        await i.response.send_message("✅ 取引完了！")
        await self.bot.finish_ticket(i.channel, self.transaction_id)

    @discord.ui.button(label="問題があります", emoji="⚠️", style=discord.ButtonStyle.danger, custom_id="shop:ticket:problem")
    async def problem(self, i, b):
        tx=await svc.transaction(self.transaction_id)
        if i.user.id not in (tx["buyer_id"],tx["seller_id"]): return await i.response.send_message("取引参加者用です。", ephemeral=True)
        await svc.set_transaction_status(self.transaction_id,"STAFF_REVIEW",tx["status"])
        await i.response.send_message("🚨 STAFF REVIEWへ移行しました。")
        await self.bot.refresh_ticket(i.channel,self.transaction_id)

    @discord.ui.button(label="取引キャンセル", emoji="❌", style=discord.ButtonStyle.secondary, custom_id="shop:ticket:cancel")
    async def cancel(self, i, b):
        tx=await svc.transaction(self.transaction_id)
        if i.user.id not in (tx["buyer_id"],tx["seller_id"]): return await i.response.send_message("取引参加者用です。", ephemeral=True)
        await svc.set_transaction_status(self.transaction_id,"CANCEL_PENDING",tx["status"])
        await i.response.send_message("❌ キャンセル申請。相手は下から選択してください。",view=CancelDecisionView(self.bot,self.transaction_id))

class CancelDecisionView(discord.ui.View):
    def __init__(self,bot,transaction_id):
        super().__init__(timeout=None); self.bot=bot; self.transaction_id=transaction_id
    @discord.ui.button(label="キャンセルに同意",style=discord.ButtonStyle.danger)
    async def agree(self,i,b):
        tx=await svc.transaction(self.transaction_id)
        ok,msg=await self.bot.refund_pal(tx)
        if not ok:return await i.response.send_message(msg,ephemeral=True)
        await svc.set_transaction_status(self.transaction_id,"REFUNDED")
        await i.response.send_message("↩️ 全額返金し、取引を終了しました。")
        await self.bot.finish_ticket(i.channel,self.transaction_id)
    @discord.ui.button(label="取引を続ける",style=discord.ButtonStyle.primary)
    async def continue_(self,i,b):
        tx=await svc.transaction(self.transaction_id)
        await svc.set_transaction_status(self.transaction_id,tx["previous_status"] or "SELLER_ACTION_REQUIRED")
        await i.response.send_message("🔁 取引を継続します。")
        await self.bot.refresh_ticket(i.channel,self.transaction_id)
