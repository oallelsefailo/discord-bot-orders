import os
import logging
import discord
from discord import app_commands
from discord.ext import commands
import pyodbc
import math
import re
import asyncio
import mysql.connector
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

# ---------- MySQL (Magento) ----------
MYSQL_HOST = os.getenv("MYSQL_HOST")
MYSQL_PORT = int(os.getenv("MYSQL_PORT", "3306"))
MYSQL_USER = os.getenv("MYSQL_USER")
MYSQL_PASSWORD = os.getenv("MYSQL_PASSWORD")
MYSQL_DB = os.getenv("MYSQL_DB")

# ---------- Palletization constants ----------
PALLET_L = 42.0
PALLET_W = 48.0
PALLET_H = 5.0
PALLET_TARE_LB = 50.0
MAX_TOTAL_H = 65.0
EPS = 1e-9

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

# ---------- MySQL (Magento) query ----------
LAST_STATUS_CHANGE_SQL = """
SELECT
  g.increment_id,
  STR_TO_DATE(
    SUBSTRING_INDEX(g.order_status_change_log, ' at ', -1),
    '%Y-%m-%d %H:%i:%s'
  ) AS last_status_change_at,
  g.order_status_change_log
FROM sales_order_grid g
WHERE g.order_status_change_log IS NOT NULL
  AND g.order_status_change_log <> ''
  AND g.order_status_change_log <> 'N/A'
  AND DATE(g.created_at) = CURDATE()
ORDER BY last_status_change_at DESC
LIMIT 1;
"""

# ---------- DB helpers ----------
def get_flag2_count() -> int:
    with pyodbc.connect(conn_str) as conn:
        cur = conn.cursor()
        cur.execute(FLAG2_SQL)
        return int(cur.fetchone()[0])

def get_last_status_change_global():
    """
    Returns:
      (increment_id, datetime, log_line)
      or (None, None, None) if nothing found or connection fails
    """
    try:
        import mysql.connector

        conn = mysql.connector.connect(
            host=MYSQL_HOST,
            port=MYSQL_PORT,
            user=MYSQL_USER,
            password=MYSQL_PASSWORD,
            database=MYSQL_DB,
            connection_timeout=5,
        )
        try:
            cur = conn.cursor()
            cur.execute(LAST_STATUS_CHANGE_SQL)
            row = cur.fetchone()
            if not row:
                return (None, None, None)
            return (row[0], row[1], row[2])
        finally:
            conn.close()
    except Exception as e:
        logging.warning(f"MySQL connection failed in get_last_status_change_global: {e}")
        return (None, None, None)

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

# ---------- Palletization helpers ----------
def _fmt_in(x: float) -> str:
    return f"{x:.1f}".rstrip('0').rstrip('.') if abs(x - round(x)) > EPS else f"{int(round(x))}"

def _fmt_lb(x: float) -> str:
    return f"{x:.1f}".rstrip('0').rstrip('.') if abs(x - round(x)) > EPS else f"{int(round(x))}"

def parse_size(size_str: str) -> tuple[float, float, float]:
    s = size_str.lower().replace("×", "x").replace(",", "x")
    s = re.sub(r"\s*x\s*", "x", s)
    s = re.sub(r"\s+", "", s)
    parts = s.split("x")
    if len(parts) != 3:
        raise ValueError("Size must be like 'L x W x H' (inches).")
    try:
        L, W, H = (float(parts[0]), float(parts[1]), float(parts[2]))
    except Exception:
        raise ValueError("Size contains non-numeric values.")
    if L <= 0 or W <= 0 or H <= 0:
        raise ValueError("All dimensions must be > 0.")
    return (L, W, H)

def enumerate_orientations(L: float, W: float, H: float):
    return [
        # Up = H
        {"deck_x": L, "deck_y": W, "up_z": H, "deck_name_x": "L", "deck_name_y": "W", "up_name": "H"},
        {"deck_x": W, "deck_y": L, "up_z": H, "deck_name_x": "W", "deck_name_y": "L", "up_name": "H"},
        # Up = L
        {"deck_x": W, "deck_y": H, "up_z": L, "deck_name_x": "W", "deck_name_y": "H", "up_name": "L"},
        {"deck_x": H, "deck_y": W, "up_z": L, "deck_name_x": "H", "deck_name_y": "W", "up_name": "L"},
        # Up = W
        {"deck_x": L, "deck_y": H, "up_z": W, "deck_name_x": "L", "deck_name_y": "H", "up_name": "W"},
        {"deck_x": H, "deck_y": L, "up_z": W, "deck_name_x": "H", "deck_name_y": "L", "up_name": "W"},
    ]

def _fit_one_deck(deck_x: float, deck_y: float, pallet_long: float, pallet_wide: float):
    nx = math.floor((pallet_long + EPS) / deck_x)
    ny = math.floor((pallet_wide + EPS) / deck_y)
    return nx, ny, nx * ny

def fit_on_deck(deck_x: float, deck_y: float):
    nx1, ny1, pl1 = _fit_one_deck(deck_x, deck_y, PALLET_L, PALLET_W)
    nx2, ny2, pl2 = _fit_one_deck(deck_x, deck_y, PALLET_W, PALLET_L)

    if pl1 >= pl2:
        return {
            "per_layer": pl1,
            "nx": nx1,
            "ny": ny1,
            "deck_used_x": deck_x,
            "deck_used_y": deck_y,
            "pallet_long": PALLET_L,
            "pallet_wide": PALLET_W,
            "swapped": False,
        }
    else:
        return {
            "per_layer": pl2,
            "nx": nx2,
            "ny": ny2,
            "deck_used_x": deck_x,
            "deck_used_y": deck_y,
            "pallet_long": PALLET_W,
            "pallet_wide": PALLET_L,
            "swapped": True,
        }

def layers_max(up_z: float) -> int:
    usable = MAX_TOTAL_H - PALLET_H
    if up_z <= 0:
        return 0
    return max(0, math.floor((usable + EPS) / up_z))

def orientation_phrase(L: float, W: float, H: float, up_z: float) -> str:
    dims_sorted = sorted([L, W, H])
    smallest, middle, largest = dims_sorted[0], dims_sorted[1], dims_sorted[2]

    def close(a, b):
        return abs(a - b) <= 1e-6

    if close(up_z, smallest):
        return f"Lay flat ({_fmt_in(up_z)}\" per layer)"
    if close(up_z, largest):
        return f"Stand up ({_fmt_in(up_z)}\" per layer)"
    return f"On its side ({_fmt_in(up_z)}\" per layer)"

def palletize(qty: int, per_layer: int, up_z: float, each_weight_lb: float):
    pallets = []
    if per_layer <= 0:
        return pallets
    max_layers = layers_max(up_z)
    if max_layers <= 0:
        return pallets
    cap = per_layer * max_layers

    left = qty
    while left > 0:
        boxes = min(left, cap)
        layers_used = max(1, math.ceil(boxes / per_layer))
        height = PALLET_H + layers_used * up_z
        weight = PALLET_TARE_LB + boxes * each_weight_lb
        pallets.append({
            "boxes": boxes,
            "layers_used": layers_used,
            "height": height,
            "weight": weight,
        })
        left -= boxes
    return pallets

def score_orientations(L: float, W: float, H: float, forced_up: float = None):
    best = None
    for o in enumerate_orientations(L, W, H):
        if forced_up is not None and abs(o["up_z"] - forced_up) > EPS:
            continue

        deck = fit_on_deck(o["deck_x"], o["deck_y"])
        per_layer = deck["per_layer"]
        if per_layer <= 0:
            continue

        z = o["up_z"]
        lay = layers_max(z)
        if lay <= 0:
            continue

        cap = per_layer * lay
        cand = {
            "deck_x": o["deck_x"], "deck_y": o["deck_y"], "up_z": z,
            "deck_name_x": o["deck_name_x"], "deck_name_y": o["deck_name_y"], "up_name": o["up_name"],
            "per_layer": per_layer, "nx": deck["nx"], "ny": deck["ny"], "layers_max": lay,
            "cap": cap, "swapped": deck["swapped"],
            "deck_used_x": deck["deck_used_x"], "deck_used_y": deck["deck_used_y"],
            "pallet_long": deck["pallet_long"], "pallet_wide": deck["pallet_wide"],
        }
        if best is None:
            best = cand
        else:
            if (cand["cap"] > best["cap"] or
                (cand["cap"] == best["cap"] and cand["layers_max"] > best["layers_max"]) or
                (cand["cap"] == best["cap"] and cand["layers_max"] == best["layers_max"] and cand["per_layer"] > best["per_layer"]) or
                (cand["cap"] == best["cap"] and cand["layers_max"] == best["layers_max"] and cand["per_layer"] == best["per_layer"] and cand["up_z"] < best["up_z"] - EPS)):
                best = cand
    return best

# ---------- Bot setup ----------
intents = discord.Intents.default()
client = commands.Bot(command_prefix="!", intents=intents)
tree = client.tree

# Create a slash-command GROUP: /orderbot ...
orderbot_group = app_commands.Group(name="orderbot", description="Order tools")

@orderbot_group.command(
    name="flag2",
    description="Get Flag 2 order count (last 2 days) + last Magento status-change time"
)
async def orderbot_flag2(interaction: discord.Interaction):
    try:
        await interaction.response.defer()

        count = await asyncio.to_thread(get_flag2_count)
        inc_id, last_dt, log_line = await asyncio.to_thread(get_last_status_change_global)

        lines = [f"🧾 Flag 2 count: **{count}**"]

        if inc_id and last_dt:
            lines.append(
                f"🕒 Last Magento Status Change: "
                f"**{last_dt:%m/%d/%Y %I:%M:%S %p}** (Order *#{inc_id}*)"
            )
        else:
            lines.append("🕒 Last Magento Status Change: **(none found)**")

        await interaction.followup.send("\n".join(lines))
        logging.info(
            f"/orderbot flag2 -> count={count}, last_magento={inc_id}@{last_dt}"
        )

    except Exception as e:
        logging.error(f"Error in /orderbot flag2: {e}")
        await interaction.followup.send(
            "⚠️ Error fetching Flag 2 count or Magento status-change info."
        )

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

@orderbot_group.command(name="dim", description="Palletize boxes on 42x48x5 (max 65\")")
@app_commands.describe(
    size='Box size as L x W x H in inches (e.g., 27.3 x 15.9 x 32.9)',
    boxes="Total number of boxes",
    weight="Weight per box (lbs)",
    orientation='Which dimension points up when stacking? Enter L, W, or H. Leave blank for auto.'
)
async def orderbot_dim(interaction: discord.Interaction, size: str, boxes: int, weight: float, orientation: str = None):
    try:
        await interaction.response.defer()

        if boxes <= 0:
            await interaction.followup.send("⚠️ `boxes` must be > 0.")
            return
        if weight <= 0:
            await interaction.followup.send("⚠️ `weight` must be > 0.")
            return

        try:
            L, W, H = parse_size(size)
        except ValueError as ve:
            await interaction.followup.send(f"⚠️ {ve}")
            return

        # Resolve forced orientation filter
        forced_up = None
        orient_label = None
        if orientation is not None:
            o_clean = orientation.strip().upper()
            if o_clean not in ("L", "W", "H"):
                await interaction.followup.send('⚠️ `orientation` must be `L`, `W`, or `H` (which dimension points up), or leave it blank for auto.')
                return
            if o_clean == "L":
                forced_up = L
            elif o_clean == "W":
                forced_up = W
            else:
                forced_up = H
            orient_label = o_clean

        best = score_orientations(L, W, H, forced_up=forced_up)
        if best is None:
            any_footprint = any(fit_on_deck(o["deck_x"], o["deck_y"])["per_layer"] > 0 for o in enumerate_orientations(L, W, H))
            if not any_footprint:
                await interaction.followup.send("❌ Box footprint will not fit on a 42×48 pallet in any orientation.")
                return
            await interaction.followup.send("❌ Box exceeds the 65\" total height limit (60\" usable above pallet) in every orientation.")
            return

        # Orientation wording
        orient_text = orientation_phrase(L, W, H, best["up_z"])

        along_42 = best["deck_used_x"] if best["pallet_long"] == PALLET_L else best["deck_used_y"]
        along_48 = best["deck_used_y"] if best["pallet_long"] == PALLET_L else best["deck_used_x"]

        deck_line = (
            f'Deck: {_fmt_in(along_42)}" along 42", {_fmt_in(along_48)}" along 48"'
        )

        # Palletize
        pallets = palletize(boxes, best["per_layer"], best["up_z"], weight)

        # Build output
        size_display = re.sub(r"\s+", " ", size.strip())
        orient_input = f"; orientation: {orient_label}" if orient_label is not None else ""
        user_line = f"**User input:** [{size_display}; boxes: {boxes}; weight: {_fmt_lb(weight)} lb{orient_input}]"
        header = f"**Orientation:** {orient_text}\n{deck_line}"
        lines = [user_line, header]

        if len(pallets) == 1:
            p = pallets[0]
            lines.append(f"**Boxes:** {p['boxes']}")
            lines.append(f"**Layers used:** {p['layers_used']}")
            lines.append(f"**Height:** {_fmt_in(p['height'])}\"")
            lines.append(f"**Weight:** {_fmt_lb(p['weight'])} lbs")

        else:
            total_w = 0.0
            for idx, p in enumerate(pallets, start=1):
                lines.append(
                    f"**Pallet {idx}** — Boxes: {p['boxes']} • Layers used: {p['layers_used']} • "
                    f"Height: {_fmt_in(p['height'])}\" • Weight: {_fmt_lb(p['weight'])} lbs"
                )
                total_w += p["weight"]
            lines.append(f"**Total:** {boxes} boxes • {len(pallets)} pallets • {_fmt_lb(total_w)} lbs")

        await interaction.followup.send("\n".join(lines))

        logging.info(
            f'Handled /orderbot dim. size="{size}" boxes={boxes} weight={weight} orientation={orientation} '
            f'-> per_layer={best["per_layer"]} layers_max={best["layers_max"]}'
        )
    except Exception as e:
        logging.error(f"Error in /orderbot dim: {e}")
        await interaction.followup.send("⚠️ Error computing palletization.")

# Register the group on the guild
tree.add_command(orderbot_group, guild=GUILD_ID)

@client.event
async def on_ready():
    logging.info(f"Logged in as {client.user} (ID: {client.user.id})")
    await tree.sync(guild=GUILD_ID)
    logging.info("Slash commands synced.")

client.run(TOKEN)
