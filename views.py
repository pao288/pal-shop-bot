import discord

def svc(i): return i.client.shop_service

class OpenShopModal(discord.ui.Modal, title="🏪 お店を開く"):
    shop_name = discord.ui.TextInput(label="店名", max_length=80)
    shop_description = discord.ui.TextInput(label="店の説明", style=discord.TextStyle.paragraph, max_length=1000)
    product_name = discord.ui.TextInput(label="最初の商品名", max_length=100)
    product_description = discord.ui.TextInput(label="商品説明", style=discord.TextStyle.paragraph, max_length=1000)
    product_price = discord.ui.TextInput(label="商品価格 PAL", placeholder="50000", max_length=18)

    async def on_submit(self, i):
        try:
            price = int(self.product_price.value)
            if price < 1: raise ValueError
        except ValueError:
            return await i.response.send_message("商品価格は1 PAL以上の整数で入力してね。", ephemeral=True)
        await i.response.defer(ephemeral=True)
        ok, result = await svc(i).create_shop(
            i.guild, i.user, self.shop_name.value, self.shop_description.value,
            self.product_name.value, self.product_description.value, price
        )
        text = "🏪 お店を開店しました！" if ok else f"作成結果: {result}"
        await i.followup.send(text, ephemeral=True)

class ShopPanelView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="🏪 お店を開く", style=discord.ButtonStyle.primary, custom_id="shop:open")
    async def open_shop(self, i, b):
        await i.response.send_modal(OpenShopModal())

    @discord.ui.button(label="🛍️ お店を見る", style=discord.ButtonStyle.secondary, custom_id="shop:browse")
    async def browse(self, i, b):
        await i.response.send_message("指定フォーラムの店舗一覧からお店を選んでね。", ephemeral=True)

class StoreView(discord.ui.View):
    def __init__(self, shop_id):
        super().__init__(timeout=None)
        self.shop_id = int(shop_id)
        self.children[0].custom_id = f"store:products:{self.shop_id}"
        self.children[1].custom_id = f"store:info:{self.shop_id}"
        self.children[2].custom_id = f"store:manage:{self.shop_id}"

    @discord.ui.button(label="📦 商品一覧", style=discord.ButtonStyle.primary, custom_id="store:products")
    async def products(self, i, b):
        rows = await svc(i).products(self.shop_id)
        if not rows:
            return await i.response.send_message("現在販売中の商品はありません。", ephemeral=True)
        await i.response.send_message(
            embed=discord.Embed(title="📦 商品一覧", description="\n\n".join(
                f"**#{r['product_id']} {r['name']}**\n{r['description']}\n💰 {r['price']:,} PAL" for r in rows[:10]
            )),
            view=ProductListView(rows[:10]), ephemeral=True
        )

    @discord.ui.button(label="ℹ️ 店舗情報", style=discord.ButtonStyle.secondary, custom_id="store:info")
    async def info(self, i, b):
        async with svc(i).pool.acquire() as con:
            s = await con.fetchrow("SELECT * FROM shop.shops WHERE shop_id=$1", self.shop_id)
        await i.response.send_message(f"🏪 {s['name']}\n\n{s['description']}\n\n👤 <@{s['owner_id']}>", ephemeral=True)

    @discord.ui.button(label="⚙️ 店舗管理", style=discord.ButtonStyle.secondary, custom_id="store:manage")
    async def manage(self, i, b):
        async with svc(i).pool.acquire() as con:
            s = await con.fetchrow("SELECT * FROM shop.shops WHERE shop_id=$1", self.shop_id)
        if not s or s["owner_id"] != i.user.id:
            return await i.response.send_message("店主専用です。", ephemeral=True)
        await i.response.send_message("⚙️ 店舗管理", view=StoreManageView(self.shop_id), ephemeral=True)

class ProductListView(discord.ui.View):
    def __init__(self, rows):
        super().__init__(timeout=300)
        options=[discord.SelectOption(label=r["name"][:100], description=f"{r['price']:,} PAL", value=str(r["product_id"])) for r in rows]
        select=discord.ui.Select(placeholder="購入する商品を選択", options=options)
        select.callback=self.selected
        self.add_item(select)

    async def selected(self, i):
        pid=int(i.data["values"][0])
        await i.response.defer(ephemeral=True)
        ok, result=await svc(i).start_purchase(i, pid)
        await i.followup.send("🎫 取引チケットを作成しました！" if ok else f"購入結果: {result}", ephemeral=True)

class AddProductModal(discord.ui.Modal, title="📦 商品追加"):
    name = discord.ui.TextInput(label="商品名", max_length=100)
    description = discord.ui.TextInput(label="商品説明", style=discord.TextStyle.paragraph, max_length=1000)
    price = discord.ui.TextInput(label="価格 PAL", placeholder="50000", max_length=18)
    def __init__(self, shop_id):
        super().__init__(); self.shop_id=shop_id
    async def on_submit(self, i):
        try:
            price=int(self.price.value)
            if price < 1: raise ValueError
        except ValueError:
            return await i.response.send_message("価格は1 PAL以上の整数で入力してね。", ephemeral=True)
        await i.response.defer(ephemeral=True)
        ok,res=await svc(i).add_product(i.guild,self.shop_id,i.user.id,self.name.value,self.description.value,price)
        await i.followup.send("📦 商品を追加しました！" if ok else f"追加結果: {res}", ephemeral=True)

class StoreManageView(discord.ui.View):
    def __init__(self, shop_id):
        super().__init__(timeout=300); self.shop_id=shop_id
    @discord.ui.button(label="➕ 商品追加", style=discord.ButtonStyle.success)
    async def add(self,i,b): await i.response.send_modal(AddProductModal(self.shop_id))
    @discord.ui.button(label="⏸️ 閉店 / 営業再開", style=discord.ButtonStyle.secondary)
    async def toggle(self,i,b):
        async with svc(i).pool.acquire() as con:
            s=await con.fetchrow("SELECT * FROM shop.shops WHERE shop_id=$1",self.shop_id)
            if not s or s["owner_id"]!=i.user.id:
                return await i.response.send_message("店主専用です。",ephemeral=True)
            new="paused" if s["status"]=="active" else "active"
            await con.execute("UPDATE shop.shops SET status=$1,updated_at=NOW() WHERE shop_id=$2",new,self.shop_id)
        await i.response.send_message("店舗状態を変更しました。",ephemeral=True)

class TransactionView(discord.ui.View):
    def __init__(self, txid):
        super().__init__(timeout=None); self.txid=int(txid)
        for n,c in enumerate(self.children): c.custom_id=f"tx:{n}:{self.txid}"
    @discord.ui.button(label="📦 商品を渡しました", style=discord.ButtonStyle.primary, custom_id="tx:delivered")
    async def delivered(self,i,b):
        ok=await svc(i).seller_delivered(self.txid,i.user.id)
        await i.response.send_message("購入者の受取確認待ちです。" if ok else "現在この操作は実行できません。",ephemeral=True)
    @discord.ui.button(label="✅ 受け取りました", style=discord.ButtonStyle.success, custom_id="tx:received")
    async def received(self,i,b):
        await i.response.defer(ephemeral=True)
        ok,res=await svc(i).buyer_received(self.txid,i.user.id)
        await i.followup.send("✅ 取引完了！" if ok else f"処理結果: {res}",ephemeral=True)
    @discord.ui.button(label="⚠️ 問題があります", style=discord.ButtonStyle.danger, custom_id="tx:problem")
    async def problem(self,i,b):
        ok=await svc(i).report_problem(self.txid,i.user.id)
        await i.response.send_message("🚨 運営確認へ移行しました。" if ok else "現在この操作は実行できません。",ephemeral=True)

class ShopAdminView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)
    async def interaction_check(self,i):
        if not i.user.guild_permissions.administrator:
            await i.response.send_message("管理者専用です。",ephemeral=True); return False
        return True
    @discord.ui.button(label="🏪 店舗管理", style=discord.ButtonStyle.primary, custom_id="admin:shops")
    async def shops(self,i,b): await i.response.send_message("店舗管理DB接続済み。",ephemeral=True)
    @discord.ui.button(label="📦 商品管理", style=discord.ButtonStyle.secondary, custom_id="admin:products")
    async def products(self,i,b): await i.response.send_message("商品管理DB接続済み。",ephemeral=True)
    @discord.ui.button(label="🎫 取引管理", style=discord.ButtonStyle.secondary, custom_id="admin:transactions")
    async def txs(self,i,b): await i.response.send_message("取引管理DB接続済み。",ephemeral=True)
    @discord.ui.button(label="🏛️ 公式ショップ管理", style=discord.ButtonStyle.secondary, custom_id="admin:official")
    async def official(self,i,b): await i.response.send_message("公式ショップ管理DB接続済み。",ephemeral=True)
    @discord.ui.button(label="📋 ログ確認", style=discord.ButtonStyle.secondary, custom_id="admin:logs")
    async def logs(self,i,b): await i.response.send_message("SHOPログDB接続済み。",ephemeral=True)
