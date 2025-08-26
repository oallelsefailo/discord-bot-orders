# OrderBot (Discord + MSSQL)

A lightweight Discord bot that surfaces key order info from SQL Server with slash commands.

**Commands:**
- `/orderbot flag2` → count of recent **Flag 2** orders  
- `/orderbot order <number>` → clean, styled **order summary** by internal ID *or* Magento order #

---

## 🔧 Setup

**Prereqs**
- Python 3.12
- Packages: `discord.py`, `pyodbc`, `python-dotenv`

Install (if needed):
~~~bash
pip install discord.py pyodbc python-dotenv
~~~

**Environment**
- Create a `.env` file next to `bot.py` with:
  ~~~
  DISCORD_TOKEN=your-bot-token-here
  ~~~
- SQL Server connection uses Windows Authentication (see `conn_str` in `bot.py`).

**Run locally (for testing)**
~~~bash
python bot.py
~~~

---

## 🪟 Running as a Windows Service (NSSM)

The bot runs as a background service via NSSM. Restart after any code change.

Service control:
~~~bash
"C:\nssm-2.24\win64\nssm.exe" start DiscordOrderBot
"C:\nssm-2.24\win64\nssm.exe" stop  DiscordOrderBot
"C:\nssm-2.24\win64\nssm.exe" restart DiscordOrderBot
"C:\nssm-2.24\win64\nssm.exe" status DiscordOrderBot  
"C:\nssm-2.24\win64\nssm.exe" edit   DiscordOrderBot 
~~~

---

## ✅ Slash Commands

### `/orderbot flag2`
Returns count of orders where:
- `order_type = 11`
- `order_flag = 2`

---

### `/orderbot order <number>`
Accepts **internal order ID** (e.g., `18XXXX`) **or** **Magento order #**.


Notes:
- SKUs show as inline code with a proper “×”.
- “Shipped” and “FOB” are plain text separated by a pipe.
- Magento number is italicized; Order # is bold.

