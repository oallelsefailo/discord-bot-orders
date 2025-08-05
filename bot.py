import discord
from discord import app_commands
from discord.ext import commands
import pyodbc
import os
from dotenv import load_dotenv

load_dotenv()

# Your Discord bot token from the .env file
TOKEN = os.getenv("DISCORD_TOKEN")

# Your server ID (as an integer)
GUILD_ID = discord.Object(id=195008314684211200)

# SQL Server connection string (Windows Auth)
conn_str = (
    r'DRIVER={SQL Server};'
    r'SERVER=SMARTSTORE2020\SMARTSTORE;'
    r'DATABASE=store_db;'
    r'Trusted_Connection=yes;'
)

# Connect to MSSQL and run query
def get_orderbot_flag2_count():
    query = """
    SELECT COUNT(*) FROM sales_orders
    WHERE 
        order_type = 11
        AND order_flag = 2
        AND CAST(added_date AS DATE) >= CAST(DATEADD(DAY, -2, GETDATE()) AS DATE)
    """
    with pyodbc.connect(conn_str) as conn:
        cursor = conn.cursor()
        cursor.execute(query)
        return cursor.fetchone()[0]

# Initialize bot
intents = discord.Intents.default()
client = commands.Bot(command_prefix="!", intents=intents)
tree = client.tree

# Slash command registration - with GUILD specified
@tree.command(name="orderbot", description="Get today's Flag 2 order count", guild=GUILD_ID)
async def orderbot(interaction: discord.Interaction):
    await interaction.response.defer()
    count = get_orderbot_flag2_count()
    await interaction.followup.send(f"🧾 Flag 2 count for today: **{count}**")

# Bot startup logic
@client.event
async def on_ready():
    print(f"✅ Logged in as {client.user} (ID: {client.user.id})")
    await tree.sync(guild=GUILD_ID)
    print("✅ Slash commands synced.")

# Run the bot
client.run(TOKEN)
