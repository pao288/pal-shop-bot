import os
import logging
import discord
from discord.ext import commands
from shop_db import init_db
from shop_service import ShopService
from views import ShopPanelView, ShopAdminView

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("pal_shop")

intents = discord.Intents.default()
intents.members = True
intents.guilds = True

bot = commands.Bot(command_prefix="!", intents=intents)
service: ShopService | None = None

@bot.event
async def setup_hook():
    global service
    pool = await init_db(os.environ["DATABASE_URL"])
    service = ShopService(pool, bot)
    bot.shop_service = service
    bot.add_view(ShopPanelView())
    bot.add_view(ShopAdminView())
    await service.restore_persistent_views()
    log.info("DB・SHOP復旧完了")

@bot.event
async def on_ready():
    log.info("PAL SHOP起動完了: %s", bot.user)

@bot.event
async def on_member_remove(member: discord.Member):
    await service.handle_owner_left(member.guild.id, member.id)

@bot.command()
@commands.has_permissions(administrator=True)
async def shopsetup(ctx: commands.Context):
    embed = discord.Embed(
        title="🛍️ PAL SHOP",
        description="PALのマーケットへようこそ。\n\n自分のお店を開いたり、みんなの商品を購入できます。"
    )
    await ctx.send(embed=embed, view=ShopPanelView())

@bot.command()
@commands.has_permissions(administrator=True)
async def shopadmin(ctx: commands.Context):
    embed = discord.Embed(
        title="🛠️ SHOP ADMIN",
        description="店舗・商品・取引・公式ショップを管理します。"
    )
    await ctx.send(embed=embed, view=ShopAdminView())

bot.run(os.environ["DISCORD_TOKEN"])
