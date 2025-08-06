# OrderBot (Discord + MSSQL)

This is my personal Discord bot that pulls the total count of `flag = 2` orders from our SQL Server database using a slash command. It checks the past 3 days (including today) and returns the result when I type `/orderbot`.

## 🔧 Setup Notes

I already have the following installed:
- Python 3.12
- `discord.py`, `pyodbc`, `python-dotenv`

To install dependencies if needed:
```bash
pip install discord.py pyodbc python-dotenv
```

To manually run the bot (when testing something in VSCode or terminal):
```bash
python bot.py
```

## 🪟 Running as a Background Windows Service

Using NSSM to keep the bot running even when VSCode is closed.

### Service Control Commands

```bash
"C:\nssm-2.24\win64\nssm.exe" start DiscordOrderBot
"C:\nssm-2.24\win64\nssm.exe" stop DiscordOrderBot
"C:\nssm-2.24\win64\nssm.exe" restart DiscordOrderBot
```

> No need to touch anything in VSCode or the terminal once it's set as a service.

## ✅ Slash Command

### `/orderbot`

This sends back the count of orders where:
- `order_type = 11`
- `order_flag = 2`
- `added_date` is within the last 3 days

That's it. I made this so I don't have to open SSMS or run queries manually.

## 💭 Notes to Self

- The bot must be restarted after code changes.
- Enabled logging incase of errors.

