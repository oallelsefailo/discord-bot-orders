import os
import logging
import discord
from discord import app_commands
from discord.ext import commands
import pyodbc
from dotenv import load_dotenv

load_dotenv()

# ---------- Logging ----------
logging.basicConfig(
    filename="discordbot.log",
    filemode="a",
    format="%(asctime)s - %(levelname)s - %(message)s",
    level=logging.INFO
)

# ---------- Discord config ----------
TOKEN = os.getenv("DISCORD_TOKEN")
GUILD_ID = discord.Object(id=195008314684211200)

# ---------- SQL Server (Windows Auth) ----------
conn_str = (
    r"DRIVER={SQL Server};"
    r"SERVER=SMARTSTORE2020\SMARTSTORE;"
    r"DATABASE=store_db;"
    r"Trusted_Connection=yes;"
)

# ---------- Queries ----------
FLAG2_SQL = """
SELECT COUNT(*) 
FROM sales_orders
WHERE order_type = 11
  AND order_flag = 2
  AND CAST(added_date AS DATE) >= CAST(DATEADD(DAY, -2, GETDATE()) AS DATE);
"""

# Parameterized summary query. Accepts either internal order_id OR Magento order number (sales_orders.po_no).
ORDER_SUMMARY_SQL = r"""
DECLARE @order_token NVARCHAR(64);
SET @order_token = ?;  -- pyodbc binds here

DECLARE @order_no NUMERIC(20,0) = NULL;

-- normalize token: trim, strip a leading '#'
DECLARE @token NVARCHAR(64) = LTRIM(RTRIM(@order_token));
IF LEFT(@token,1) = '#'
    SET @token = SUBSTRING(@token, 2, 63);

-- numeric-only? could be order_id or order_seq
DECLARE @maybe_num NUMERIC(20,0) = CASE 
    WHEN @token NOT LIKE '%[^0-9]%' AND LEN(@token) > 0 THEN TRY_CAST(@token AS NUMERIC(20,0)) 
    ELSE NULL 
END;

-- 1) direct internal order_id
IF @maybe_num IS NOT NULL AND @order_no IS NULL
BEGIN
    SELECT TOP 1 @order_no = o.order_id
    FROM sales_orders AS o
    WHERE o.order_id = @maybe_num;
END

-- 2) exact match on po_no (web/Magento orders). We already stripped '#', so compare to @token.
IF @order_no IS NULL
BEGIN
    SELECT TOP 1 @order_no = o.order_id
    FROM sales_orders AS o
    WHERE LTRIM(RTRIM(o.po_no)) = @token;
END

-- 3) numeric line order_seq → order_id
IF @maybe_num IS NOT NULL AND @order_no IS NULL
BEGIN
    SELECT TOP 1 @order_no = l.order_id
    FROM sales_order_lines AS l
    WHERE l.order_seq = @maybe_num;
END

IF @order_no IS NULL
BEGIN
    SELECT CAST('Not found: ' + @order_token AS NVARCHAR(4000)) AS summary;
    RETURN;
END

;WITH so AS (
    SELECT o.order_id, magento_no = NULLIF(LTRIM(RTRIM(o.po_no)), '')
    FROM sales_orders o
    WHERE o.order_id = @order_no
),
line_data AS (
    SELECT
        l.order_id,
        sku = CASE 
                WHEN LTRIM(RTRIM(l.part_no)) LIKE 'ZZ%' 
                    THEN SUBSTRING(LTRIM(RTRIM(l.part_no)), 3, 100)
                ELSE LTRIM(RTRIM(l.part_no))
              END,
        qty = CAST(l.order_qty AS INT),
        line_order = l.order_seq
    FROM sales_order_lines l
    WHERE l.order_id = @order_no
      AND (l.cancelled_flag IS NULL OR l.cancelled_flag <> 'Y')
),
ship AS (
    SELECT TOP 1
        ship_via =
          CASE 
            WHEN v.ship_via_description IS NULL OR LTRIM(RTRIM(v.ship_via_description)) = '' 
                 THEN 'Code ' + ISNULL(CAST(o.via_code AS VARCHAR(50)),'')
            ELSE REPLACE(LTRIM(RTRIM(v.ship_via_description)), 'Fedex', 'FedEx')
          END,
        fob_point = COALESCE(i.fob_point, o.fob_point)
    FROM sales_orders o
    LEFT JOIN ship_vias v ON v.via_code = o.via_code
    OUTER APPLY (
        SELECT TOP 1 inv.fob_point
        FROM invoices inv
        WHERE inv.order_id = o.order_id
        ORDER BY inv.invoice_date DESC
    ) i
    WHERE o.order_id = @order_no
)
SELECT
    summary =
        'Order # ' + CAST(@order_no AS VARCHAR(20)) +
        CASE WHEN s0.magento_no IS NOT NULL THEN ' (Magento # ' + s0.magento_no + ')' ELSE '' END +
        ' | ' +
        ISNULL(STUFF((
            SELECT ' | ' + ld2.sku + ' x ' + CAST(ld2.qty AS VARCHAR(32))
            FROM line_data ld2
            ORDER BY ld2.line_order
            FOR XML PATH(''), TYPE
        ).value('.', 'nvarchar(max)'), 1, 3, ''), '(no active lines)') +
        ' | Shipped: ' + ISNULL(s.ship_via, 'Unknown') +
        ' | FOB: '   + ISNULL(CAST(s.fob_point AS VARCHAR(50)), 'Unknown')
FROM ship s
CROSS JOIN so s0;
"""

# ---------- DB helpers ----------
def get_flag2_count() -> int:
    with pyodbc.connect(conn_str) as conn:
        cur = conn.cursor()
        cur.execute(FLAG2_SQL)
        return int(cur.fetchone()[0])

def get_order_summary(order_token: str) -> str:
    token = order_token.strip()[:64]
    if token.startswith("#"):
        token = token[1:]
    with pyodbc.connect(conn_str) as conn:
        cur = conn.cursor()
        cur.execute(ORDER_SUMMARY_SQL, (token,))
        row = cur.fetchone()
        if not row or row[0] is None:
            return f"Not found: {order_token.strip()}"
        return str(row[0])
    
def style_summary(summary: str) -> str:
    parts = [p.strip() for p in summary.split("|")]
    if len(parts) < 3:
        return f"🚚 {summary}" 

    order_part = parts[0]
    item_parts = [f"`{p.strip().replace(' x ', ' × ')}`" for p in parts[1:-2]]

    # Plain labels, single line with pipe separator
    shipped_part = parts[-2].strip()  
    fob_part = parts[-1].strip()    
    shipped_fob_line = f"{shipped_part} | {fob_part}"

    # Format: Order # **[number]** (Magento *#[number]*)
    if "(Magento #" in order_part:
        left, magento = order_part.split("(Magento #", 1)
        order_num = left.split("Order #", 1)[1].strip()
        magento_num = magento.strip(" )")
        order_part = f"Order # **{order_num}** (Magento *#{magento_num}*)"
    else:
        order_num = order_part.split("Order #", 1)[1].strip()
        order_part = f"Order # **{order_num}**"

    items_line = " • ".join(item_parts)
    return f"🚚 {order_part}\n{items_line}\n{shipped_fob_line}"

# ---------- Bot setup ----------
intents = discord.Intents.default()
client = commands.Bot(command_prefix="!", intents=intents)
tree = client.tree

# Create a slash-command GROUP: /orderbot ...
orderbot_group = app_commands.Group(name="orderbot", description="Order tools")

@orderbot_group.command(name="flag2", description="Get Flag 2 order count (last 2 days)")
async def orderbot_flag2(interaction: discord.Interaction):
    try:
        await interaction.response.defer()
        count = get_flag2_count()
        await interaction.followup.send(f"🧾 Flag 2 count: **{count}**")
        logging.info(f"Handled /orderbot flag2. Count: {count}")
    except Exception as e:
        logging.error(f"Error in /orderbot flag2: {e}")
        await interaction.followup.send("⚠️ Error fetching Flag 2 count.")

@orderbot_group.command(name="order", description="Get order summary by internal ID or Magento number")
@app_commands.describe(number="Internal order_id (e.g., 18XXXX) or Magento order # (e.g., 1000XXXX)")
async def orderbot_order(interaction: discord.Interaction, number: str):
    try:
        await interaction.response.defer()
        summary = get_order_summary(number)
        styled = style_summary(summary)
        await interaction.followup.send(styled)
        logging.info(f"Handled /orderbot order. Token: {number} -> {summary}")
    except Exception as e:
        logging.error(f"Error in /orderbot order: {e}")
        await interaction.followup.send("⚠️ Error fetching order summary.")

# Register the group on the guild
tree.add_command(orderbot_group, guild=GUILD_ID)

@client.event
async def on_ready():
    logging.info(f"Logged in as {client.user} (ID: {client.user.id})")
    await tree.sync(guild=GUILD_ID)
    logging.info("Slash commands synced.")

client.run(TOKEN)
