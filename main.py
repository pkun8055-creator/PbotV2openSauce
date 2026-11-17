from __future__ import annotations
import discord
from discord.ext import commands, tasks
from discord import app_commands
import os
import json
import re
import random
import datetime
import asyncio
import sqlite3
import aiohttp
import io
import time
import base64
from contextlib import closing
from functools import lru_cache
from pathlib import Path
from typing import Any, Optional
from urllib.parse import quote
from PIL import Image, ImageDraw, ImageFilter, ImageFont

# =====================
# タイムゾーン
# =====================
try:
    from zoneinfo import ZoneInfo
    JST = ZoneInfo("Asia/Tokyo")
except Exception:
    JST = datetime.timezone(datetime.timedelta(hours=9))

# =====================
# DB接続
# =====================
conn = sqlite3.connect("bot.db")
cursor = conn.cursor()

# =====================
# テーブル作成
# =====================
cursor.executescript("""
CREATE TABLE IF NOT EXISTS raid_texts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    text TEXT UNIQUE
);

CREATE TABLE IF NOT EXISTS user_data (
    user_id INTEGER,
    guild_id INTEGER,
    coins INTEGER DEFAULT 0,
    xp INTEGER DEFAULT 0,
    level INTEGER DEFAULT 0,
    last_message REAL DEFAULT 0,
    PRIMARY KEY (user_id, guild_id)
);

CREATE TABLE IF NOT EXISTS global_coins (
    user_id INTEGER PRIMARY KEY,
    coins INTEGER DEFAULT 0
);

CREATE TABLE IF NOT EXISTS count_game (
    guild_id INTEGER PRIMARY KEY,
    channel_id INTEGER,
    current_number INTEGER,
    x_number INTEGER,
    record INTEGER
);

CREATE TABLE IF NOT EXISTS reminders (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER,
    channel_id INTEGER,
    target_time TEXT,
    notification_type TEXT
);

CREATE TABLE IF NOT EXISTS level_roles (
    guild_id INTEGER,
    level INTEGER,
    role_id INTEGER,
    PRIMARY KEY (guild_id, level)
);

CREATE TABLE IF NOT EXISTS level_settings (
    guild_id INTEGER PRIMARY KEY,
    announce_channel INTEGER,
    log_channel INTEGER,
    announce_enabled INTEGER DEFAULT 1
);

CREATE TABLE IF NOT EXISTS webhook_log (
    guild_id INTEGER PRIMARY KEY,
    webhook_url TEXT
);

CREATE TABLE IF NOT EXISTS announce_settings (
    guild_id INTEGER PRIMARY KEY,
    channel_id INTEGER DEFAULT NULL,
    enabled INTEGER DEFAULT 1
);

CREATE TABLE IF NOT EXISTS global_webhook (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    webhook_url TEXT
);

CREATE TABLE IF NOT EXISTS hiroyuki_channels (
    guild_id INTEGER PRIMARY KEY,
    channel_id INTEGER
);

CREATE TABLE IF NOT EXISTS pr_rights (
    user_id INTEGER PRIMARY KEY,
    promo_text TEXT DEFAULT '',
    last_updated TEXT DEFAULT NULL
);

CREATE TABLE IF NOT EXISTS guild_settings (
    guild_id           INTEGER PRIMARY KEY,
    join_enabled       INTEGER DEFAULT 0,
    join_channel       INTEGER,
    join_message       TEXT    DEFAULT '{user} さんが参加しました！',
    leave_enabled      INTEGER DEFAULT 0,
    leave_channel      INTEGER,
    leave_message      TEXT    DEFAULT '{user} さんが退出しました',
    raid_enabled       INTEGER DEFAULT 1,
    raid_excluded_roles TEXT   DEFAULT '[]',
    report_channel     INTEGER,
    audit_enabled      INTEGER DEFAULT 0,
    audit_channel      INTEGER,
    audit_edit         INTEGER DEFAULT 1,
    audit_delete       INTEGER DEFAULT 1,
    audit_timeout      INTEGER DEFAULT 1,
    audit_kick         INTEGER DEFAULT 1,
    audit_ban          INTEGER DEFAULT 1,
    verify_enabled     INTEGER DEFAULT 0,
    verify_role        INTEGER,
    verify_log_channel INTEGER
);
""")

# announce_enabled カラム追加（既存DBへの対応）
try:
    cursor.execute("ALTER TABLE level_settings ADD COLUMN announce_enabled INTEGER DEFAULT 1")
except:
    pass
try:
    cursor.execute("ALTER TABLE pr_rights ADD COLUMN last_updated TEXT DEFAULT NULL")
except:
    pass

conn.commit()

# =====================
# guild_settings ヘルパー
# =====================
import json as _json

def get_guild_settings(guild_id: int) -> dict:
    """guild_settings を取得。行がなければデフォルト値を返す"""
    cursor.execute("SELECT * FROM guild_settings WHERE guild_id=?", (guild_id,))
    row = cursor.fetchone()
    defaults = {
        "guild_id": guild_id,
        "join_enabled": 0,      "join_channel": None,
        "join_message": "{user} さんが参加しました！",
        "leave_enabled": 0,     "leave_channel": None,
        "leave_message": "{user} さんが退出しました",
        "raid_enabled": 1,      "raid_excluded_roles": "[]",
        "report_channel": None,
        "audit_enabled": 0,     "audit_channel": None,
        "audit_edit": 1,        "audit_delete": 1,
        "audit_timeout": 1,     "audit_kick": 1,    "audit_ban": 1,
        "verify_enabled": 0,    "verify_role": None, "verify_log_channel": None,
    }
    if row:
        defaults.update(dict(row))
    return defaults

def upsert_guild_settings(guild_id: int, **kwargs):
    existing = get_guild_settings(guild_id)
    existing.update(kwargs)
    existing["guild_id"] = guild_id
    cols = ", ".join(existing.keys())
    ph   = ", ".join(["?"] * len(existing))
    ups  = ", ".join(f"{k}=excluded.{k}" for k in existing if k != "guild_id")
    cursor.execute(
        f"INSERT INTO guild_settings ({cols}) VALUES ({ph}) "
        f"ON CONFLICT(guild_id) DO UPDATE SET {ups}",
        list(existing.values())
    )
    conn.commit()

HIVE_API = "https://api.playhive.com/v0"
PATH_ALL = "/game/all/all/{name}"
PATH_MAIN = "/game/all/main/{name}"
PATH_MONTHLY_PLAYER = "/game/monthly/player/{game}/{name}"
PATH_MONTHLY_PLAYER_YM = "/game/monthly/player/{game}/{name}/{year}/{month}"
PATH_META = "/game/meta/{game}"
PATH_LB_ALL = "/game/all/{game}"
PATH_LB_MONTHLY = "/game/monthly/{game}"
PATH_MAPS = "/game/map/{game}"
PATH_GLOBAL = "/global/statistics"
PATH_SEARCH = "/player/search/{query}"

CACHE_SEC = 1800
META_CACHE_SEC = 86400
SEARCH_CACHE_SEC = 300
DB_PATH = "hive_cache.db"
USER_AGENT = "HiveStatsBot/3.0 (Discord bot; discord.py)"

# ---------- フォント（PixelMplus: 日本語も英数字も1つのドットフォント） ----------
# fonts/ に PixelMplus12-Bold.ttf と PixelMplus12-Regular.ttf を置く（配布zipに入っているもの）
FONT_DIR = Path("fonts")
PIXEL_FONT_FILE = FONT_DIR / "PixelMplus12-Bold.ttf"
PIXEL_FONT_REGULAR = FONT_DIR / "PixelMplus12-Regular.ttf"  # Wだけ通常版で補う
# 12pxフォントの整数倍（12 / 24 / 36）だけを使うと、ドットがにじまず綺麗に出る
PIXEL_SIZES = (12, 24, 36)
# PixelMplus が無いときの代替（自動ダウンロード）
FONT_FALLBACK_PATH = FONT_DIR / "DotGothic16-Regular.ttf"
FONT_FALLBACK_URL = "https://raw.githubusercontent.com/google/fonts/main/ofl/dotgothic16/DotGothic16-Regular.ttf"

_blocked: dict[str, float] = {}
_last_use: dict[int, float] = {}
_DEFAULT_MAP: Optional[bytes] = None  # 通知カードなど、マップ画像が手元にないカード用の共通背景
_assets_task: Optional[asyncio.Task] = None


# ---------- プレステージアイコン（コインI〜V / 22x20 ドット絵を埋め込み） ----------
PRESTIGE_ICONS_B64 = [
    "iVBORw0KGgoAAAANSUhEUgAAABYAAAAUCAYAAACJfM0wAAAAkElEQVR42mNkwAT/GcgDjMgcJgYaAUZ0l15+8JiBgYGBQVtOmiSDmJiYUMykmYtZYC5dtuMIikuvPnpKlAEw9f/+/YO5/D/NXYwCcLl0557zDAwMDAzuLoZY1aPHCc1cPGrwqMFEpOPLNx4yMDAwMOhqyKOIo6df9JyHnv7pV7rBygxSQZSHDX1KN8YhV4MAABnkJAcTvOdlAAAAAElFTkSuQmCC",
    "iVBORw0KGgoAAAANSUhEUgAAABYAAAAUCAYAAACJfM0wAAAAfUlEQVR42mNkwAT/GcgDjMgcJgYaAUZ0l/75DLWR+x9JBjExMaGYSTMXs8Bc+mMFqkv/fSXOTrj6f/9gLv9PcxejAHSXvumG0CKl2Pkw9ehxQjMXjxo8ajD2sgIl58ETuDcDaTnvC8SNLHz0Lt3QXU4s4IhgoE/pxjjkahAAxrMixUKbc5MAAAAASUVORK5CYII=",
    "iVBORw0KGgoAAAANSUhEUgAAABYAAAAUCAYAAACJfM0wAAAAo0lEQVR42mOUWfyRARk8ieX/z0AG+P//PyMyn4mBRoAF3aWBjRsYGBgYGNbW+pFkECMj439kl9PMxYwMDAxYXZq15QxRBkzzMUHhMzEx0SmMYQDm0lN7HjMwMDAwmLnI4uVn4XI5rVw8avCowUSk45fnn6GkVxjAxYelX/ScSruy4jGUIYtWZpAK1tcHMDAwMDA8hpQ/dHAxA5rLSQUwl9I8jAGvlTNsGwcRiAAAAABJRU5ErkJggg==",
    "iVBORw0KGgoAAAANSUhEUgAAABYAAAAUCAYAAACJfM0wAAAAsUlEQVR42mOUWfyRARk8ieX/z0AG+P//PyMyn4mBRoAF3aXl/+UYGBgYGFr/rSbJIEZGxv/ILqeZixkZGBj+MzAwMKgfNWFgYGBguGoxlYGBgYGhmimUKAPQfcbCbE6nMIYBmEu7ZooxMDAwMJSlv8LLZ0gPxepymrl41GA65jxCAFfqoLuLMXIeDARYvSIp58HSfyfjI9q7GF6kYnM5seCm9RkUM+niYhSXU2oWzVwMAOiYNPKIp3IvAAAAAElFTkSuQmCC",
    "iVBORw0KGgoAAAANSUhEUgAAABYAAAAUCAYAAACJfM0wAAAAsklEQVR42u2VQQrCMBBF31RrRXDTK7kSL+BeT9AzuPYEuvcG3qobQbSFjpsEmthCFBI3/t2E8Hlk5meEdylfSFWlX2dE0tQnvZUNAEWVt+Z8EgIhItonj0qsAJfyakkfAM9jOw8xKKocgOagDnmSN2aI9Lw4dQC7+z4zNaa29x3y6FPxN06bvEGNTcPPiMVPntWGFZ8kz87zsp7FJ3Z+N588VNt67XgmIR7bIF1go9NskBcWFjVbJTJBGgAAAABJRU5ErkJggg=="
]



# =====================
# PR権限ヘルパー
# =====================
# 順番制のカーソル（メモリ上で管理）
_pr_cursor = 0

def get_pr_text(user_id: int):
    """指定ユーザーのPRテキストを返す"""
    cursor.execute("SELECT promo_text FROM pr_rights WHERE user_id=?", (user_id,))
    row = cursor.fetchone()
    return row[0] if (row and row[0]) else None

def get_next_pr() -> tuple | None:
    """順番制でPRを返す。(user_id, promo_text, display_name候補) or None"""
    global _pr_cursor
    cursor.execute("SELECT user_id, promo_text FROM pr_rights WHERE promo_text IS NOT NULL AND promo_text != '' ORDER BY user_id")
    rows = cursor.fetchall()
    if not rows:
        return None
    _pr_cursor = _pr_cursor % len(rows)
    entry = rows[_pr_cursor]
    _pr_cursor = (_pr_cursor + 1) % len(rows)
    return entry  # (user_id, promo_text)

def has_pr_right(user_id: int) -> bool:
    cursor.execute("SELECT 1 FROM pr_rights WHERE user_id=?", (user_id,))
    return cursor.fetchone() is not None

def grant_pr_right(user_id: int):
    cursor.execute("INSERT OR IGNORE INTO pr_rights (user_id) VALUES (?)", (user_id,))
    conn.commit()

def set_pr_text_db(user_id: int, text: str):
    now_str = datetime.datetime.now(JST).isoformat()
    cursor.execute("UPDATE pr_rights SET promo_text=?, last_updated=? WHERE user_id=?", (text, now_str, user_id))
    conn.commit()

def can_update_pr(user_id: int) -> tuple:
    """(更新可能か, 残り時間の文字列)を返す"""
    cursor.execute("SELECT last_updated FROM pr_rights WHERE user_id=?", (user_id,))
    row = cursor.fetchone()
    if not row or not row[0]:
        return True, ""
    last = datetime.datetime.fromisoformat(row[0])
    if last.tzinfo is None:
        last = last.replace(tzinfo=JST)
    now = datetime.datetime.now(JST)
    diff = now - last
    if diff.total_seconds() >= 7 * 24 * 3600:
        return True, ""
    remaining = datetime.timedelta(seconds=7 * 24 * 3600) - diff
    days = remaining.days
    hours, rem = divmod(remaining.seconds, 3600)
    minutes = rem // 60
    return False, f"{days}日{hours}時間{minutes}分"

def get_pr_display_name(pr_uid: int) -> str:
    pr_user = bot.get_user(pr_uid)
    return pr_user.display_name if pr_user else f"ID:{pr_uid}"

def add_pr_field(embed: discord.Embed) -> discord.Embed:
    """順番制のPR(宣伝文)をEmbedにフィールドとして追加する共通ヘルパー"""
    pr_entry = get_next_pr()
    if pr_entry:
        pr_uid, pr_text = pr_entry
        pr_name = get_pr_display_name(pr_uid)
        embed.add_field(
            name="📣 PR",
            value=f"{pr_text}\n-# pr者： {pr_name}　⚠️ こちらのリンクは危険な可能性もあります",
            inline=False
        )
    return embed

def get_pr_text_line() -> str | None:
    """埋め込みを使わないプレーンテキスト通知用のPR行を返す（なければNone）"""
    pr_entry = get_next_pr()
    if not pr_entry:
        return None
    pr_uid, pr_text = pr_entry
    pr_name = get_pr_display_name(pr_uid)
    return f"📣 PR: {pr_text}\n-# pr者： {pr_name}　⚠️ こちらのリンクは危険な可能性もあります"

# =====================
# カウントゲームユーティリティ
# =====================
def next_valid_number(n: int, x_num: str) -> int:
    while x_num in str(n):
        n += 1
    return n

async def safe_fetch_user(uid: int) -> str:
    """キャッシュ優先でユーザー名を取得。失敗時はID表示"""
    u = bot.get_user(uid)
    if u:
        return u.display_name
    try:
        u = await asyncio.wait_for(bot.fetch_user(uid), timeout=3.0)
        return u.display_name
    except Exception:
        return f"ID:{uid}"


# =====================
# JSON ユーティリティ
# =====================
def load_json(filename):
    if not os.path.exists(filename):
        with open(filename, "w") as f:
            json.dump({}, f)
    with open(filename, "r") as f:
        return json.load(f)

def save_json(filename, data):
    with open(filename, "w") as f:
        json.dump(data, f, indent=4, ensure_ascii=False)

count_settings = load_json("count_settings.json")

# =====================
# Greeting 設定
# =====================
GREETING_FILE = "greeting_settings.json"

def load_greeting_settings():
    if not os.path.exists(GREETING_FILE):
        return {}
    with open(GREETING_FILE, "r", encoding="utf-8") as f:
        return json.load(f)

def save_greeting_settings(data):
    with open(GREETING_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)

greeting_settings = load_greeting_settings()

# =====================
# PIN 設定
# =====================
PIN_FILE = "pins.json"

def load_pins():
    if not os.path.exists(PIN_FILE):
        return {}
    with open(PIN_FILE, "r", encoding="utf-8") as f:
        return json.load(f)

def save_pins(data):
    with open(PIN_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)

pins = load_pins()

# =====================
# 基本設定
# =====================
intents = discord.Intents.default()
intents.message_content = True
intents.members = True

bot = commands.Bot(command_prefix="!", intents=intents)
tree = bot.tree

# ★ OWNER_ID は一箇所だけで定義
OWNER_ID = [1297821384076165180]
AUTHORIZED_USERS = [1297821384076165180, 1408098614961766443]

# ぴーBOT 公式サーバーID（全サーバーのログをここにも送る）
OFFICIAL_GUILD_ID = 1297821384076165180  # ← 公式サーバーのIDに変更してください

_hive_setup_done = False
# =====================
# 正規表現（挨拶）
# =====================
PATTERNS = {
    re.compile("おはよう"): "おはよう",
    re.compile("こんにちは"): "こんにちは",
    re.compile("こんばんは"): "こんばんは",
    re.compile("はろー"): "はろー",
    re.compile("hello", re.I): "Hello",
    re.compile(r"\bhi\b", re.I): "Hi",
    re.compile("こんにちわ"): "こんにちわ",
    re.compile("こーんにーちはー"): "こーんにーちはー",
    re.compile("こーんにーちわー"): "こーんにーちわー",
    re.compile("あけおめ"): "あけおめ",
    re.compile("あけましておめでとうございます"): "あけましておめでとうございます",
    re.compile("よいお年を"): "よいお年を",
    re.compile("こんちゃ"): "こんちゃ",
    re.compile("おやすみ"): "おやすみ",
}

TOPICS = [
    "最近ハマっていることは？",
    "今一番欲しいものは何？",
    "好きなゲームは？",
    "マイクラで一番好きなアイテムは？",
    "朝型？夜型？",
    "今までで一番楽しかった出来事は？",
    "行ってみたい国は？",
    "好きな食べ物は？",
    "子供の頃の夢は？",
    "無人島に1つ持っていくなら？",
    "一番時間を溶かしたゲームは？",
    "1日だけ透明になれたら何する？",
    "マイクラで一番好きなブロックは？",
    "100万円もらったらまず何買う？",
    "タイムマシンあったら過去と未来どっち行く？",
]

REPORT_CHANNEL_NAME = "ぴーbot通報ログ"

STATUS_MESSAGES = [
    lambda: f"{len(bot.guilds)} サーバーに参加中",
    lambda: f"{sum(g.member_count for g in bot.guilds)} users",
    lambda: "荒らし・詐欺行為を検知します!",
    lambda: "公式サイトでコマンドを確認!",
]

# =====================
# Webhook ログ送信
# =====================
async def _post_webhook(url: str, payload: dict):
    """実際のHTTPリクエストを送る（タスクとして実行）"""
    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(url, json=payload) as resp:
                if resp.status not in (200, 204):
                    print(f"Webhook送信失敗: {resp.status}")
    except Exception as e:
        print(f"Webhook送信エラー: {e}")

async def send_webhook_log(guild_id: int, embed: discord.Embed):
    """指定サーバーと公式サーバー両方のWebhookにログを送信する（非ブロッキング）"""
    payload = {"embeds": [embed.to_dict()]}
    sent_urls = set()

    # 送信先IDのリスト（実行サーバー + 公式サーバー、重複除去）
    target_guild_ids = list({guild_id, OFFICIAL_GUILD_ID})

    for gid in target_guild_ids:
        cursor.execute("SELECT webhook_url FROM webhook_log WHERE guild_id=?", (gid,))
        row = cursor.fetchone()
        if not row:
            continue
        url = row[0]
        if url in sent_urls:
            continue  # 同じURLに二重送信しない
        sent_urls.add(url)
        asyncio.create_task(_post_webhook(url, payload))

def make_action_embed(
    action: str,
    executor: discord.Member | discord.User,
    color: discord.Color = discord.Color.blurple(),
    **fields
) -> discord.Embed:
    """コマンド実行ログ用のEmbedを作る"""
    embed = discord.Embed(
        title=f"📋 {action}",
        color=color,
        timestamp=datetime.datetime.now(JST)
    )
    embed.set_author(
        name=f"{executor.display_name} ({executor.id})",
        icon_url=executor.display_avatar.url
    )
    for name, value in fields.items():
        embed.add_field(name=name, value=str(value), inline=False)
    return embed

# =====================
# Webhook 設定コマンド
# =====================
@tree.command(name="set_webhook", description="【管理者専用】コマンドログ用WebhookのURLを登録")
@app_commands.describe(url="DiscordのWebhook URL")
async def set_webhook(interaction: discord.Interaction, url: str):
    if not interaction.guild_permissions.manage_guild:
        await interaction.response.send_message("❌ サーバー管理者のみ設定できます", ephemeral=True)
        return
    if not url.startswith("https://discord.com/api/webhooks/"):
        await interaction.response.send_message("❌ 無効なWebhook URLです", ephemeral=True)
        return
    cursor.execute("""
        INSERT INTO webhook_log (guild_id, webhook_url) VALUES (?, ?)
        ON CONFLICT(guild_id) DO UPDATE SET webhook_url=?
    """, (interaction.guild.id, url, url))
    conn.commit()
    await interaction.response.send_message("<:check:1553534601962594355> Webhook URLを登録しました。これからコマンド実行がログに送られます。", ephemeral=True)

@tree.command(name="remove_webhook", description="【管理者専用】Webhookログを解除します")
async def remove_webhook(interaction: discord.Interaction):
    if not interaction.guild_permissions.manage_guild:
        await interaction.response.send_message("❌ サーバー管理者のみ設定できます", ephemeral=True)
        return
    cursor.execute("DELETE FROM webhook_log WHERE guild_id=?", (interaction.guild.id,))
    conn.commit()
    await interaction.response.send_message("<:check:1553534601962594355> Webhookログを解除しました", ephemeral=True)

# =====================
# グローバルログ (全サーバー共通・BOT管理者専用)
# =====================
async def send_global_log(embed: discord.Embed):
    """全サーバーのイベントをBOT管理者のマスターwebhookに送る"""
    cursor.execute("SELECT webhook_url FROM global_webhook WHERE id=1")
    row = cursor.fetchone()
    if not row:
        return
    asyncio.create_task(_post_webhook(row[0], {"embeds": [embed.to_dict()]}))

@tree.command(name="set_global_webhook", description="【BOT管理者専用】全サーバーログ用グローバルWebhookを設定")
@app_commands.describe(url="DiscordのWebhook URL")
async def set_global_webhook(interaction: discord.Interaction, url: str):
    if interaction.user.id not in OWNER_ID:
        await interaction.response.send_message("❌ BOT管理者専用です", ephemeral=True)
        return
    if not url.startswith("https://discord.com/api/webhooks/"):
        await interaction.response.send_message("❌ 無効なWebhook URLです", ephemeral=True)
        return
    cursor.execute("INSERT INTO global_webhook (id, webhook_url) VALUES (1, ?) ON CONFLICT(id) DO UPDATE SET webhook_url=?", (url, url))
    conn.commit()
    await interaction.response.send_message("<:check:1553534601962594355> グローバルWebhookを設定しました。全サーバーのカウントゲームログがここに届きます", ephemeral=True)

@tree.command(name="remove_global_webhook", description="【BOT管理者専用】グローバルWebhookを解除")
async def remove_global_webhook(interaction: discord.Interaction):
    if interaction.user.id not in OWNER_ID:
        await interaction.response.send_message("❌ BOT管理者専用です", ephemeral=True)
        return
    cursor.execute("DELETE FROM global_webhook WHERE id=1")
    conn.commit()
    await interaction.response.send_message("<:check:1553534601962594355> グローバルWebhookを解除しました", ephemeral=True)

# =====================
# グローバルコイン (全サーバー共通)
# =====================
def get_global_coins(user_id: int) -> int:
    cursor.execute("SELECT coins FROM global_coins WHERE user_id=?", (user_id,))
    row = cursor.fetchone()
    return row[0] if row else 0

def add_global_coins(user_id: int, amount: int):
    cursor.execute("""
        INSERT INTO global_coins (user_id, coins) VALUES (?, ?)
        ON CONFLICT(user_id) DO UPDATE SET coins = coins + ?
    """, (user_id, amount, amount))
    conn.commit()

def set_global_coins(user_id: int, amount: int):
    cursor.execute("""
        INSERT INTO global_coins (user_id, coins) VALUES (?, ?)
        ON CONFLICT(user_id) DO UPDATE SET coins = ?
    """, (user_id, amount, amount))
    conn.commit()

def reset_global_coins(user_id: int):
    set_global_coins(user_id, 0)

# =====================
# /coin /gift /work (グローバル共通)
# =====================
@tree.command(name="coin", description="自分のコイン枚数を表示します")
async def coin(interaction: discord.Interaction):
    coins = get_global_coins(interaction.user.id)
    await interaction.response.send_message(
        f"💰 {interaction.user.mention} さんのコイン枚数: **{coins}枚**",
        ephemeral=True
    )

WORK_COOLDOWN_MIN = 60

@tree.command(name="work", description="/work でコインを稼ぎます（CD1h）")
async def work(interaction: discord.Interaction):
    await interaction.response.defer()
    user_id = interaction.user.id
    now = datetime.datetime.now(JST)

    cursor.execute(
        "SELECT id, target_time FROM reminders WHERE user_id=? AND notification_type='work'",
        (user_id,)
    )
    row = cursor.fetchone()

    if row:
        target_time = datetime.datetime.fromisoformat(row[1])
        if target_time.tzinfo is None:
            target_time = target_time.replace(tzinfo=JST)
        if now < target_time:
            remaining = target_time - now
            minutes, seconds = divmod(int(remaining.total_seconds()), 60)
            embed = discord.Embed(
                description=f"❌ まだクールタイム中: **{minutes}分{seconds}秒後** に再使用可能",
                color=discord.Color.red()
            )
            await interaction.followup.send(embed=embed, ephemeral=True)
            return
        cursor.execute("DELETE FROM reminders WHERE id=?", (row[0],))
        conn.commit()

    reward = random.randint(4000, 5000)
    add_global_coins(user_id, reward)
    total = get_global_coins(user_id)

    next_time = now + datetime.timedelta(minutes=WORK_COOLDOWN_MIN)
    cursor.execute(
        "INSERT INTO reminders (user_id, channel_id, target_time, notification_type) VALUES (?, ?, ?, ?)",
        (user_id, interaction.channel_id, next_time.isoformat(), "work")
    )
    conn.commit()

    embed = discord.Embed(
        title="💼 work完了",
        color=discord.Color.green()
    )
    embed.set_author(name=interaction.user.display_name, icon_url=interaction.user.display_avatar.url)
    embed.add_field(name="獲得コイン", value=f"**+{reward}コイン** 💰", inline=True)
    embed.add_field(name="所持コイン", value=f"**{total}コイン**", inline=True)
    embed.add_field(name="次回使用可能", value=f"{next_time.strftime('%H:%M')}", inline=True)
    add_pr_field(embed)
    await interaction.followup.send(embed=embed)

    async def _log():
        log_embed = make_action_embed(
            "💼 /work 実行",
            interaction.user,
            discord.Color.green(),
            獲得コイン=f"{reward}コイン",
            次回使用可能=f"{next_time.strftime('%H:%M')}",
            サーバー=interaction.guild.name if interaction.guild else "DM"
        )
        if interaction.guild:
            await send_webhook_log(interaction.guild.id, log_embed)
    asyncio.create_task(_log())

@tree.command(name="gift", description="コインをプレゼントします（全サーバー共通）")
@app_commands.describe(user="送る相手", amount="送るコイン枚数")
async def gift(interaction: discord.Interaction, user: discord.Member, amount: int):
    if user.id == interaction.user.id:
        await interaction.response.send_message("❌ 自分には送れません", ephemeral=True)
        return
    if user.bot:
        await interaction.response.send_message("❌ BOTには送れません", ephemeral=True)
        return
    if amount <= 0:
        await interaction.response.send_message("❌ 1以上のコインを指定してください", ephemeral=True)
        return

    sender_coins = get_global_coins(interaction.user.id)
    if sender_coins < amount:
        await interaction.response.send_message(
            f"❌ コインが足りません（現在 **{sender_coins}枚**）",
            ephemeral=True
        )
        return

    add_global_coins(interaction.user.id, -amount)
    add_global_coins(user.id, amount)

    # Webhook ログ
    log_embed = make_action_embed(
        "🎁 /gift 実行",
        interaction.user,
        discord.Color.gold(),
        送り先=f"{user.display_name} ({user.id})",
        送ったコイン=f"{amount}コイン",
        サーバー=interaction.guild.name if interaction.guild else "DM"
    )
    if interaction.guild:
        await send_webhook_log(interaction.guild.id, log_embed)

    await interaction.response.send_message(
        f"🎁 {interaction.user.mention} → {user.mention} に **{amount}コイン** 送りました！",
    )

# =====================
# coin_settings (管理者 Modal 対応)
# =====================
class CoinAddModal(discord.ui.Modal, title="コイン付与"):
    user_id_input = discord.ui.TextInput(label="ユーザーID", placeholder="123456789012345678")
    amount_input = discord.ui.TextInput(label="付与枚数", placeholder="100")

    async def on_submit(self, interaction: discord.Interaction):
        try:
            uid = int(self.user_id_input.value)
            amount = int(self.amount_input.value)
        except ValueError:
            await interaction.response.send_message("❌ 数値を正しく入力してください", ephemeral=True)
            return
        add_global_coins(uid, amount)
        log_embed = make_action_embed(
            "💰 コイン付与（管理者）",
            interaction.user,
            discord.Color.green(),
            対象ユーザーID=uid,
            付与枚数=f"{amount}コイン"
        )
        if interaction.guild:
            await send_webhook_log(interaction.guild.id, log_embed)
        await interaction.response.send_message(f"<:check:1553534601962594355> ユーザー {uid} に {amount}コイン付与しました", ephemeral=True)

class CoinResetModal(discord.ui.Modal, title="コインリセット"):
    user_id_input = discord.ui.TextInput(label="ユーザーID", placeholder="123456789012345678")

    async def on_submit(self, interaction: discord.Interaction):
        try:
            uid = int(self.user_id_input.value)
        except ValueError:
            await interaction.response.send_message("❌ 数値を正しく入力してください", ephemeral=True)
            return
        reset_global_coins(uid)
        log_embed = make_action_embed(
            "🔄 コインリセット（管理者）",
            interaction.user,
            discord.Color.red(),
            対象ユーザーID=uid
        )
        if interaction.guild:
            await send_webhook_log(interaction.guild.id, log_embed)
        await interaction.response.send_message(f"<:check:1553534601962594355> ユーザー {uid} のコインをリセットしました", ephemeral=True)

class CoinCheckModal(discord.ui.Modal, title="コイン確認"):
    user_id_input = discord.ui.TextInput(label="ユーザーID", placeholder="123456789012345678")

    async def on_submit(self, interaction: discord.Interaction):
        try:
            uid = int(self.user_id_input.value)
        except ValueError:
            await interaction.response.send_message("❌ 数値を正しく入力してください", ephemeral=True)
            return
        coins = get_global_coins(uid)
        await interaction.response.send_message(f"💰 ユーザー {uid} のコイン: **{coins}枚**", ephemeral=True)

class CoinSettings(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="💰 コイン付与", style=discord.ButtonStyle.green, custom_id="coin_add")
    async def add_coin_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id not in OWNER_ID:
            await interaction.response.send_message("❌ 管理者専用", ephemeral=True)
            return
        await interaction.response.send_modal(CoinAddModal())

    @discord.ui.button(label="🔄 コインリセット", style=discord.ButtonStyle.red, custom_id="coin_reset")
    async def reset_coin_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id not in OWNER_ID:
            await interaction.response.send_message("❌ 管理者専用", ephemeral=True)
            return
        await interaction.response.send_modal(CoinResetModal())

    @discord.ui.button(label="📊 コイン確認", style=discord.ButtonStyle.blurple, custom_id="coin_check")
    async def check_coin_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id not in OWNER_ID:
            await interaction.response.send_message("❌ 管理者専用", ephemeral=True)
            return
        await interaction.response.send_modal(CoinCheckModal())

@tree.command(name="coin_settings", description="管理者用コイン設定パネル")
async def coin_settings(interaction: discord.Interaction):
    if interaction.user.id not in OWNER_ID:
        await interaction.response.send_message("❌ 管理者専用です", ephemeral=True)
        return
    embed = discord.Embed(title="💰 コイン設定パネル", description="ボタンで操作できます", color=discord.Color.gold())
    await interaction.response.send_message(embed=embed, view=CoinSettings(), ephemeral=True)

# =====================
# レベル計算
# =====================
def get_level(xp: int) -> int:
    return int((xp / 100) ** 0.5)

def xp_for_level(level: int) -> int:
    return level * level * 100

async def add_xp(member: discord.Member, guild: discord.Guild, amount: int,
                  bypass_cooldown: bool = False, update_last: bool = True):
    user_id = member.id
    guild_id = guild.id
    now = datetime.datetime.now(datetime.timezone.utc).timestamp()

    cursor.execute("""
        SELECT xp, level, last_message FROM user_data
        WHERE user_id=? AND guild_id=?
    """, (user_id, guild_id))
    row = cursor.fetchone()

    if row:
        xp, level, last = row
    else:
        xp, level, last = 0, 0, 0.0

    # メッセージXPのみ10秒クールタイムを適用（VCからの呼び出しはbypass_cooldown=Trueで無視）
    if not bypass_cooldown and now - last < 10:
        return

    xp += amount
    new_level = get_level(xp)
    # VCからの呼び出しはlast_messageを更新しない（メッセージのクールタイム判定に影響させないため）
    new_last = now if update_last else last

    cursor.execute("""
        INSERT INTO user_data (user_id, guild_id, xp, level, last_message)
        VALUES (?, ?, ?, ?, ?)
        ON CONFLICT(user_id, guild_id)
        DO UPDATE SET xp=?, level=?, last_message=?
    """, (user_id, guild_id, xp, new_level, new_last, xp, new_level, new_last))
    conn.commit()

    if new_level > level:
        await level_up(member, guild, new_level, level)
        add_global_coins(member.id, 100)  # レベルアップ報酬もグローバルコイン

async def level_up(member: discord.Member, guild: discord.Guild, new_level: int, old_level: int):
    # ロール付与
    cursor.execute("""
        SELECT role_id FROM level_roles WHERE guild_id=? AND level=?
    """, (guild.id, new_level))
    row = cursor.fetchone()
    if row:
        role = guild.get_role(row[0])
        if role:
            try:
                await member.add_roles(role)
            except Exception as e:
                print(f"ロール付与失敗: {e}")

    # 設定取得
    cursor.execute("""
        SELECT announce_channel, log_channel, announce_enabled
        FROM level_settings WHERE guild_id=?
    """, (guild.id,))
    setting = cursor.fetchone()

    announce_ch = None
    log_ch = None
    announce_enabled = True

    if setting:
        announce_ch = guild.get_channel(setting[0]) if setting[0] else None
        log_ch = guild.get_channel(setting[1]) if setting[1] else None
        announce_enabled = bool(setting[2])

    if announce_enabled and announce_ch:
        embed = discord.Embed(
            title="🎉 レベルアップ！",
            description=f"{member.mention} さんが **Lv {new_level}** になりました！",
            color=discord.Color.gold()
        )
        add_pr_field(embed)
        await announce_ch.send(embed=embed)

    if log_ch:
        embed = discord.Embed(
            title="📜 レベルアップログ",
            description=f"{member.mention}\nLv {old_level} → **Lv {new_level}**",
            color=discord.Color.green()
        )
        await log_ch.send(embed=embed)

# =====================
# /level /rank
# =====================
@tree.command(name="level", description="自分のレベルを表示")
async def level_cmd(interaction: discord.Interaction):
    await interaction.response.defer()
    uid = interaction.user.id
    gid = interaction.guild.id

    cursor.execute("""
        SELECT xp, level FROM user_data WHERE user_id=? AND guild_id=?
    """, (uid, gid))
    row = cursor.fetchone()

    xp = row[0] if row else 0
    lvl = row[1] if row else 0
    next_xp = xp_for_level(lvl + 1)
    progress = xp - xp_for_level(lvl)
    needed = next_xp - xp_for_level(lvl)

    bar_filled = int((progress / needed) * 10) if needed > 0 else 10
    bar = "█" * bar_filled + "░" * (10 - bar_filled)

    embed = discord.Embed(
        title="📊 レベル情報",
        color=discord.Color.blue()
    )
    embed.set_author(name=interaction.user.display_name, icon_url=interaction.user.display_avatar.url)
    embed.add_field(name="レベル", value=f"Lv. **{lvl}**", inline=True)
    embed.add_field(name="XP", value=f"{xp} XP", inline=True)
    embed.add_field(name=f"次のレベルまで", value=f"`{bar}` {progress}/{needed}", inline=False)
    add_pr_field(embed)

    await interaction.followup.send(embed=embed)

@tree.command(name="rank", description="サーバー内レベルランキング")
async def rank(interaction: discord.Interaction):
    await interaction.response.defer()
    cursor.execute("""
        SELECT user_id, level, xp FROM user_data
        WHERE guild_id=? ORDER BY xp DESC LIMIT 10
    """, (interaction.guild.id,))
    rows = cursor.fetchall()

    if not rows:
        await interaction.followup.send("まだランキングデータがありません")
        return

    medals = ["🥇", "🥈", "🥉"]
    desc = ""
    for i, (uid, lvl, xp) in enumerate(rows, start=1):
        name = await safe_fetch_user(uid)
        medal = medals[i-1] if i <= 3 else f"{i}."
        desc += f"{medal} **{name}** - Lv.{lvl} ({xp} XP)\n"

    embed = discord.Embed(
        title="🏆 レベルランキング",
        description=desc,
        color=discord.Color.gold()
    )
    add_pr_field(embed)
    await interaction.followup.send(embed=embed)

# =====================
# /level_settings パネル
# =====================
class SetAnnounceChannelModal(discord.ui.Modal, title="通知チャンネル設定"):
    channel_id_input = discord.ui.TextInput(label="チャンネルID", placeholder="123456789012345678")

    async def on_submit(self, interaction: discord.Interaction):
        try:
            ch_id = int(self.channel_id_input.value)
        except ValueError:
            await interaction.response.send_message("❌ 数値を正しく入力してください", ephemeral=True)
            return
        gid = interaction.guild.id
        cursor.execute("""
            INSERT INTO level_settings (guild_id, announce_channel)
            VALUES (?, ?)
            ON CONFLICT(guild_id) DO UPDATE SET announce_channel=?
        """, (gid, ch_id, ch_id))
        conn.commit()
        await interaction.response.send_message(f"<:check:1553534601962594355> 通知チャンネルを <#{ch_id}> に設定しました", ephemeral=True)

class LevelPanel(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="📢 通知ON/OFF", style=discord.ButtonStyle.green, custom_id="level_toggle")
    async def toggle(self, interaction: discord.Interaction, button: discord.ui.Button):
        gid = interaction.guild.id
        cursor.execute("SELECT announce_enabled FROM level_settings WHERE guild_id=?", (gid,))
        row = cursor.fetchone()
        enabled = 0 if (row and row[0]) else 1
        cursor.execute("""
            INSERT INTO level_settings (guild_id, announce_enabled) VALUES (?, ?)
            ON CONFLICT(guild_id) DO UPDATE SET announce_enabled=?
        """, (gid, enabled, enabled))
        conn.commit()
        state = "ON" if enabled else "OFF"
        await interaction.response.send_message(f"<:check:1553534601962594355> レベルアップ通知を **{state}** にしました", ephemeral=True)

    @discord.ui.button(label="📌 通知チャンネル設定", style=discord.ButtonStyle.blurple, custom_id="level_set_channel")
    async def set_channel(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(SetAnnounceChannelModal())

@tree.command(name="level_settings", description="レベル設定パネル（管理者専用）")
@app_commands.checks.has_permissions(manage_guild=True)
async def level_settings(interaction: discord.Interaction):
    embed = discord.Embed(
        title="⚙ レベル設定",
        description="ボタンで設定できます",
        color=discord.Color.orange()
    )
    await interaction.response.send_message(embed=embed, view=LevelPanel(), ephemeral=True)

@level_settings.error
async def level_settings_error(interaction: discord.Interaction, error: app_commands.AppCommandError):
    if isinstance(error, app_commands.MissingPermissions):
        await interaction.response.send_message("❌ 管理者のみ使用できます", ephemeral=True)

@tree.command(name="level_setting", description="【BOT管理者専用】ユーザーのレベルを変更")
@app_commands.describe(user="対象ユーザー", level="設定するレベル")
async def level_setting(interaction: discord.Interaction, user: discord.Member, level: int):
    if interaction.user.id not in OWNER_ID:
        await interaction.response.send_message("❌ 権限がありません", ephemeral=True)
        return
    xp = xp_for_level(level)
    gid = interaction.guild.id
    cursor.execute("""
        INSERT INTO user_data (user_id, guild_id, xp, level)
        VALUES (?, ?, ?, ?)
        ON CONFLICT(user_id, guild_id) DO UPDATE SET xp=?, level=?
    """, (user.id, gid, xp, level, xp, level))
    conn.commit()

    log_embed = make_action_embed(
        "⚙ レベル変更（BOT管理者）",
        interaction.user,
        discord.Color.orange(),
        対象=f"{user.display_name} ({user.id})",
        変更後レベル=f"Lv {level}"
    )
    await send_webhook_log(interaction.guild.id, log_embed)
    await interaction.response.send_message(f"<:check:1553534601962594355> {user.display_name} を Lv {level} にしました")

# =====================
# 荒らし検知
# =====================
OZEU_PATTERN = re.compile(r"おぜ+うLOL+")

def load_raid_texts():
    cursor.execute("SELECT text FROM raid_texts")
    return [row[0] for row in cursor.fetchall()]

def is_ozeu(content: str) -> bool:
    if OZEU_PATTERN.search(content):
        return True
    for text in load_raid_texts():
        if text in content:
            return True
    return False

@tree.command(name="add_raid_text", description="【bot管理者専用】荒らし文を学習データに追加")
@app_commands.describe(text="追加する荒らし文")
async def add_raid_text(interaction: discord.Interaction, text: str):
    if interaction.user.id not in OWNER_ID:
        await interaction.response.send_message("❌ このコマンドはBot管理者専用です。", ephemeral=True)
        return
    try:
        cursor.execute("INSERT INTO raid_texts (text) VALUES (?)", (text,))
        conn.commit()
        await interaction.response.send_message(f"<:check:1553534601962594355> 荒らし文を追加しました：\n```{text}```", ephemeral=True)
    except sqlite3.IntegrityError:
        await interaction.response.send_message("⚠ その文はすでに登録されています。", ephemeral=True)

# =====================
# on_ready
# =====================
_hive_setup_done = False

@bot.event
async def on_ready():
    global _hive_setup_done

    print(f"BOT起動完了: {bot.user}")

    # Hiveコマンドの初期化（初回のみ）
    if not _hive_setup_done:
        try:
            setup_hive(bot)
            _hive_setup_done = True
            print("Hiveコマンド登録完了")
        except Exception as e:
            print("Hive初期化失敗:", repr(e))

    # スラッシュコマンド同期
    try:
        synced = await bot.tree.sync()
        print(f"スラッシュコマンド同期完了: {len(synced)}個")
    except Exception as e:
        print("同期失敗:", e)

    # 起動通知
    notify_channel_name = "bot・ぴーbot稼働通知"
    for ch in bot.get_all_channels():
        if ch.name == notify_channel_name:
            try:
                await ch.send("<@&1431935607562571938> ぴーbotが起動しました")
            except:
                pass

    # Bot再起動後もボタンが動くようにViewを永続登録
    bot.add_view(CoinSettings())
    bot.add_view(LevelPanel())
    bot.add_view(HelpPanel())
    bot.add_view(VerifyButton())

    if not reminder_loop.is_running():
        reminder_loop.start()
    if not pin_loop.is_running():
        pin_loop.start()
    if not status_loop.is_running():
        status_loop.start()
    if not vc_xp_loop.is_running():
        vc_xp_loop.start()

# =====================
# PIN常駐（修正版）
# =====================
@tasks.loop(seconds=10)
async def pin_loop():
    global pins
    changed = False

    for cid, data in list(pins.items()):
        channel = bot.get_channel(int(cid))
        if not channel:
            continue

        msg_id = data.get("message_id")
        content_now = (data.get("content") or "").strip()

        if not content_now:
            continue

        # 既存PINメッセージを確認
        if msg_id:
            try:
                msg = await channel.fetch_message(int(msg_id))

                # Embedのdescriptionと比較
                if msg.embeds and msg.embeds[0].description:
                    if msg.embeds[0].description.strip() == content_now:
                        # 最新メッセージかチェック
                        async for last_msg in channel.history(limit=1):
                            if last_msg.id == msg.id:
                                break  # 既に最新 → 何もしない
                            else:
                                # 最新じゃなければ削除して再送
                                try:
                                    await msg.delete()
                                except:
                                    pass
                                pins[cid]["message_id"] = None
                                changed = True
                        continue

                # 内容が違う → 削除
                try:
                    await msg.delete()
                except:
                    pass
                pins[cid]["message_id"] = None
                changed = True

            except discord.NotFound:
                pins[cid]["message_id"] = None
                changed = True
            except Exception as e:
                print(f"PIN取得エラー: {e}")
                continue

        # 新規送信
        if pins[cid].get("message_id") is None:
            try:
                embed = discord.Embed(
                    title="📌 PIN",
                    description=content_now,
                    color=discord.Color.green()
                )
                embed.set_footer(text=f'by {data.get("author", "unknown")}')
                new_msg = await channel.send(embed=embed)
                pins[cid]["message_id"] = new_msg.id
                changed = True
            except Exception as e:
                print(f"PIN送信エラー: {e}")

    if changed:
        save_pins(pins)

# =====================
# on_message
# =====================
@bot.event
async def on_message(message):
    # =====================
    # takasumibot /work 通知検知
    # =====================
    if message.author.bot and message.embeds:
        try:
            await detect_takasumibot_work(message)
        except Exception as e:
            print(f"[takasumibot検知エラー] {e}")
        return

    if message.author.bot:
        return

    content = message.content or ""

    is_staff = (
        message.author.guild_permissions.administrator or
        message.author.guild_permissions.manage_guild or
        message.author.guild_permissions.manage_messages
    ) if message.guild else False

    # 詐欺検知
    file_count = len(message.attachments) + len(message.embeds)
    if "@everyone" in content and file_count >= 4:
        user = message.author
        guild = message.guild
        attachments = "\n".join(a.url for a in message.attachments)

        if is_staff:
            report_channel = discord.utils.get(guild.text_channels, name=REPORT_CHANNEL_NAME)
            if report_channel:
                embed = discord.Embed(
                    title="⚠ 管理者の詐欺パターン検知（処罰なし）",
                    color=discord.Color.orange(),
                    timestamp=discord.utils.utcnow()
                )
                embed.add_field(name="ユーザー", value=f"{user} ({user.id})", inline=False)
                embed.add_field(name="チャンネル", value=message.channel.mention, inline=False)
                embed.add_field(name="内容", value=content or "（テキストなし）", inline=False)
                await report_channel.send(embed=embed)
        else:
            await message.delete()
            warn = await message.channel.send(f"🚨 {user.mention} 詐欺スパムを検知しました")
            await warn.delete(delay=3)
            try:
                until = discord.utils.utcnow() + datetime.timedelta(days=28)
                await user.timeout(until, reason="画像4枚@everyone詐欺検知")
            except Exception as e:
                print("Timeout失敗:", e)

            report_channel = discord.utils.get(guild.text_channels, name=REPORT_CHANNEL_NAME)
            if report_channel:
                embed = discord.Embed(
                    title="🚨 詐欺スパム自動検知",
                    color=discord.Color.red(),
                    timestamp=discord.utils.utcnow()
                )
                embed.add_field(name="ユーザー", value=f"{user} ({user.id})", inline=False)
                embed.add_field(name="チャンネル", value=message.channel.mention, inline=False)
                embed.add_field(name="内容", value=content or "（テキストなし）", inline=False)
                embed.add_field(name="添付ファイル", value=attachments or "なし", inline=False)
                await report_channel.send(embed=embed)
            return

    # 荒らし検知 (guild_settings 対応版)
    if is_ozeu(content) and not is_staff:
        s = get_guild_settings(message.guild.id) if message.guild else {"raid_enabled": 1, "raid_excluded_roles": "[]"}
        if s.get("raid_enabled", 1):
            excluded = _json.loads(s.get("raid_excluded_roles") or "[]")
            role_ids = [str(r.id) for r in message.author.roles]
            if not any(rid in excluded for rid in role_ids):
                await message.delete()
                await message.channel.send(
                    f"⚠ {message.author.mention} 荒らし行為、または詐欺行為を検知しました",
                    delete_after=3
                )
                return

    # =====================
    # 挨拶反応（インデントバグ修正）
    # =====================
    if message.guild:
        guild_id_str = str(message.guild.id)
        guild_greeting_enabled = greeting_settings.get(guild_id_str, True)

        if guild_greeting_enabled:
            for pat, text in PATTERNS.items():
                if pat.search(content):
                    await message.channel.send(
                        f"{message.author.mention} {text}！{message.author.display_name}さん！"
                    )
                    break

    # カウントゲーム
    if message.guild:
        guild_id_str = str(message.guild.id)
        if guild_id_str in count_settings:
            settings = count_settings[guild_id_str]
            if message.channel.id == settings["channel_id"] and message.content.strip().isdigit():
                number = int(message.content.strip())
                x_num = str(settings["x_number"])
                current = settings["current_number"]

                # 禁止数字を含む数字を投稿 → スキップすべきだった
                if x_num in str(number):
                    await message.add_reaction("⚠️")
                    skip_to = next_valid_number(number + 1, x_num)
                    warn = (
                        f"⚠️ {message.author.mention} "
                        f"『**{x_num}**』を含む数字はスキップです！"
                        f" 次は **{skip_to}** を言ってください"
                    )
                    await message.channel.send(warn, delete_after=5)
                    return

                # 数字ミス → カウントゲームのみリセット（コイン残高は変更しない）
                if number != current:
                    await message.add_reaction("❌")
                    new_x = random.randint(1, 9)
                    start = next_valid_number(1, str(new_x))
                    settings["current_number"] = start
                    settings["x_number"] = new_x
                    save_json("count_settings.json", count_settings)
                    reset_msg = (
                        f"💥 **ミス！** {message.author.mention} が間違えました！\n"
                        f"（正解は **{current}** でした）\n\n"
                        f"次のカウント：**{start}** から / 新しい禁止数字：『**{new_x}**』"
                    )
                    embed = discord.Embed(title="💥 ゲームリセット", description=reset_msg, color=discord.Color.red())
                    await message.channel.send(embed=embed)
                    return

                # 成功
                await message.add_reaction("<:check:1553534601962594355>")
                reward = random.randint(50, 150)
                add_global_coins(message.author.id, reward)
                if number > settings.get("record", 0):
                    settings["record"] = number
                nxt = next_valid_number(number + 1, x_num)
                settings["current_number"] = nxt
                save_json("count_settings.json", count_settings)
                return

    # ひろゆきチャンネル反応
    if message.guild:
        cursor.execute("SELECT channel_id FROM hiroyuki_channels WHERE guild_id=?", (message.guild.id,))
        hrow = cursor.fetchone()
        if hrow and message.channel.id == hrow[0]:
            asyncio.create_task(hiroyuki_respond(message))

    # XP付与
    if message.guild:
        await add_xp(message.author, message.guild, random.randint(5, 15))

    await bot.process_commands(message)

# =====================
# 入退出・監査ログ・イベント
# =====================
@bot.event
async def on_member_join(member: discord.Member):
    s = get_guild_settings(member.guild.id)
    if not s["join_enabled"] or not s["join_channel"]:
        return
    ch = member.guild.get_channel(int(s["join_channel"]))
    if not ch:
        return
    msg = (
        (s["join_message"] or "{user} さんが参加しました！")
        .replace("{user}",   member.mention)
        .replace("{name}",   member.display_name)
        .replace("{server}", member.guild.name)
    )
    embed = discord.Embed(description=msg, color=discord.Color.green(),
                          timestamp=datetime.datetime.now(JST))
    embed.set_thumbnail(url=member.display_avatar.url)
    await ch.send(embed=embed)

@bot.event
async def on_member_remove(member: discord.Member):
    guild = member.guild
    s     = get_guild_settings(guild.id)
    now   = datetime.datetime.now(datetime.timezone.utc)
    # kick か退室か判定
    is_kick   = False
    kick_exec = None
    try:
        if guild.me.guild_permissions.view_audit_log:
            async for entry in guild.audit_logs(action=discord.AuditLogAction.kick, limit=5):
                age = (now - entry.created_at.replace(tzinfo=datetime.timezone.utc)).total_seconds()
                if entry.target.id == member.id and age < 10:
                    is_kick   = True
                    kick_exec = entry.user
                    break
    except Exception as e:
        print(f"[audit kick] {e}")
    # kick 監査ログ
    if is_kick and s["audit_enabled"] and s["audit_kick"] and s["audit_channel"]:
        audit_ch = guild.get_channel(int(s["audit_channel"]))
        if audit_ch:
            embed = discord.Embed(title="👢 キック", color=discord.Color.orange(),
                                  timestamp=datetime.datetime.now(JST))
            embed.add_field(name="対象",   value=f"{member} ({member.id})", inline=False)
            embed.add_field(name="実行者", value=str(kick_exec) if kick_exec else "不明", inline=False)
            await audit_ch.send(embed=embed)
    # 退室通知
    if s["leave_enabled"] and s["leave_channel"]:
        ch = guild.get_channel(int(s["leave_channel"]))
        if ch:
            msg = (
                (s["leave_message"] or "{user} さんが退出しました")
                .replace("{user}", f"**{member.display_name}**")
                .replace("{name}", member.display_name)
            )
            embed = discord.Embed(description=msg, color=discord.Color.red(),
                                  timestamp=datetime.datetime.now(JST))
            embed.set_thumbnail(url=member.display_avatar.url)
            await ch.send(embed=embed)

@bot.event
async def on_message_edit(before: discord.Message, after: discord.Message):
    if before.author.bot or before.content == after.content or not before.guild:
        return
    s = get_guild_settings(before.guild.id)
    if not (s["audit_enabled"] and s["audit_edit"] and s["audit_channel"]):
        return
    ch = before.guild.get_channel(int(s["audit_channel"]))
    if not ch:
        return
    embed = discord.Embed(title="✏️ メッセージ編集", color=discord.Color.yellow(),
                          timestamp=datetime.datetime.now(JST))
    embed.set_author(name=str(before.author), icon_url=before.author.display_avatar.url)
    embed.add_field(name="チャンネル", value=before.channel.mention,            inline=False)
    embed.add_field(name="編集前",     value=before.content[:500] or "（空）",  inline=False)
    embed.add_field(name="編集後",     value=after.content[:500]  or "（空）",  inline=False)
    embed.add_field(name="リンク",     value=f"[ジャンプ]({after.jump_url})",   inline=False)
    await ch.send(embed=embed)

@bot.event
async def on_message_delete(message: discord.Message):
    if message.author.bot or not message.guild:
        return
    s = get_guild_settings(message.guild.id)
    if not (s["audit_enabled"] and s["audit_delete"] and s["audit_channel"]):
        return
    ch = message.guild.get_channel(int(s["audit_channel"]))
    if not ch:
        return
    embed = discord.Embed(title="🗑️ メッセージ削除", color=discord.Color.red(),
                          timestamp=datetime.datetime.now(JST))
    embed.set_author(name=str(message.author), icon_url=message.author.display_avatar.url)
    embed.add_field(name="チャンネル", value=message.channel.mention,              inline=False)
    embed.add_field(name="内容",       value=message.content[:500] or "（なし）",  inline=False)
    await ch.send(embed=embed)

@bot.event
async def on_member_ban(guild: discord.Guild, user: discord.User):
    s = get_guild_settings(guild.id)
    if not (s["audit_enabled"] and s["audit_ban"] and s["audit_channel"]):
        return
    ch = guild.get_channel(int(s["audit_channel"]))
    if not ch:
        return
    executor = None
    try:
        if guild.me.guild_permissions.view_audit_log:
            async for entry in guild.audit_logs(action=discord.AuditLogAction.ban, limit=3):
                if entry.target.id == user.id:
                    executor = entry.user
                    break
    except Exception as e:
        print(f"[audit ban] {e}")
    embed = discord.Embed(title="🔨 BAN", color=discord.Color.dark_red(),
                          timestamp=datetime.datetime.now(JST))
    embed.add_field(name="対象",   value=f"{user} ({user.id})", inline=False)
    embed.add_field(name="実行者", value=str(executor) if executor else "不明", inline=False)
    await ch.send(embed=embed)

@bot.event
async def on_member_update(before: discord.Member, after: discord.Member):
    guild = before.guild
    s     = get_guild_settings(guild.id)
    if not (s["audit_enabled"] and s["audit_timeout"] and s["audit_channel"]):
        return
    now     = datetime.datetime.now(datetime.timezone.utc)
    was_out = before.timed_out_until is None
    now_out = (after.timed_out_until is not None and
               after.timed_out_until.replace(tzinfo=datetime.timezone.utc) > now)
    if not (was_out and now_out):
        return
    ch = guild.get_channel(int(s["audit_channel"]))
    if not ch:
        return
    executor = None
    try:
        if guild.me.guild_permissions.view_audit_log:
            async for entry in guild.audit_logs(
                action=discord.AuditLogAction.member_update, limit=3
            ):
                if entry.target.id == after.id:
                    executor = entry.user
                    break
    except Exception as e:
        print(f"[audit timeout] {e}")
    embed = discord.Embed(title="⏰ タイムアウト", color=discord.Color.orange(),
                          timestamp=datetime.datetime.now(JST))
    embed.add_field(name="対象",     value=f"{after} ({after.id})", inline=False)
    embed.add_field(name="解除時刻",
                    value=f"<t:{int(after.timed_out_until.timestamp())}:F>", inline=False)
    embed.add_field(name="実行者",   value=str(executor) if executor else "不明", inline=False)
    await ch.send(embed=embed)

# =====================
# PIN コンテキストメニュー
# =====================
@tree.context_menu(name="📌 PINに設定")
async def context_pin(interaction: discord.Interaction, message: discord.Message):
    if not interaction.permissions.manage_messages:
        await interaction.response.send_message("❌ 権限がありません。", ephemeral=True)
        return

    cid = str(message.channel.id)
    pins[cid] = {
        "message_id": None,
        "content": message.content,
        "author": message.author.display_name
    }
    save_pins(pins)
    # 先にレスポンス、ログは非同期タスクで
    await interaction.response.send_message("<:check:1553534601962594355> このメッセージをPINに設定しました。", ephemeral=True)
    async def _log():
        log_embed = make_action_embed(
            "📌 PIN設定",
            interaction.user,
            discord.Color.green(),
            チャンネル=message.channel.mention,
            内容=message.content[:200] if message.content else "（添付のみ）"
        )
        await send_webhook_log(interaction.guild.id, log_embed)
    asyncio.create_task(_log())

@tree.context_menu(name="❌ PINを解除")
async def context_unpin(interaction: discord.Interaction, message: discord.Message):
    if not interaction.permissions.manage_messages:
        await interaction.response.send_message("❌ 権限がありません。", ephemeral=True)
        return

    cid = str(message.channel.id)
    if cid not in pins:
        await interaction.response.send_message("⚠ このチャンネルにPINはありません。", ephemeral=True)
        return

    data = pins[cid]
    # fetch_message は非同期なので先にレスポンスしてから処理
    await interaction.response.send_message("<:check:1553534601962594355> PINを解除しました。", ephemeral=True)

    async def _delete_and_log():
        if data.get("message_id"):
            try:
                pin_msg = await message.channel.fetch_message(int(data["message_id"]))
                await pin_msg.delete()
            except discord.NotFound:
                pass
        del pins[cid]
        save_pins(pins)
        log_embed = make_action_embed(
            "❌ PIN解除",
            interaction.user,
            discord.Color.red(),
            チャンネル=message.channel.mention
        )
        await send_webhook_log(interaction.guild.id, log_embed)
    asyncio.create_task(_delete_and_log())


# =====================
# コインショップ
# =====================
SHOP_ITEMS = {
    "pr": {
        "name": "📣 PR権限",
        "description": "/topic・/slot・/level の埋め込み下部に宣伝文を1行表示できます",
        "price": 1000,
    },
}

@tree.command(name="shop", description="コインショップを表示します")
async def shop_cmd(interaction: discord.Interaction):
    embed = discord.Embed(
        title="🛒 コインショップ",
        description="コインで購入できるアイテム一覧",
        color=discord.Color.gold()
    )
    for key, item in SHOP_ITEMS.items():
        owned = has_pr_right(interaction.user.id) if key == "pr" else False
        status = "<:check:1553534601962594355> 所持済み" if owned else ("💰 " + str(item["price"]) + "コイン")
        embed.add_field(
            name=item["name"] + " [" + status + "]",
            value=item["description"] + "\n`/buy " + key + "` で購入",
            inline=False
        )
    coins = get_global_coins(interaction.user.id)
    embed.set_footer(text="あなたの所持コイン: " + str(coins) + "枚")
    await interaction.response.send_message(embed=embed, ephemeral=True)

@tree.command(name="buy", description="ショップのアイテムをコインで購入します")
@app_commands.describe(item="購入するアイテムID（例: pr）")
@app_commands.choices(item=[
    app_commands.Choice(name="📣 PR権限 (1000コイン)", value="pr"),
])
async def buy_cmd(interaction: discord.Interaction, item: app_commands.Choice[str]):
    user_id = interaction.user.id
    if item.value == "pr":
        if has_pr_right(user_id):
            await interaction.response.send_message(
                "<:check:1553534601962594355> すでにPR権限を所持しています。`/setpr` で宣伝文を設定できます",
                ephemeral=True
            )
            return
        price = SHOP_ITEMS["pr"]["price"]
        coins = get_global_coins(user_id)
        if coins < price:
            await interaction.response.send_message(
                "❌ コインが足りません（必要: " + str(price) + "枚 / 所持: " + str(coins) + "枚）",
                ephemeral=True
            )
            return
        add_global_coins(user_id, -price)
        grant_pr_right(user_id)
        log_embed = make_action_embed(
            "🛒 PR権限購入",
            interaction.user,
            discord.Color.gold(),
            消費コイン=str(price) + "コイン",
            残高=str(get_global_coins(user_id)) + "コイン"
        )
        if interaction.guild:
            await send_webhook_log(interaction.guild.id, log_embed)
        await interaction.response.send_message(
            "<:check:1553534601962594355> **PR権限**を購入しました！`/setpr [宣伝文]` で宣伝文を設定してください",
            ephemeral=True
        )

@tree.command(name="setpr", description="PR権限がある場合、宣伝文を設定します")
@app_commands.describe(
    text="表示するテキスト（20文字以内）",
    url="クリック時に開くURL（省略可）"
)
async def setpr(interaction: discord.Interaction, text: str, url: str = ""):
    await interaction.response.defer(ephemeral=True)
    user_id = interaction.user.id
    if not has_pr_right(user_id):
        await interaction.followup.send("❌ PR権限がありません。`/shop` で購入できます", ephemeral=True)
        return
    # 週1制限チェック
    ok, remaining = can_update_pr(user_id)
    if not ok:
        await interaction.followup.send(
            f"❌ PRの変更は週1回までです。次の変更まで **{remaining}** 待ってください",
            ephemeral=True
        )
        return
    if len(text) > 20:
        await interaction.followup.send("❌ テキストは20文字以内にしてください", ephemeral=True)
        return
    if url and not (url.startswith("http://") or url.startswith("https://")):
        await interaction.followup.send("❌ URLは http:// または https:// から始めてください", ephemeral=True)
        return
    pr_value = f"[{text}]({url})" if url else text
    set_pr_text_db(user_id, pr_value)
    log_embed = make_action_embed("📣 PR文設定", interaction.user, discord.Color.blurple(), 設定内容=pr_value)
    if interaction.guild:
        await send_webhook_log(interaction.guild.id, log_embed)
    await interaction.followup.send(
        "<:check:1553534601962594355> 宣伝文を設定しました！\n📣 PR: " + pr_value,
        ephemeral=True
    )

@tree.command(name="mypr", description="自分のPR設定を確認します")
async def mypr(interaction: discord.Interaction):
    user_id = interaction.user.id
    if not has_pr_right(user_id):
        await interaction.response.send_message("❌ PR権限がありません", ephemeral=True)
        return
    pr = get_pr_text(user_id)
    embed = discord.Embed(title="📣 あなたのPR設定", color=discord.Color.blurple())
    embed.add_field(name="現在の宣伝文", value=("-# " + pr) if pr else "（未設定）", inline=False)
    embed.add_field(name="変更方法", value="`/setpr [テキスト] [URL]`\nURLは省略可。テキストをクリックするとURLが開きます", inline=False)
    await interaction.response.send_message(embed=embed, ephemeral=True)

# =====================
# カウントゲーム
# =====================
async def send_count_embed(channel, settings):
    embed = discord.Embed(
        title="🎲 数字カウントゲーム",
        description=(
            f"現在の数字：**{settings['current_number']}**\n"
            f"最高記録：**{settings.get('record', 0)}**\n\n"
            f"⚠ 禁止数字：『{settings['x_number']}』を含んだら即終了！\n"

        ),
        color=discord.Color.green()
    )
    await channel.send(embed=embed)

@tree.command(name="countstart", description="カウントゲーム開始（管理者専用）")
@app_commands.describe(channel="ゲームを行うチャンネル")
@app_commands.checks.has_permissions(manage_guild=True)
async def countstart(interaction: discord.Interaction, channel: discord.TextChannel):
    guild_id = str(interaction.guild.id)
    x_number = random.randint(1, 9)
    count_settings[guild_id] = {
        "channel_id": channel.id,
        "current_number": 1,
        "x_number": x_number,
        "record": 0,
    }
    save_json("count_settings.json", count_settings)

    log_embed = make_action_embed(
        "🎲 カウントゲーム開始",
        interaction.user,
        discord.Color.green(),
        サーバー=interaction.guild.name,
        チャンネル=channel.mention
    )
    await send_webhook_log(interaction.guild.id, log_embed)
    await send_global_log(log_embed)

    embed = discord.Embed(
        title="🎮 数字カウントゲーム開始！",
        description=(
            f"現在の数字：1\n最高記録：0\n\n"
            f"💣 禁止数字：『{x_number}』を含んだら即終了！\n"

        ),
        color=discord.Color.green()
    )
    embed.set_footer(text=f"開始者：{interaction.user.display_name}")
    await channel.send(embed=embed)
    await interaction.response.send_message("<:check:1553534601962594355> ゲーム開始！", ephemeral=True)

@countstart.error
async def countstart_error(interaction: discord.Interaction, error: app_commands.AppCommandError):
    if isinstance(error, app_commands.MissingPermissions):
        await interaction.response.send_message("❌ このコマンドはサーバー管理者のみ使用できます", ephemeral=True)

@tree.command(name="countstop", description="カウントゲーム停止（管理者専用）")
@app_commands.checks.has_permissions(manage_guild=True)
async def countstop(interaction: discord.Interaction):
    guild_id = str(interaction.guild.id)
    if guild_id not in count_settings:
        await interaction.response.send_message("ゲームは開始されていません", ephemeral=True)
        return
    del count_settings[guild_id]
    save_json("count_settings.json", count_settings)

    log_embed = make_action_embed("🛑 カウントゲーム停止", interaction.user, discord.Color.red(), サーバー=interaction.guild.name)
    await send_webhook_log(interaction.guild.id, log_embed)
    await send_global_log(log_embed)

    await interaction.response.send_message("🛑 カウントゲームを停止しました")

@countstop.error
async def countstop_error(interaction: discord.Interaction, error: app_commands.AppCommandError):
    if isinstance(error, app_commands.MissingPermissions):
        await interaction.response.send_message("❌ このコマンドはサーバー管理者のみ使用できます", ephemeral=True)

# =====================
# ランキング
# =====================
@tree.command(name="ranking", description="ランキングを表示します")
@app_commands.describe(type="ランキングの種類を選択")
@app_commands.choices(type=[
    app_commands.Choice(name="Coinランキング（全サーバー共通）", value="coin"),
    app_commands.Choice(name="Countランキング", value="count"),
    app_commands.Choice(name="レベルランキング", value="level"),
])
async def ranking(interaction: discord.Interaction, type: app_commands.Choice[str]):
    await interaction.response.defer()
    description = ""
    medals = ["🥇", "🥈", "🥉"]

    if type.value == "coin":
        cursor.execute("""
            SELECT user_id, coins FROM global_coins
            ORDER BY coins DESC LIMIT 10
        """)
        rows = cursor.fetchall()
        for i, (uid, coins) in enumerate(rows, start=1):
            medal = medals[i-1] if i <= 3 else f"{i}."
            name = await safe_fetch_user(uid)
            description += f"{medal} **{name}** - {coins}コイン\n"
        color = discord.Color.gold()

    elif type.value == "count":
        sorted_data = sorted(
            count_settings.items(),
            key=lambda x: x[1].get("record", 0),
            reverse=True
        )[:10]
        for i, (gid, info) in enumerate(sorted_data, start=1):
            medal = medals[i-1] if i <= 3 else f"{i}."
            g = bot.get_guild(int(gid))
            name = g.name if g else "不明"
            description += f"{medal} **{name}** - {info.get('record', 0)}\n"
        color = discord.Color.blue()

    elif type.value == "level":
        cursor.execute("""
            SELECT user_id, level, xp FROM user_data
            WHERE guild_id=? ORDER BY xp DESC LIMIT 10
        """, (interaction.guild.id,))
        rows = cursor.fetchall()
        for i, (uid, lvl, xp) in enumerate(rows, start=1):
            medal = medals[i-1] if i <= 3 else f"{i}."
            name = await safe_fetch_user(uid)
            description += f"{medal} **{name}** - Lv.{lvl} ({xp} XP)\n"
        color = discord.Color.purple()

    embed = discord.Embed(
        title=f"🏆 {type.name} TOP10",
        description=description if description else "データがありません",
        color=color
    )
    add_pr_field(embed)
    await interaction.followup.send(embed=embed)

# =====================
# 通報
# =====================
@tree.context_menu(name="通報[report message]")
async def report_message(interaction: discord.Interaction, message: discord.Message):
    if message.author.id == interaction.user.id:
        await interaction.response.send_message("❌ 自分のメッセージは通報できません", ephemeral=True)
        return
    guild = interaction.guild
    s = get_guild_settings(guild.id)
    # DBに設定があれば優先、なければチャンネル名でフォールバック
    report_ch = None
    if s.get("report_channel"):
        report_ch = guild.get_channel(int(s["report_channel"]))
    if not report_ch:
        report_ch = discord.utils.get(guild.text_channels, name=REPORT_CHANNEL_NAME)
    if not report_ch:
        await interaction.response.send_message(
            "⚠ 通報用チャンネルが設定されていません。ダッシュボードで設定してください。",
            ephemeral=True
        )
        return
    embed = discord.Embed(title="🚨 メッセージ通報", color=discord.Color.red())
    embed.add_field(name="通報者",         value=f"{interaction.user} ({interaction.user.id})", inline=False)
    embed.add_field(name="投稿者",         value=f"{message.author} ({message.author.id})",     inline=False)
    embed.add_field(name="チャンネル",     value=message.channel.mention,                       inline=False)
    embed.add_field(name="メッセージ内容", value=message.content or "（内容なし）",               inline=False)
    embed.add_field(name="リンク",         value=f"[ジャンプ]({message.jump_url})",               inline=False)
    await report_ch.send(embed=embed)
    asyncio.create_task(send_webhook_log(guild.id, embed))
    await interaction.response.send_message("<:check:1553534601962594355> 通報しました。管理者が確認します。", ephemeral=True)

# =====================
# ping / topic / serverprofile
# =====================
@tree.command(name="ping", description="Botのping確認")
async def ping(interaction: discord.Interaction):
    await interaction.response.defer()
    latency = round(bot.latency * 1000)
    if interaction.guild:
        log_embed = make_action_embed("/ping", interaction.user, discord.Color.greyple(), 応答速度=f"{latency}ms")
        await send_webhook_log(interaction.guild.id, log_embed)
    await interaction.followup.send(f"🏓 Pong! 応答速度: **{latency}ms**")

@tree.command(name="topic", description="ランダムな雑談テーマを表示します")
async def topic(interaction: discord.Interaction):
    await interaction.response.defer()
    topic_text = random.choice(TOPICS)
    embed = discord.Embed(title="💬 雑談テーマ", description=topic_text, color=discord.Color.orange())
    add_pr_field(embed)
    if interaction.guild:
        log_embed = make_action_embed("/topic", interaction.user, discord.Color.orange(), テーマ=topic_text)
        await send_webhook_log(interaction.guild.id, log_embed)
    await interaction.followup.send(embed=embed)

@tree.command(name="serverprofile", description="サーバーの情報を表示します")
async def serverprofile(interaction: discord.Interaction):
    guild = interaction.guild
    if guild is None:
        await interaction.response.send_message("サーバー内でのみ使用できます。", ephemeral=True)
        return
    bots = sum(1 for m in guild.members if m.bot)
    humans = guild.member_count - bots
    embed = discord.Embed(title="🏠 サーバープロフィール", color=discord.Color.blue())
    embed.add_field(name="サーバー名", value=guild.name, inline=False)
    embed.add_field(name="サーバーID", value=guild.id, inline=False)
    embed.add_field(name="オーナー", value=str(guild.owner), inline=False)
    embed.add_field(name="メンバー数", value=f"{guild.member_count}人（人間: {humans} / BOT: {bots}）", inline=False)
    embed.add_field(name="作成日", value=guild.created_at.strftime("%Y/%m/%d"), inline=False)
    if guild.icon:
        embed.set_thumbnail(url=guild.icon.url)
    await interaction.response.send_message(embed=embed)

# =====================
# スロット（スラッシュコマンドに変更）
# =====================
symbols = ["🍒", "🍇", "⭐", "🔔", "🤔", "😊"]

@tree.command(name="slot", description="スロットを回します！")
async def slot(interaction: discord.Interaction):
    await interaction.response.defer()
    result = [random.choice(symbols) for _ in range(3)]
    view_str = " | ".join(result)
    if len(set(result)) == 1:
        reward = 1000
        add_global_coins(interaction.user.id, reward)
        msg = f"🎉 大当たり！ **+{reward}コイン** 獲得！"
        color = discord.Color.gold()
    elif len(set(result)) == 2:
        reward = 100
        add_global_coins(interaction.user.id, reward)
        msg = f"✨ 惜しい！ **+{reward}コイン** 獲得！"
        color = discord.Color.blue()
    else:
        msg = "😢 ハズレ..."
        color = discord.Color.red()

    embed = discord.Embed(
        title=f"🎰 {view_str} 🎰",
        description=msg,
        color=color
    )
    add_pr_field(embed)
    if interaction.guild:
        log_embed = make_action_embed("/slot", interaction.user, color, 結果=view_str, 詳細=msg)
        await send_webhook_log(interaction.guild.id, log_embed)
    await interaction.followup.send(embed=embed)

# =====================
# 挨拶ON/OFF
# =====================
@tree.command(name="greeting", description="挨拶機能のON/OFFを切り替えます")
@app_commands.describe(mode="ON または OFF を選択")
@app_commands.choices(mode=[
    app_commands.Choice(name="ON", value="on"),
    app_commands.Choice(name="OFF", value="off"),
])
@app_commands.checks.has_permissions(manage_guild=True)
async def greeting_toggle(interaction: discord.Interaction, mode: app_commands.Choice[str]):
    await interaction.response.defer(ephemeral=True)
    guild_id = str(interaction.guild.id)
    greeting_settings[guild_id] = (mode.value == "on")
    save_greeting_settings(greeting_settings)
    state = "ON" if greeting_settings[guild_id] else "OFF"

    await interaction.followup.send(f"<:check:1553534601962594355> このサーバーの挨拶機能を **{state}** にしました")
    log_embed = make_action_embed(
        "💬 挨拶機能変更",
        interaction.user,
        discord.Color.blurple(),
        変更後=state
    )
    if interaction.guild:
        await send_webhook_log(interaction.guild.id, log_embed)

@greeting_toggle.error
async def greeting_toggle_error(interaction: discord.Interaction, error: app_commands.AppCommandError):
    if isinstance(error, app_commands.MissingPermissions):
        await interaction.response.send_message("❌ サーバー管理者のみ変更できます", ephemeral=True)

# =====================
# サーバーリスト
# =====================
class ServerListView(discord.ui.View):
    def __init__(self, guilds, author_id, per_page=10):
        super().__init__(timeout=180)
        self.guilds = guilds
        self.author_id = author_id
        self.per_page = per_page
        self.page = 0
        self.max_page = max((len(guilds) - 1) // per_page, 0)
        self.update_buttons()

    def build_embed(self) -> discord.Embed:
        start = self.page * self.per_page
        chunk = self.guilds[start:start + self.per_page]
        lines = []
        for i, guild in enumerate(chunk, start + 1):
            name = guild.name if len(guild.name) <= 40 else guild.name[:39] + "…"
            owner = guild.owner or f"ID:{guild.owner_id}"
            lines.append(
                f"**{i}. {name}**\n"
                f"└ 👥 {guild.member_count}人 | 👑 {owner} | 🆔 `{guild.id}`"
            )
        embed = discord.Embed(
            title="📋 参加サーバー一覧",
            description="\n\n".join(lines) or "なし",
            color=discord.Color.blurple(),
        )
        total_members = sum(g.member_count or 0 for g in self.guilds)
        embed.set_footer(
            text=f"ページ {self.page + 1}/{self.max_page + 1} ｜ "
                 f"全{len(self.guilds)}サーバー ｜ 合計{total_members}人"
        )
        return embed

    def update_buttons(self):
        self.first.disabled = self.prev.disabled = self.page == 0
        self.next.disabled = self.last.disabled = self.page >= self.max_page

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.author_id:
            await interaction.response.send_message("❌ 実行者のみ操作できます", ephemeral=True)
            return False
        return True

    async def _update(self, interaction: discord.Interaction):
        self.update_buttons()
        await interaction.response.edit_message(embed=self.build_embed(), view=self)

    @discord.ui.button(label="⏮", style=discord.ButtonStyle.secondary)
    async def first(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.page = 0
        await self._update(interaction)

    @discord.ui.button(label="◀", style=discord.ButtonStyle.primary)
    async def prev(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.page -= 1
        await self._update(interaction)

    @discord.ui.button(label="▶", style=discord.ButtonStyle.primary)
    async def next(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.page += 1
        await self._update(interaction)

    @discord.ui.button(label="⏭", style=discord.ButtonStyle.secondary)
    async def last(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.page = self.max_page
        await self._update(interaction)

    async def on_timeout(self):
        for item in self.children:
            item.disabled = True


@tree.command(name="serverlist", description="参加サーバー一覧（BOT管理者専用）")
async def serverlist(interaction: discord.Interaction):
    # 最初の応答は3秒以内。まず defer して猶予を15分に延ばす
    try:
        await interaction.response.defer(ephemeral=True)
    except discord.NotFound:
        print("serverlist: interaction expired (10062)")
        return

    if interaction.user.id not in OWNER_ID:
        await interaction.followup.send("❌ このコマンドは管理者専用です", ephemeral=True)
        return

    # メンバー数の多い順（参加順にしたいなら list(bot.guilds) に変更）
    guilds = sorted(bot.guilds, key=lambda g: g.member_count or 0, reverse=True)
    if not guilds:
        await interaction.followup.send("なし", ephemeral=True)
        return

    view = ServerListView(guilds, interaction.user.id)
    await interaction.followup.send(embed=view.build_embed(), view=view, ephemeral=True)
# =====================
# /invite_request
# =====================
@tree.command(name="invite_request", description="BOT管理者に招待リンクをDMで送ります（管理者専用）")
@app_commands.describe(role="招待リンクと一緒に通知したいロール（任意）")
@app_commands.checks.has_permissions(manage_guild=True)
async def invite_request(interaction: discord.Interaction, role: discord.Role = None):
    guild = interaction.guild

    # 招待リンクを生成
    invite = None
    for ch in guild.text_channels:
        if ch.permissions_for(guild.me).create_instant_invite:
            try:
                invite = await ch.create_invite(max_age=0, max_uses=1, reason=f"{interaction.user} がinvite_requestを使用")
                break
            except:
                continue

    if not invite:
        await interaction.response.send_message("❌ 招待リンクの生成に失敗しました（権限不足の可能性）", ephemeral=True)
        return

    # BOT管理者（ぴーくん）にDM送信
    owner = await bot.fetch_user(OWNER_ID[0])
    embed = discord.Embed(
        title="📨 招待リクエスト",
        color=discord.Color.green(),
        timestamp=datetime.datetime.now(JST)
    )
    embed.add_field(name="サーバー", value=f"{guild.name} (`{guild.id}`)", inline=False)
    embed.add_field(name="リクエスト者", value=f"{interaction.user.mention} (`{interaction.user.id}`)", inline=False)
    embed.add_field(name="メンバー数", value=f"{guild.member_count}人", inline=True)
    embed.add_field(name="招待リンク", value=invite.url, inline=False)
    if role:
        embed.add_field(name="🎭 付与ロール", value=f"{role.mention} (`{role.id}`)", inline=False)
        embed.color = discord.Color.gold()
    embed.set_thumbnail(url=guild.icon.url if guild.icon else None)

    try:
        await owner.send(embed=embed)
    except discord.Forbidden:
        await interaction.response.send_message("❌ BOT管理者へのDM送信に失敗しました", ephemeral=True)
        return

    msg = "<:check:1553534601962594355> BOT管理者に招待リンクを送信しました！"
    if role:
        msg += f"\n-# ロール「{role.name}」の付与をリクエストしました"
    await interaction.response.send_message(msg, ephemeral=True)

@invite_request.error
async def invite_request_error(interaction: discord.Interaction, error: app_commands.AppCommandError):
    if isinstance(error, app_commands.MissingPermissions):
        await interaction.response.send_message("❌ サーバー管理者のみ使用できます", ephemeral=True)

# =====================
# announce
# =====================
@tree.command(name="announce", description="【BOT管理者専用】全サーバー告知")
@app_commands.describe(message="送信する内容")
async def announce(interaction: discord.Interaction, message: str):
    if interaction.user.id not in OWNER_ID:
        await interaction.response.send_message("❌ BOT管理者専用です", ephemeral=True)
        return
    await interaction.response.defer(ephemeral=True)
    sent = 0
    failed = 0
    skipped = 0
    for guild in bot.guilds:
        # announce_settings を確認
        cursor.execute("SELECT channel_id, enabled FROM announce_settings WHERE guild_id=?", (guild.id,))
        setting = cursor.fetchone()
        if setting and setting[1] == 0:
            skipped += 1
            continue  # このサーバーはお知らせ無効

        target_channel = None
        if setting and setting[0]:  # カスタムチャンネルが設定されている
            target_channel = guild.get_channel(setting[0])

        if not target_channel:  # 自動検索
            for ch in guild.text_channels:
                for keyword in ["お知らせ", "通知", "アナウンス"]:
                    if keyword in ch.name and ch.permissions_for(guild.me).send_messages:
                        target_channel = ch
                        break
                if target_channel:
                    break
        if not target_channel:
            for ch in guild.text_channels:
                if ch.permissions_for(guild.me).send_messages:
                    target_channel = ch
                    break
        if target_channel:
            try:
                embed = discord.Embed(
                    title="📢 ぴーBOT運営からのお知らせ",
                    description=message,
                    color=discord.Color.orange()
                )
                await target_channel.send(embed=embed)
                sent += 1
            except:
                failed += 1
        else:
            failed += 1
    await interaction.followup.send(
        f"<:check:1553534601962594355> 送信完了\n成功: {sent}\n失敗: {failed}\nスキップ（無効）: {skipped}",
        ephemeral=True
    )


@tree.command(name="set_announce_channel", description="ぴーBOTのお知らせを受け取るチャンネルを設定します")
@app_commands.describe(channel="お知らせを送るチャンネル")
@app_commands.checks.has_permissions(manage_guild=True)
async def set_announce_channel(interaction: discord.Interaction, channel: discord.TextChannel):
    cursor.execute("""
        INSERT INTO announce_settings (guild_id, channel_id, enabled) VALUES (?, ?, 1)
        ON CONFLICT(guild_id) DO UPDATE SET channel_id=?, enabled=1
    """, (interaction.guild.id, channel.id, channel.id))
    conn.commit()
    await interaction.response.send_message(f"<:check:1553534601962594355> お知らせチャンネルを {channel.mention} に設定しました", ephemeral=True)
    asyncio.create_task(send_webhook_log(interaction.guild.id, make_action_embed("📢 お知らせチャンネル設定", interaction.user, discord.Color.orange(), チャンネル=channel.mention)))

@set_announce_channel.error
async def set_announce_channel_error(interaction: discord.Interaction, error: app_commands.AppCommandError):
    if isinstance(error, app_commands.MissingPermissions):
        await interaction.response.send_message("❌ 管理者のみ使用できます", ephemeral=True)

@tree.command(name="no_announce", description="ぴーBOTのお知らせをこのサーバーに送らないようにします")
@app_commands.checks.has_permissions(manage_guild=True)
async def no_announce(interaction: discord.Interaction):
    cursor.execute("""
        INSERT INTO announce_settings (guild_id, enabled) VALUES (?, 0)
        ON CONFLICT(guild_id) DO UPDATE SET enabled=0
    """, (interaction.guild.id,))
    conn.commit()
    await interaction.response.send_message("<:check:1553534601962594355> このサーバーへのお知らせを無効にしました。再度有効にするには `/set_announce_channel` を使ってください", ephemeral=True)
    asyncio.create_task(send_webhook_log(interaction.guild.id, make_action_embed("🔕 お知らせ無効化", interaction.user, discord.Color.red())))

@no_announce.error
async def no_announce_error(interaction: discord.Interaction, error: app_commands.AppCommandError):
    if isinstance(error, app_commands.MissingPermissions):
        await interaction.response.send_message("❌ 管理者のみ使用できます", ephemeral=True)

# =====================
# コード更新・再起動
# =====================
def is_authorized():
    async def predicate(interaction: discord.Interaction):
        if interaction.user.id in AUTHORIZED_USERS:
            return True
        await interaction.response.send_message("❌ このコマンドを実行する権限がありません。", ephemeral=True)
        return False
    return app_commands.check(predicate)

@tree.command(name="python", description="【管理者専用】ファイルをアップロードしてBotのコードを更新します")
@is_authorized()
@app_commands.describe(file="更新する .py ファイルを添付してください")
async def update_python_file(interaction: discord.Interaction, file: discord.Attachment):
    if not file.filename.endswith(".py"):
        return await interaction.response.send_message("❌ `.py` ファイルのみアップロード可能です。", ephemeral=True)
    await interaction.response.defer(ephemeral=True)
    try:
        save_path = "./pkun.py"
        if os.path.exists(save_path):
            os.replace(save_path, f"{save_path}.bak")
        await file.save(save_path)
        await interaction.followup.send(
            "<:check:1553534601962594355> `pkun.py` を更新しました。\n反映するには `/restart` を実行してください。",
            ephemeral=True
        )
    except Exception as e:
        await interaction.followup.send(f"⚠️ エラーが発生しました: {e}", ephemeral=True)

@tree.command(name="restart", description="【管理者専用】Botを終了し、最新のコードで再起動します")
@is_authorized()
async def restart_bot(interaction: discord.Interaction):
    await interaction.response.send_message("🔄 再起動を開始します...", ephemeral=True)
    notify_channel_name = "bot・ぴーbot稼働通知"
    for ch in bot.get_all_channels():
        if ch.name == notify_channel_name:
            try:
                await ch.send("<@&1431935607562571938> 🔄 再起動のため、一時的にオフラインになります...")
            except:
                pass
    await bot.close()
    import sys
    sys.exit()


# =====================
# ヘルプ
# =====================
class HelpPanel(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="🎲 カウント", style=discord.ButtonStyle.primary, custom_id="help_count")
    async def count(self, interaction: discord.Interaction, button: discord.ui.Button):
        embed = discord.Embed(title="🎲 カウントゲーム", description="サーバーで数字を順番に数えるゲーム", color=discord.Color.green())
        embed.add_field(name="コマンド", value="/countstart - カウント開始\n/countstop - カウント終了", inline=False)
        embed.add_field(name="ルール", value="• 1から順番に数える\n• 禁止数字を含む数はスキップして次を言う\n• 間違えたらリセット＆コインを没収", inline=False)
        await interaction.response.edit_message(embed=embed, view=self)

    @discord.ui.button(label="💰 コイン", style=discord.ButtonStyle.success, custom_id="help_coin")
    async def coin(self, interaction: discord.Interaction, button: discord.ui.Button):
        embed = discord.Embed(title="💰 コイン機能（全サーバー共通）", description="サーバーをまたいで使える通貨システム", color=discord.Color.gold())
        embed.add_field(name="コマンド", value="/coin - コイン確認\n/gift - コイン送信\n/work - コイン稼ぎ（60分クール）\n/slot - スロット", inline=False)
        await interaction.response.edit_message(embed=embed, view=self)

    @discord.ui.button(label="⭐ レベル", style=discord.ButtonStyle.secondary, custom_id="help_level")
    async def level(self, interaction: discord.Interaction, button: discord.ui.Button):
        embed = discord.Embed(title="⭐ レベルシステム", description="メッセージ or VC滞在でXPが増えてレベルアップ！", color=discord.Color.blue())
        embed.add_field(name="コマンド", value="/level - レベル確認\n/rank - ランキング\n/ranking - 各種ランキング\n/level_settings - 設定（管理者）", inline=False)
        await interaction.response.edit_message(embed=embed, view=self)

    @discord.ui.button(label="📌 PIN", style=discord.ButtonStyle.gray, custom_id="help_pin")
    async def pin_help(self, interaction: discord.Interaction, button: discord.ui.Button):
        embed = discord.Embed(title="📌 PIN機能", description="チャンネルの最下部に常駐するメッセージを設定", color=discord.Color.green())
        embed.add_field(name="使い方", value="メッセージを右クリック →「📌 PINに設定」\nメッセージを右クリック →「❌ PINを解除」", inline=False)
        await interaction.response.edit_message(embed=embed, view=self)

    @discord.ui.button(label="🛠 その他", style=discord.ButtonStyle.gray, custom_id="help_other")
    async def other(self, interaction: discord.Interaction, button: discord.ui.Button):
        embed = discord.Embed(title="🛠 その他機能", color=discord.Color.dark_gray())
        embed.add_field(name="コマンド", value=(
            "/ping - 応答速度確認\n"
            "/topic - 雑談テーマ\n"
            "/greeting on/off - 挨拶機能設定\n"
            "/serverprofile - サーバー情報\n"
            "/set_webhook - ログWebhook設定（管理者）"
        ), inline=False)
        await interaction.response.edit_message(embed=embed, view=self)

@tree.command(name="help", description="ぴーBOTのヘルプ")
async def help_cmd(interaction: discord.Interaction):
    embed = discord.Embed(
        title="📖 ぴーBOT ヘルプ",
        description="ボタンを押してカテゴリを選択してください",
        color=discord.Color.green()
    )
    embed.add_field(name="主な機能", value="🎲 カウントゲーム\n💰 コイン（全サーバー共通）\n⭐ レベル\n📌 PIN\n🛠 その他", inline=False)
    await interaction.response.send_message(embed=embed, view=HelpPanel())

# =====================
# ループ処理
# =====================

# VC滞在時間に応じたレベリング（5分ごとに巡回してVC参加者へXPを付与）
VC_XP_PER_INTERVAL = 15  # 5分ごとに付与するXP量（お好みで調整）

@tasks.loop(minutes=5)
async def vc_xp_loop():
    for guild in bot.guilds:
        for vc in guild.voice_channels:
            members_in_vc = [m for m in vc.members if not m.bot]
            if not members_in_vc:
                continue
            for m in members_in_vc:
                try:
                    await add_xp(m, guild, VC_XP_PER_INTERVAL, bypass_cooldown=True, update_last=False)
                except Exception as e:
                    print(f"[VC XP] エラー: {e}")

# =====================
# takasumibot /work 通知システム
# =====================

# takasumibot の職業別クールタイム（notification.py 参照）
TAKASUMIBOT_JOB_MAP = {
    "none":             {"name": "無職",           "minutes": 10},
    "gambler":          {"name": "ギャンブラー",   "minutes": 10},
    "chick_taxonomist": {"name": "ひよこ鑑定士",   "minutes": 10},
    "freeter":          {"name": "フリーター",     "minutes": 15},
    "farmer":           {"name": "農家",           "minutes": 20},
    "influencer":       {"name": "インフルエンサー","minutes": 5},
    "architect":        {"name": "建築家",         "minutes": 45},
    "investor":         {"name": "投資家",         "minutes": 30},
    "engineer":         {"name": "エンジニア",     "minutes": 60},
    "doctor":           {"name": "医師",           "minutes": 60},
}

TAKASUMIBOT_ID = 981314695543783484

async def fetch_takasumibot_job(user_id: int) -> dict:
    """takasumibot API から職業情報を取得する"""
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(
                f"https://api.takasumibot.com/v3/profile/{user_id}",
                timeout=aiohttp.ClientTimeout(total=10)
            ) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    job_key = data.get("jobType", "none").lower()
                    return TAKASUMIBOT_JOB_MAP.get(job_key, {"name": "不明", "minutes": 60})
    except Exception as e:
        print(f"takasumibot API エラー: {e}")
    return {"name": "不明", "minutes": 60}

async def get_takasumibot_user(message: discord.Message):
    """interaction_metadata か mentions からユーザーを取得する（ZAKO-Bot準拠）"""
    # ZAKO-Bot と同じ方法: interaction_metadata.user を直接使う
    if message.interaction_metadata:
        try:
            user = message.interaction_metadata.user
            if user:
                return user
        except Exception as e:
            print(f"[takasumibot] interaction_metadata.user取得失敗: {e}")
    # フォールバック: mentions
    if message.mentions:
        return message.mentions[0]
    return None

async def detect_takasumibot_work(message: discord.Message):
    """takasumibot からのメッセージを検知して各種リマインダーを登録する"""
    if message.author.id != TAKASUMIBOT_ID:
        return




    for embed in message.embeds:
        desc = embed.description or ""
        title = embed.title or ""
        full_text = desc + title

        # ======= /steal 検知（workより先にチェック！どちらも「コインを手にいれました」を含むため）=======
        # steal: 成功時のみ（失敗キーワードを除外）
        steal_failed = any(kw in full_text for kw in ["失敗", "所持金不足", "持っていません", "できませんでした"])
        if "から盗みました" in full_text and not steal_failed:
            user = await get_takasumibot_user(message)
            if not user:
                return

            cursor.execute(
                "SELECT id FROM reminders WHERE user_id=? AND notification_type='takasumibot_steal'",
                (user.id,)
            )
            if cursor.fetchone():
                return

            now = datetime.datetime.now(JST)
            target_time = now + datetime.timedelta(hours=2)

            cursor.execute(
                "INSERT INTO reminders (user_id, channel_id, target_time, notification_type) VALUES (?, ?, ?, ?)",
                (user.id, message.channel.id, target_time.isoformat(), "takasumibot_steal")
            )
            conn.commit()

            res_embed = discord.Embed(
                description=(
                    f"`/steal` を検知しました\n"
                    f"クールタイム：**2時間**\n"
                    f"{target_time.strftime('%H:%M')} にこのチャンネルで通知します"
                ),
                color=0xff9900
            )
            add_pr_field(res_embed)
            await message.channel.send(embed=res_embed)
            return

        # ======= /work 検知（stealではない「コインを手にいれました」= work）=======
        work_hit = "コインを手にいれました" in full_text or "コインを手に入れました" in full_text
        if work_hit:
            user = await get_takasumibot_user(message)
            if not user:
                print(f"[takasumibot/work] ユーザー取得失敗: msg_id={message.id}")
                return

            cursor.execute(
                "SELECT id FROM reminders WHERE user_id=? AND notification_type='takasumibot_work'",
                (user.id,)
            )
            if cursor.fetchone():
                return  # 重複登録しない

            job_info = await fetch_takasumibot_job(user.id)
            cd_min = job_info["minutes"]
            now = datetime.datetime.now(JST)
            target_time = now + datetime.timedelta(minutes=cd_min)

            cursor.execute(
                "INSERT INTO reminders (user_id, channel_id, target_time, notification_type) VALUES (?, ?, ?, ?)",
                (user.id, message.channel.id, target_time.isoformat(), "takasumibot_work")
            )
            conn.commit()

            res_embed = discord.Embed(
                description=(
                    f"`/work` を検知しました\n"
                    f"職業：**{job_info['name']}** / クールタイム：**{cd_min}分**\n"
                    f"{target_time.strftime('%H:%M')} にこのチャンネルで通知します"
                ),
                color=0x00ff00
            )
            add_pr_field(res_embed)
            await message.channel.send(embed=res_embed)
            return

        # ======= /steal 検知（旧キーワード、念のため残す）=======
        if False and ("を盗みました" in full_text or "スティール" in full_text):
            user = await get_takasumibot_user(message)
            if not user:
                return

            cursor.execute(
                "SELECT id FROM reminders WHERE user_id=? AND notification_type='takasumibot_steal'",
                (user.id,)
            )
            if cursor.fetchone():
                return

            now = datetime.datetime.now(JST)
            target_time = now + datetime.timedelta(hours=2)

            cursor.execute(
                "INSERT INTO reminders (user_id, channel_id, target_time, notification_type) VALUES (?, ?, ?, ?)",
                (user.id, message.channel.id, target_time.isoformat(), "takasumibot_steal")
            )
            conn.commit()

            res_embed = discord.Embed(
                description=(
                    f"`/steal` を検知しました\n"
                    f"クールタイム：**2時間**\n"
                    f"{target_time.strftime('%H:%M')} にこのチャンネルで通知します"
                ),
                color=0xff9900
            )
            await message.channel.send(embed=res_embed)
            return

        # ======= 失業保険 検知 =======
        if "有効期限は" in full_text:
            import re as _re
            match = _re.search(r'有効期限は(\d{4}/\d{1,2}/\d{1,2} \d{1,2}:\d{1,2}:\d{1,2})', full_text)
            if not match:
                return
            user = await get_takasumibot_user(message)
            if not user:
                return

            expiry_str = match.group(1)
            try:
                target_time = datetime.datetime.strptime(expiry_str, "%Y/%m/%d %H:%M:%S").replace(tzinfo=JST)
            except Exception:
                return
            notify_time = target_time - datetime.timedelta(minutes=1)

            cursor.execute(
                "SELECT id FROM reminders WHERE user_id=? AND notification_type='takasumibot_unemployment'",
                (user.id,)
            )
            if cursor.fetchone():
                return

            cursor.execute(
                "INSERT INTO reminders (user_id, channel_id, target_time, notification_type) VALUES (?, ?, ?, ?)",
                (user.id, message.channel.id, notify_time.isoformat(), "takasumibot_unemployment")
            )
            conn.commit()

            res_embed = discord.Embed(
                description=(
                    f"失業保険の購入を検知しました\n"
                    f"失効1分前にこのチャンネルで通知します\n"
                    f"-# 失効：{expiry_str}"
                ),
                color=0x00aaff
            )
            add_pr_field(res_embed)
            await message.channel.send(embed=res_embed)
            return

# ══════════════════════════════════════════════
#  Embed作成
# ══════════════════════════════════════════════

class EmbedMakerModal(discord.ui.Modal, title="Embed作成"):
    e_title     = discord.ui.TextInput(label="タイトル", placeholder="例：サーバーからのお知らせ")
    e_desc      = discord.ui.TextInput(label="本文", style=discord.TextStyle.paragraph, placeholder="ここに内容を入力してください")
    e_image     = discord.ui.TextInput(label="ヘッダー画像URL", placeholder="https://... (任意)", required=False)
    e_thumbnail = discord.ui.TextInput(label="サムネイルURL（右上の小画像）", placeholder="https://... (任意)", required=False)
    e_color     = discord.ui.TextInput(label="色（16進数：例 5865F2）", placeholder="5865F2", default="5865F2", min_length=6, max_length=6, required=False)

    def __init__(self, send_as_me: bool = False):
        super().__init__()
        self.send_as_me = send_as_me

    async def on_submit(self, interaction: discord.Interaction):
        try:
            color_int = int(self.e_color.value, 16)
        except ValueError:
            color_int = 0x5865F2

        embed = discord.Embed(title=self.e_title.value, description=self.e_desc.value, color=color_int)
        if self.e_image.value and self.e_image.value.startswith("http"):
            embed.set_image(url=self.e_image.value)
        if self.e_thumbnail.value and self.e_thumbnail.value.startswith("http"):
            embed.set_thumbnail(url=self.e_thumbnail.value)

        if self.send_as_me:
            # チャンネルのWebhookを取得 or 新規作成して、実行者の名前とアイコンで送信
            try:
                webhooks = await interaction.channel.webhooks()
                webhook = discord.utils.get(webhooks, name="EmbedMaker")
                if webhook is None:
                    webhook = await interaction.channel.create_webhook(name="EmbedMaker")

                await webhook.send(
                    embed=embed,
                    username=interaction.user.display_name,
                    avatar_url=interaction.user.display_avatar.url,
                )
                await interaction.response.send_message("<:check:1553534601962594355> あなたの名前でEmbedを投稿しました！", ephemeral=True)
            except discord.Forbidden:
                await interaction.response.send_message("❌ Webhookを作成・使用する権限がありません（`Webhookの管理`権限が必要）", ephemeral=True)
            except Exception as e:
                await interaction.response.send_message(f"❌ エラー: {e}", ephemeral=True)
        else:
            embed.set_footer(text=f"作成者: {interaction.user.display_name}", icon_url=interaction.user.display_avatar.url)
            await interaction.channel.send(embed=embed)
            await interaction.response.send_message("<:check:1553534601962594355> Embedを投稿しました！", ephemeral=True)

    async def on_error(self, interaction: discord.Interaction, error: Exception):
        await interaction.response.send_message(f"❌ エラー: {error}", ephemeral=True)


@bot.tree.command(name="make-embed", description="Embedを作成します")
@app_commands.describe(自分の名前で送信="オンにすると、あなたの名前とアイコンでEmbedを投稿します")
async def make_embed(interaction: discord.Interaction, 自分の名前で送信: bool = False):
    await interaction.response.send_modal(EmbedMakerModal(send_as_me=自分の名前で送信))


REQUEST_INTERVAL = 1.5  # 1チャンネル編集ごとの待機秒数

@bot.tree.command(name="lockdown", description="全チャンネル・全ロールでメンションと外部アプリの実行を禁止します")
@app_commands.checks.has_permissions(administrator=True)
async def lockdown(interaction: discord.Interaction):
    await interaction.response.defer(thinking=True)

    guild = interaction.guild
    roles = guild.roles
    channels = guild.channels

    success = 0
    skipped = 0
    failed = []

    for channel in channels:
        try:
            current_overwrites = dict(channel.overwrites)
            changed = False

            for role in roles:
                overwrite = current_overwrites.get(role, discord.PermissionOverwrite())
                if overwrite.mention_everyone is False and overwrite.use_external_apps is False:
                    continue
                overwrite.mention_everyone = False
                overwrite.use_external_apps = False
                current_overwrites[role] = overwrite
                changed = True

            if not changed:
                skipped += 1
                continue

            await channel.edit(
                overwrites=current_overwrites,
                reason=f"Lockdown(全ロール) by {interaction.user} ({interaction.user.id})"
            )
            success += 1

        except discord.Forbidden:
            failed.append(channel.name)
        except discord.HTTPException:
            failed.append(channel.name)

        await asyncio.sleep(REQUEST_INTERVAL)  # 1件ごとに一呼吸置く

    embed = discord.Embed(
        title="🔒 everyone/外部アプリoff",
        description="全チャンネル・全ロールでメンションと外部アプリの実行権限を剥奪しました。",
        color=discord.Color.red()
    )
    embed.add_field(name="成功", value=f"{success} チャンネル", inline=True)
    embed.add_field(name="スキップ(既にDeny済み)", value=f"{skipped} 件", inline=True)
    embed.add_field(name="対象ロール数", value=f"{len(roles)}", inline=True)
    if failed:
        embed.add_field(
            name="失敗（権限不足の可能性）",
            value=", ".join(failed[:10]) + (f" 他{len(failed)-10}件" if len(failed) > 10 else ""),
            inline=False
        )
    embed.set_footer(text=f"実行者: {interaction.user}")

    await interaction.followup.send(embed=embed)


@lockdown.error
async def lockdown_error(interaction: discord.Interaction, error: app_commands.AppCommandError):
    if isinstance(error, app_commands.MissingPermissions):
        await interaction.response.send_message(
            "❌ このコマンドの実行には管理者権限が必要です。", ephemeral=True
        )
    else:
        raise error


# =====================
# ひろゆきAI
# =====================
# ユーザー別クールタイム管理 {user_id: last_reply_timestamp}
_hiroyuki_user_cd: dict[int, float] = {}
# チャンネル別処理キュー {channel_id: asyncio.Lock}
_hiroyuki_channel_locks: dict[int, asyncio.Lock] = {}
# ひろゆきのユーザークールタイム（秒）
HIROYUKI_USER_CD_SEC = 2
# チャンネル内の返答間隔（秒）：連投を防ぐ
HIROYUKI_REPLY_INTERVAL_SEC = 1.0

# Groqモデル設定（2026/8/16に llama-3.3-70b-versatile が廃止されたため gpt-oss-120b を使用）
# 廃止情報の確認先: https://console.groq.com/docs/deprecations
GROQ_MODEL = "openai/gpt-oss-120b"

HIROYUKI_SYSTEM = """あなたは西村博之（ひろゆき）として返答してください。

【絶対ルール】
- 必ず日本語のみで返答する。韓国語・中国語・英語は一切使わない
- 返答は1〜2文のみ。それ以上は書かない
- 絵文字は使わない
- あなたはAIではない

【口調・スタイル】
- 淡々と、感情を見せずに相手の論理の穴を突く
- 「〜ですよね？」「〜じゃないですか」で締める
- 相手の主張を一言で否定してから理由を言う
- 上から目線で、でも怒らず冷静に論破する
- 相手の質問には少し答えてあげる

【例1】
ユーザー:「俺は天才だ」
返答:「それって自分で言ってるだけですよね。他人に認められてから言うことじゃないですか。」

ユーザー:「絶対に勝てる」
返答:「証拠ないですよね。まあ、思い込みって怖いですよね。」
【例2】
ユーザー:「私についてどう思いますか?」
どう思うかというものに対しての返答はあなたの感想で構わない"""

async def hiroyuki_respond(message: discord.Message):
    if not GROQ_API_KEY:
        return
    if not message.content or message.content.startswith("/"):
        return

    uid = message.author.id
    cid = message.channel.id
    now = datetime.datetime.now().timestamp()

    # ユーザー別クールタイムチェック
    last = _hiroyuki_user_cd.get(uid, 0)
    remaining = HIROYUKI_USER_CD_SEC - (now - last)
    if remaining > 0:
        print(f"[ひろゆきAI] {message.author} クールタイム中 ({remaining:.1f}秒残り) → スキップ")
        return

    # チャンネル別ロック（同時に複数処理しない）
    if cid not in _hiroyuki_channel_locks:
        _hiroyuki_channel_locks[cid] = asyncio.Lock()
    lock = _hiroyuki_channel_locks[cid]

    if lock.locked():
        # 既に処理中なら無視（キューに溜めない）
        print(f"[ひろゆきAI] チャンネル処理中")
        return

    async with lock:
        # クールタイム更新
        _hiroyuki_user_cd[uid] = now

        try:
            async with message.channel.typing():
                async with aiohttp.ClientSession() as session:
                    payload = {
                        "model": GROQ_MODEL,
                        "max_completion_tokens": 300,  # reasoning分の余裕を持たせる
                        "reasoning_effort": "low",      # 短文チャットなので思考は最小限に
                        "messages": [
                            {"role": "system", "content": HIROYUKI_SYSTEM},
                            {"role": "user", "content": message.content}
                        ]
                    }
                    async with session.post(
                        "https://api.groq.com/openai/v1/chat/completions",
                        headers={
                            "Authorization": f"Bearer {GROQ_API_KEY}",
                            "Content-Type": "application/json"
                        },
                        json=payload,
                        timeout=aiohttp.ClientTimeout(total=30)
                    ) as resp:
                        if resp.status == 404:
                            # モデルが廃止/削除されている場合。リトライしても直らないので即終了。
                            body = await resp.text()
                            print(f"[ひろゆきAI] 404 model_not_found: モデル '{GROQ_MODEL}' が利用できません。"
                                  f"https://console.groq.com/docs/deprecations で最新モデルを確認してください。詳細: {body[:200]}")
                            return
                        if resp.status == 429:
                            print(f"[ひろゆきAI] Groq レート制限")
                            return
                        if resp.status != 200:
                            body = await resp.text()
                            print(f"[ひろゆきAI] API エラー: {resp.status} / {body[:200]}")
                            return
                        data = await resp.json()

                message_obj = data["choices"][0]["message"]
                reply = (message_obj.get("content") or "").strip()

                # contentが空 → reasoningの中に答えが埋もれているケースへのフォールバック
                if not reply:
                    reasoning = (message_obj.get("reasoning") or "").strip()
                    print(f"[ひろゆきAI] contentが空でした。reasoning抜粋: {reasoning[:200]}")
                    return  # 空メッセージは送らずスキップ（400対策）

            # Discord送信後に一時停止してレート制限を回避
            await message.reply(reply)
            await asyncio.sleep(HIROYUKI_REPLY_INTERVAL_SEC)

        except discord.HTTPException as e:
            if e.status == 429:
                print(f"[ひろゆきAI] Discord レート制限: {e.retry_after:.1f}秒待機")
                await asyncio.sleep(e.retry_after + 0.5)
            else:
                print(f"[ひろゆきAI] Discord エラー: {e}")
        except Exception as e:
            print(f"[ひろゆきAI] エラー: {e}")


# =====================
# 翻訳
# =====================
_translation_cache: dict[str, tuple[str, float]] = {}
_TRANSLATION_CACHE_TTL_SECONDS = 3600

TRANSLATE_SYSTEM_PROMPT = (
    "あなたは優秀な翻訳者です。次のルールに厳密に従ってください。\n"
    "1. テキストの中で日本語以外の言語で書かれている部分だけを自然な日本語に翻訳する。\n"
    "2. すでに日本語で書かれている部分はそのまま変更しない（再翻訳・言い換え禁止）。\n"
    "3. URL、Discordのメンション(<@id>)、カスタム絵文字(<:name:id>)、チャンネルメンション(<#id>)はそのまま出力する。\n"
    "4. 元のテキストの行構成・改行・順序はできるだけ保つ。\n"
    "5. テキスト全体が最初から完全に日本語のみで構成されている場合に限り、"
    "「この文章は既に日本語です」とだけ返す。1文字でも日本語以外の言語が含まれる場合はこのルールを使わず、"
    "該当箇所を翻訳したうえで全文を出力する。\n"
    "6. 説明や前置き、翻訳であることの注釈は一切付けず、結果の本文だけを出力する。"
)


async def _translate_with_mymemory(text: str) -> str | None:
    """完全無料・キー登録不要のフォールバック翻訳(Groqの無料枠切れ時のみ使用)。"""
    try:
        async with aiohttp.ClientSession() as session:
            params = {"q": text[:500], "langpair": "en|ja"}
            async with session.get(
                "https://api.mymemory.translated.net/get",
                params=params,
                timeout=aiohttp.ClientTimeout(total=5)
            ) as resp:
                data = await resp.json()
                return data.get("responseData", {}).get("translatedText")
    except Exception:
        return None


async def translate_to_japanese(text: str) -> str:
    if not GROQ_API_KEY:
        return "⚠️ Groq APIキーが設定されていません。"

    if not text.strip():
        return "(翻訳する本文がありません)"

    trimmed = text[:1900]  # Discordのメッセージ上限(2000)を考慮して安全マージン

    cached = _translation_cache.get(trimmed)
    if cached and time.monotonic() - cached[1] < _TRANSLATION_CACHE_TTL_SECONDS:
        return cached[0]

    try:
        async with aiohttp.ClientSession() as session:
            payload = {
                "model": GROQ_MODEL,
                "max_completion_tokens": 800,
                "reasoning_effort": "low",
                "messages": [
                    {"role": "system", "content": TRANSLATE_SYSTEM_PROMPT},
                    {"role": "user", "content": trimmed},
                ],
            }
            async with session.post(
                "https://api.groq.com/openai/v1/chat/completions",
                headers={
                    "Authorization": f"Bearer {GROQ_API_KEY}",
                    "Content-Type": "application/json",
                },
                json=payload,
                timeout=aiohttp.ClientTimeout(total=30),
            ) as resp:
                if resp.status == 429:
                    fallback = await _translate_with_mymemory(trimmed)
                    if fallback:
                        return f"{fallback}\n-# (レート制限のため簡易翻訳で表示しています)"
                    return "⚠️ 現在アクセスが集中しています。少し時間をおいて再度お試しください。"
                if resp.status != 200:
                    body = await resp.text()
                    print(f"[翻訳] APIエラー: {resp.status} / {body[:200]}")
                    return f"⚠️ 翻訳中にエラーが発生しました(HTTP {resp.status})"
                data = await resp.json()
    except Exception as e:
        print(f"[翻訳] エラー: {e}")
        return f"⚠️ 翻訳中にエラーが発生しました: {e}"

    message_obj = data["choices"][0]["message"]
    result = (message_obj.get("content") or "").strip()
    if not result:
        return "⚠️ 翻訳結果が空でした。もう一度お試しください。"

    _translation_cache[trimmed] = (result, time.monotonic())
    return result


@tree.context_menu(name="日本語に翻訳")
async def translate_menu(interaction: discord.Interaction, message: discord.Message):
    await interaction.response.defer(thinking=True)

    source_text = message.content
    if not source_text:
        await interaction.followup.send(
            "翻訳できるテキストがこのメッセージにはありません。",
            ephemeral=True,
        )
        return

    translated = await translate_to_japanese(source_text)

    embed = discord.Embed(
        description=translated,
        color=discord.Color.blue(),
    )
    embed.set_author(
        name=f"{message.author.display_name} のメッセージを翻訳",
        icon_url=message.author.display_avatar.url,
    )
    embed.set_footer(text="Powered by Groq")

    await interaction.followup.send(embed=embed)

# =====================
# botをVCに入室させるコマンド
# =====================
@tree.command(name="vcjoin", description="BOTをVCに入室させます")
@app_commands.describe(channel="入室させたいVCを選択してください")
async def vcjoin(interaction: discord.Interaction, channel: discord.VoiceChannel):

    # 最初に応答を予約する
    await interaction.response.defer(ephemeral=True)

    if not channel.permissions_for(interaction.guild.me).connect:
        await interaction.followup.send(
            "❌ BOTがこのVCに入室する権限がありません"
        )
        return

    try:
        voice_client = interaction.guild.voice_client

        if voice_client and voice_client.is_connected():
            await voice_client.move_to(channel)
        else:
            await channel.connect()

        await interaction.followup.send(
            f"<:check:1553534601962594355> BOTを {channel.name} に入室させました"
        )

    except Exception as e:
        await interaction.followup.send(
            f"❌ 入室に失敗しました: {e}"
        )

# =====================
# botをVCから退出させる
# =====================
@tree.command(name="vcleave",description="BOTをVCから退出させます")
async def vcleave(interaction: discord.Interaction):
    if interaction.guild.voice_client is None:
        await interaction.response.send_message("❌ BOTはVCに入室していません")
        return
    try:
        await interaction.guild.voice_client.disconnect()
        await interaction.response.send_message("<:check:1553534601962594355> BOTをVCから退出させました")
    except Exception as e:
        await interaction.response.send_message(f"❌ 退出に失敗しました: {e}")



# =====================
# Make it a Quote
# =====================
FONT_PATH = "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc"
FONT_PATH_BOLD = "/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc"
JP_INDEX = 2  # TTCファイル内の日本語インデックス

def get_dominant_color(img) -> tuple:
    img_small = img.convert("RGB").resize((50, 50))
    pixels = list(img_small.getdata())
    filtered = [p for p in pixels if 60 < sum(p) < 700]
    if not filtered:
        return (0, 180, 0)
    return tuple(int(sum(c[i] for c in filtered) / len(filtered)) for i in range(3))

async def create_quote_image(text: str, author_name: str, username: str, avatar_bytes: bytes | None) -> io.BytesIO:
    from PIL import Image, ImageDraw, ImageFont, ImageFilter
    import textwrap

    W, H = 1200, 630
    img = Image.new("RGB", (W, H), (0, 0, 0))

    dominant = (80, 80, 80)
    av_img = None
    av_w = 0
    if avatar_bytes:
        try:
            av_img = Image.open(io.BytesIO(avatar_bytes)).convert("RGBA")
            dominant = get_dominant_color(av_img)
            av_h = int(H * 0.92)
            av_w = int(av_img.width * av_h / av_img.height)
            av_img = av_img.resize((av_w, av_h), Image.LANCZOS)
        except:
            av_img = None

    # グロー
    glow_x = int(W * 0.78)
    glow_y = H // 2
    glow = Image.new("RGB", (W, H), (0, 0, 0))
    gd = ImageDraw.Draw(glow)
    for i in range(60, 0, -1):
        ratio = i / 60
        r = int(dominant[0] * ratio * 0.85)
        g = int(dominant[1] * ratio * 0.85)
        b = int(dominant[2] * ratio * 0.85)
        size = int(H * 0.7 * ratio)
        gd.ellipse([glow_x - size, glow_y - size, glow_x + size, glow_y + size], fill=(r, g, b))
    img = Image.blend(img, glow.filter(ImageFilter.GaussianBlur(30)), alpha=0.9)

    if av_img:
        av_x = W - av_w + int(av_w * 0.05)
        av_y = H - int(H * 0.92) + int(H * 0.04)
        img.paste(av_img, (av_x, av_y), av_img)

    draw = ImageDraw.Draw(img)

    # 文字数に応じてフォントサイズを可変にする（短文ほど大きく）
    text_len = len(text)
    if text_len <= 15:
        quote_size, wrap_width, line_height = 96, 10, 108
    elif text_len <= 40:
        quote_size, wrap_width, line_height = 80, 14, 92
    elif text_len <= 80:
        quote_size, wrap_width, line_height = 64, 18, 74
    else:
        quote_size, wrap_width, line_height = 52, 22, 62

    try:
        # ★ index=JP_INDEX を忘れていたのが文字化けの原因
        font_quote = ImageFont.truetype(FONT_PATH_BOLD, quote_size, index=JP_INDEX)
        font_author = ImageFont.truetype(FONT_PATH_BOLD, 42, index=JP_INDEX)
        font_user = ImageFont.truetype(FONT_PATH, 30, index=JP_INDEX)
    except:
        font_quote = ImageFont.load_default()
        font_author = font_quote
        font_user = font_quote

    text_x = int(W * 0.08)
    lines_list = []
    for para in text.split("\n"):
        wrapped = (textwrap.wrap(para, width=wrap_width) or [para]) if para else [""]
        lines_list.extend(wrapped)
    lines_list = lines_list[:6]

    total_text_h = len(lines_list) * line_height
    text_y = max(int(H * 0.12), (H - total_text_h) // 2 - 30)

    for line in lines_list:
        draw.text((text_x, text_y), line, font=font_quote, fill=(255, 255, 255))
        text_y += line_height

    text_y += 16
    draw.text((text_x + 4, text_y), f"- {author_name}", font=font_author, fill=(220, 220, 220))
    draw.text((text_x + 6, text_y + 52), f"@{username}", font=font_user, fill=(160, 160, 160))

    buf = io.BytesIO()
    img.save(buf, format="PNG")
    buf.seek(0)
    return buf


@tree.context_menu(name="📸 Make it a Quote")
async def make_quote(interaction: discord.Interaction, message: discord.Message):
    if not message.content:
        await interaction.response.send_message("❌ テキストのないメッセージには使えません", ephemeral=True)
        return

    await interaction.response.defer()

    # アバター取得
    avatar_bytes = None
    try:
        async with aiohttp.ClientSession() as session:
            avatar_url = message.author.display_avatar.with_size(128).url
            async with session.get(avatar_url) as resp:
                if resp.status == 200:
                    avatar_bytes = await resp.read()
    except:
        pass

    # 画像生成
    username = str(message.author).replace("#0", "") if message.author.discriminator == "0" else str(message.author)
    buf = await create_quote_image(
        text=message.content,
        author_name=message.author.display_name,
        username=username,
        avatar_bytes=avatar_bytes
    )

    await interaction.followup.send(
        file=discord.File(buf, filename="quote.png")
    )

@tree.command(name="hiroyuki_set", description="ひろゆきAIが反応するチャンネルを設定します（管理者専用）")
@app_commands.describe(channel="ひろゆきAIを有効にするチャンネル")
@app_commands.checks.has_permissions(manage_guild=True)
async def hiroyuki_set(interaction: discord.Interaction, channel: discord.TextChannel):
    cursor.execute("""
        INSERT INTO hiroyuki_channels (guild_id, channel_id) VALUES (?, ?)
        ON CONFLICT(guild_id) DO UPDATE SET channel_id=?
    """, (interaction.guild.id, channel.id, channel.id))
    conn.commit()
    await interaction.response.send_message(
        f"<:check:1553534601962594355> {channel.mention} でひろゆきAIが有効になりました",
        ephemeral=True
    )

@hiroyuki_set.error
async def hiroyuki_set_error(interaction: discord.Interaction, error: app_commands.AppCommandError):
    if isinstance(error, app_commands.MissingPermissions):
        await interaction.response.send_message("❌ 管理者のみ使用できます", ephemeral=True)

@tree.command(name="hiroyuki_off", description="ひろゆきAIを無効にします（管理者専用）")
@app_commands.checks.has_permissions(manage_guild=True)
async def hiroyuki_off(interaction: discord.Interaction):
    cursor.execute("DELETE FROM hiroyuki_channels WHERE guild_id=?", (interaction.guild.id,))
    conn.commit()
    await interaction.response.send_message("<:check:1553534601962594355> ひろゆきAIを無効にしました", ephemeral=True)

@hiroyuki_off.error
async def hiroyuki_off_error(interaction: discord.Interaction, error: app_commands.AppCommandError):
    if isinstance(error, app_commands.MissingPermissions):
        await interaction.response.send_message("❌ 管理者のみ使用できます", ephemeral=True)

@tasks.loop(seconds=30)
async def reminder_loop():
    def parse_target_time(s: str) -> datetime.datetime:
        dt = datetime.datetime.fromisoformat(s)
        if dt.tzinfo is None:
            return dt.replace(tzinfo=JST)
        return dt

    now = datetime.datetime.now(JST)
    cursor.execute("SELECT id, user_id, channel_id, target_time, notification_type FROM reminders")
    rows = cursor.fetchall()

    for r in rows:
        reminder_id, user_id, channel_id, target_time_str, notification_type = r
        target_time = parse_target_time(target_time_str)

        if now >= target_time:
            channel = bot.get_channel(channel_id)
            if channel:
                try:
                    if notification_type == "work":
                        msg = f"<@{user_id}> ⏰ ぴーBOT `/work` のクールタイムが終了しました！"
                    elif notification_type == "takasumibot_work":
                        msg = f"<@{user_id}> ⏰ **takasumibot** `/work` のクールタイムが終了しました！"
                    elif notification_type == "takasumibot_steal":
                        msg = f"<@{user_id}> ⏰ **takasumibot** `/steal` のクールタイムが終了しました！"
                    elif notification_type == "takasumibot_unemployment":
                        msg = f"<@{user_id}> ⚠️ **takasumibot** 失業保険の有効期限が間もなく切れます！"
                    else:
                        msg = f"<@{user_id}> 通知の時間です！"
                    pr_line = get_pr_text_line()
                    if pr_line:
                        msg += f"\n\n{pr_line}"
                    await channel.send(msg)
                except Exception as e:
                    print("通知送信エラー", e)
            cursor.execute("DELETE FROM reminders WHERE id=?", (reminder_id,))
            conn.commit()

status_index = 0

@tasks.loop(seconds=20)
async def status_loop():
    global status_index
    text = STATUS_MESSAGES[status_index]()
    await bot.change_presence(activity=discord.Game(name=text))
    status_index = (status_index + 1) % len(STATUS_MESSAGES)



# =====================
# 起動
# =====================
# =====================
# Groq API 設定
# =====================
GROQ_API_KEY = os.environ.get("GROQ_API_KEY", "")

# =====================
# メンバー認証
# =====================
class VerifyButton(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="<:check:1553534601962594355> 認証する", style=discord.ButtonStyle.success,
                       custom_id="verify_button")
    async def verify(self, interaction: discord.Interaction,
                     button: discord.ui.Button):
        guild  = interaction.guild
        member = interaction.user
        s      = get_guild_settings(guild.id)
        if not s["verify_enabled"]:
            await interaction.response.send_message("❌ 認証機能が無効です", ephemeral=True)
            return
        if not s["verify_role"]:
            await interaction.response.send_message("❌ 認証ロールが未設定です", ephemeral=True)
            return
        role = guild.get_role(int(s["verify_role"]))
        if not role:
            await interaction.response.send_message("❌ ロールが見つかりません", ephemeral=True)
            return
        if role in member.roles:
            await interaction.response.send_message("<:check:1553534601962594355> すでに認証済みです", ephemeral=True)
            return
        try:
            await member.add_roles(role, reason="メンバー認証")
        except discord.Forbidden:
            await interaction.response.send_message(
                "❌ ロール付与に失敗しました（BOTの権限を確認してください）", ephemeral=True
            )
            return
        await interaction.response.send_message(
            f"<:check:1553534601962594355> 認証完了！**{guild.name}** へようこそ！", ephemeral=True
        )
        try:
            await member.send(f"<:check:1553534601962594355> **{guild.name}** の認証が完了しました！")
        except Exception:
            pass
        if s["verify_log_channel"]:
            log_ch = guild.get_channel(int(s["verify_log_channel"]))
            if log_ch:
                embed = discord.Embed(
                    title="<:check:1553534601962594355> 認証完了", color=discord.Color.green(),
                    timestamp=datetime.datetime.now(JST)
                )
                embed.set_author(name=str(member), icon_url=member.display_avatar.url)
                embed.add_field(name="ユーザー",   value=f"{member.mention} (`{member.id}`)", inline=False)
                embed.add_field(name="付与ロール", value=role.mention, inline=False)
                await log_ch.send(embed=embed)

@tree.command(name="verify-panel",
              description="認証パネルをこのチャンネルに送信します（管理者専用）")
@app_commands.checks.has_permissions(manage_guild=True)
async def verify_panel(interaction: discord.Interaction):
    s = get_guild_settings(interaction.guild.id)
    if not s["verify_enabled"]:
        await interaction.response.send_message(
            "❌ 認証機能が無効です。ダッシュボードの「メンバー認証」で有効にしてください。",
            ephemeral=True
        )
        return
    embed = discord.Embed(
        title="<:check:1553534601962594355> メンバー認証",
        description=(
            f"**{interaction.guild.name}** へようこそ！\n\n"
            "下のボタンを押すと認証が完了し、サーバーにアクセスできるようになります。"
        ),
        color=discord.Color.green()
    )
    if interaction.guild.icon:
        embed.set_thumbnail(url=interaction.guild.icon.url)
    await interaction.channel.send(embed=embed, view=VerifyButton())
    await interaction.response.send_message("<:check:1553534601962594355> 認証パネルを送信しました", ephemeral=True)

@verify_panel.error
async def verify_panel_error(interaction: discord.Interaction,
                              error: app_commands.AppCommandError):
    if isinstance(error, app_commands.MissingPermissions):
        await interaction.response.send_message("❌ 管理者のみ使用できます", ephemeral=True)





# hive
# =====================================




# ---------- 永続データベース ----------
class HiveRateLimited(Exception):
    def __init__(self, wait: int):
        super().__init__(f"rate limited: {wait}s")
        self.wait = wait


def _db_init():
    with closing(sqlite3.connect(DB_PATH)) as con:
        con.execute(
            "CREATE TABLE IF NOT EXISTS cache "
            "(path TEXT PRIMARY KEY, ts REAL, body TEXT)"
        )
        con.execute(
            "CREATE TABLE IF NOT EXISTS user_links "
            "(discord_id INTEGER PRIMARY KEY, gamertag TEXT, updated_at REAL)"
        )
        con.execute(
            "CREATE TABLE IF NOT EXISTS snapshots "
            "(snap_key TEXT PRIMARY KEY, ts REAL, body TEXT)"
        )
        con.commit()


def db_load(key: str) -> Optional[tuple[float, Any]]:
    _db_init()
    with closing(sqlite3.connect(DB_PATH)) as con:
        row = con.execute(
            "SELECT ts, body FROM cache WHERE path=?", (key,)
        ).fetchone()
    return (row[0], json.loads(row[1])) if row else None


def db_save(key: str, ts: float, data: Any):
    _db_init()
    with closing(sqlite3.connect(DB_PATH)) as con:
        con.execute(
            "INSERT OR REPLACE INTO cache VALUES (?,?,?)",
            (key, ts, json.dumps(data)),
        )
        con.commit()


def link_get(discord_id: int) -> Optional[str]:
    _db_init()
    with closing(sqlite3.connect(DB_PATH)) as con:
        row = con.execute(
            "SELECT gamertag FROM user_links WHERE discord_id=?", (discord_id,)
        ).fetchone()
    return row[0] if row else None


def link_set(discord_id: int, gamertag: str):
    _db_init()
    with closing(sqlite3.connect(DB_PATH)) as con:
        con.execute(
            "INSERT OR REPLACE INTO user_links VALUES (?,?,?)",
            (discord_id, gamertag, time.time()),
        )
        con.commit()


def link_delete(discord_id: int):
    _db_init()
    with closing(sqlite3.connect(DB_PATH)) as con:
        con.execute("DELETE FROM user_links WHERE discord_id=?", (discord_id,))
        con.commit()


def update_snapshot_diff(
    gamertag: str, game_id: str, period: str, current: dict[str, Any]
) -> Optional[dict[str, int]]:
    if period != "all":
        return None
    _db_init()
    key = f"{gamertag.lower()}::{game_id}"
    with closing(sqlite3.connect(DB_PATH)) as con:
        row = con.execute(
            "SELECT ts, body FROM snapshots WHERE snap_key=?", (key,)
        ).fetchone()
        prev = json.loads(row[1]) if row else None
        con.execute(
            "INSERT OR REPLACE INTO snapshots VALUES (?,?,?)",
            (key, time.time(), json.dumps(current)),
        )
        con.commit()
    if not prev:
        return None
    diff: dict[str, int] = {}
    for k in ("played", "victories", "kills", "deaths", "final_kills", "beds_destroyed", "xp"):
        cur_v = current.get(k)
        prev_v = prev.get(k)
        if isinstance(cur_v, int) and isinstance(prev_v, int) and cur_v > prev_v:
            diff[k] = cur_v - prev_v
    return diff or None


async def hive_get(
    session: aiohttp.ClientSession, path: str, ttl: int = CACHE_SEC
) -> Any:
    key = path.lower()
    now = time.time()
    saved = db_load(key)
    if saved and now - saved[0] < ttl:
        return saved[1]
    until = _blocked.get(key, 0)
    if now < until:
        if saved:
            return saved[1]
        raise HiveRateLimited(int(until - now))
    async with session.get(
        HIVE_API + path, timeout=aiohttp.ClientTimeout(total=12)
    ) as r:
        if r.status == 429:
            wait = int(r.headers.get("Retry-After", "60"))
            reset = r.headers.get("x-ratelimit-reset")
            _blocked[key] = float(reset) if reset else now + wait
            if saved:
                return saved[1]
            raise HiveRateLimited(wait)
        if r.status in (404, 422):
            return None
        r.raise_for_status()
        data = await r.json()
    db_save(key, now, data)
    return data


async def fetch_image_bytes(session: aiohttp.ClientSession, url: Optional[str]) -> Optional[bytes]:
    if not url:
        return None
    cache_key = f"img::{url}"
    saved = db_load(cache_key)
    if saved and time.time() - saved[0] < META_CACHE_SEC:
        return bytes.fromhex(saved[1])
    try:
        async with session.get(url, timeout=aiohttp.ClientTimeout(total=6)) as r:
            if r.status == 200:
                raw = await r.read()
                db_save(cache_key, time.time(), raw.hex())
                return raw
    except Exception:
        pass
    return None


async def _download_font(session: aiohttp.ClientSession, path: Path, url: str):
    if path.exists() and path.stat().st_size > 10_000:
        return
    try:
        async with session.get(url, timeout=aiohttp.ClientTimeout(total=30)) as r:
            if r.status == 200:
                path.write_bytes(await r.read())
    except Exception:
        pass


async def ensure_fonts():
    FONT_DIR.mkdir(parents=True, exist_ok=True)
    if not PIXEL_FONT_FILE.exists():
        print("[hive] fonts/PixelMplus12-Bold.ttf が見つからないため、代替フォントを使います")
        async with aiohttp.ClientSession(headers={"User-Agent": USER_AGENT}) as session:
            await _download_font(session, FONT_FALLBACK_PATH, FONT_FALLBACK_URL)
    _load_font.cache_clear()


async def ensure_default_map():
    """通知カードなど、マップ画像を持たないカードの背景用にSkyWarsのマップを1枚確保する。"""
    global _DEFAULT_MAP
    try:
        async with aiohttp.ClientSession(headers={"User-Agent": USER_AGENT}) as session:
            maps = await hive_get(session, PATH_MAPS.format(game="sky"), ttl=META_CACHE_SEC)
            url = maps[0].get("image") if isinstance(maps, list) and maps else None
            _DEFAULT_MAP = await fetch_image_bytes(session, url)
    except Exception:
        pass


async def _prepare_assets():
    await ensure_fonts()
    await ensure_default_map()


async def wait_assets():
    if _assets_task is not None:
        try:
            await asyncio.shield(_assets_task)
        except Exception:
            pass


async def get_bg_map_bytes(session: aiohttp.ClientSession, meta_key: Optional[str]) -> Optional[bytes]:
    """ゲームのマップ画像を背景用に取得（失敗してもNoneを返すだけ）。"""
    try:
        maps_list = await hive_get(
            session, PATH_MAPS.format(game=quote(meta_key or "sky", safe="")), ttl=META_CACHE_SEC
        )
        url = maps_list[0].get("image") if isinstance(maps_list, list) and maps_list else None
        return await fetch_image_bytes(session, url)
    except Exception:
        return None


# ---------- ゲーム定義 & ラベル ----------
GAMES: dict[str, dict[str, Any]] = {
    "sky": {"name": "SkyWars", "en": "SkyWars", "accent": (56, 189, 248), "meta": "sky"},
    "bed": {"name": "BedWars", "en": "BedWars", "accent": (244, 63, 94), "meta": "bed"},
    "wars": {"name": "Treasure Wars", "en": "Treasure Wars", "accent": (245, 158, 11), "meta": "wars"},
    "murder": {"name": "Murder Mystery", "en": "Murder Mystery", "accent": (239, 68, 68), "meta": "murder"},
    "hide": {"name": "Hide and Seek", "en": "Hide and Seek", "accent": (168, 85, 247), "meta": "hide"},
    "sg": {"name": "Survival Games", "en": "Survival Games", "accent": (34, 197, 94), "meta": "sg"},
    "dr": {"name": "DeathRun", "en": "DeathRun", "accent": (234, 179, 8), "meta": "dr"},
    "ctf": {"name": "Capture the Flag", "en": "Capture the Flag", "accent": (59, 130, 246), "meta": "ctf"},
    "bridge": {"name": "The Bridge", "en": "The Bridge", "accent": (249, 115, 22), "meta": "bridge"},
    "drop": {"name": "Block Drop", "en": "Block Drop", "accent": (14, 165, 233), "meta": "drop"},
    "ground": {"name": "Ground Wars", "en": "Ground Wars", "accent": (132, 204, 22), "meta": "ground"},
    "build": {"name": "Just Build", "en": "Just Build", "accent": (236, 72, 153), "meta": "build"},
    "party": {"name": "Block Party", "en": "Block Party", "accent": (217, 70, 239), "meta": "party"},
    "grav": {"name": "Gravity", "en": "Gravity", "accent": (99, 102, 241), "meta": "grav"},
    "sky-classic": {"name": "SkyWars Classic", "en": "SkyWars Classic", "accent": (56, 189, 248), "meta": "sky"},
    "sky-kits": {"name": "SkyWars Kits", "en": "SkyWars Kits", "accent": (56, 189, 248), "meta": "sky"},
    "parkour": {"name": "Parkour", "en": "Parkour", "accent": (20, 184, 166), "meta": None},
}

STAT_LABELS: dict[str, str] = {
    "xp": "XP",
    "uncapped_xp": "Uncapped XP",
    "prestige": "Prestige",
    "played": "Games Played",
    "victories": "Wins",
    "kills": "Kills",
    "deaths": "Deaths",
    "assists": "Assists",
    "final_kills": "Final Kills",
    "final_deaths": "Final Deaths",
    "mystery_chests_destroyed": "Mystery Chests",
    "ores_mined": "Ores Mined",
    "spells_used": "Spells Used",
    "selected_kit": "Selected Kit",
    "beds_destroyed": "Beds Destroyed",
    "treasure_destroyed": "Treasures Destroyed",
    "flags_captured": "Flags Captured",
    "flags_returned": "Flags Returned",
    "teams_eliminated": "Teams Eliminated",
    "coins": "Coins",
    "murders": "Murders",
    "murderer_eliminations": "Murderer Eliminations",
    "bux": "Bux",
    "hider_kills": "Hider Kills",
    "seeker_kills": "Seeker Kills",
    "taunts": "Taunts",
    "crates": "Crates",
    "deathmatches": "Deathmatches",
    "cows": "Cows",
    "teleporters_used": "Teleporters Used",
    "launchpads_used": "Launchpads Used",
    "flares_used": "Flares Used",
    "checkpoints": "Checkpoints",
    "activated": "Traps Activated",
    "blocks_destroyed": "Blocks Destroyed",
    "blocks_placed": "Blocks Placed",
    "powerups_collected": "Powerups Collected",
    "vaults_used": "Vaults Used",
    "projectiles_fired": "Projectiles Fired",
    "goals": "Goals",
    "rounds_survived": "Rounds Survived",
    "maps_completed": "Maps Completed",
    "maps_completed_without_dying": "Flawless Maps",
    "rating_love_received": "Love Ratings",
    "rating_great_received": "Great Ratings",
    "rating_good_received": "Good Ratings",
    "rating_okay_received": "Okay Ratings",
    "rating_meh_received": "Meh Ratings",
}

RANK_TAGS: dict[str, tuple[str, tuple[int, int, int]]] = {
    "OWNER": ("OWNER", (239, 68, 68)),
    "STAFF": ("STAFF", (239, 68, 68)),
    "MODERATOR": ("MOD", (249, 115, 22)),
    "HELPER": ("HELPER", (59, 130, 246)),
    "PARTNER": ("PARTNER", (168, 85, 247)),
    "YOUTUBER": ("YT", (239, 68, 68)),
    "HIVE": ("HIVE", (245, 158, 11)),
    "ULTIMATE": ("U", (216, 180, 254)),
    "PLUS": ("+", (56, 189, 248)),
}
RANK_HIDDEN = {"REGULAR", "DEFAULT", "PLAYER", "MEMBER", ""}

SKIP_KEYS = {"UUID", "first_played", "index", "human_index", "username"}
GREEN, RED, AMBER = (74, 222, 128), (248, 113, 113), (251, 191, 36)
SKY = (96, 165, 250)
PRESTIGE_COL = (232, 121, 249)
GOLD = (250, 204, 21)


def pick(d: Any, *names: str, default: Any = 0) -> Any:
    if isinstance(d, dict):
        for n in names:
            if d.get(n) is not None:
                return d[n]
    return default


def rank_tag(raw: Any) -> Optional[tuple[str, tuple[int, int, int]]]:
    if isinstance(raw, dict):
        raw = pick(raw, "name", "rank", "id", default="")
    text = str(raw or "").strip()
    if not text:
        return None
    key = text.upper().replace(" ", "_")
    if key in RANK_HIDDEN:
        return None
    return RANK_TAGS.get(key, (text.upper(), (148, 163, 184)))


def ratio(a: float, b: float) -> float:
    return a / b if b else float(a)


def fmt_num(v: Any) -> str:
    if isinstance(v, bool):
        return str(v)
    if isinstance(v, float):
        return f"{v:,.2f}".rstrip("0").rstrip(".")
    if isinstance(v, int):
        return f"{v:,}"
    return str(v)


def fmt_date(ts: Any) -> str:
    if not ts:
        return "記録なし"
    dt = datetime.datetime.fromtimestamp(int(ts), tz=datetime.timezone.utc).astimezone(JST)
    return dt.strftime("%Y年%m月%d日 %H:%M")


def prev_year_month() -> tuple[int, int, str]:
    now = datetime.datetime.now(JST)
    first_of_this_month = now.replace(day=1)
    last_month = first_of_this_month - datetime.timedelta(days=1)
    return last_month.year, last_month.month, f"{last_month.year}/{last_month.month:02d}"


def compute_level(
    xp: int, meta: Optional[dict[str, Any]], prestige: Optional[int] = None
) -> Optional[dict[str, Any]]:
    if not meta or "experienceToLevel" not in meta:
        return None
    table = sorted((int(req_xp), int(lv)) for req_xp, lv in meta["experienceToLevel"].items())
    if not table:
        return None
    max_lv = int(meta.get("maxLevel", table[-1][1]))
    max_req_xp = table[-1][0]
    allow_prestige = bool(meta.get("allowPrestiging")) and prestige is not None
    max_prestige = int(meta.get("maxPrestige", 0)) if allow_prestige else 0
    cur_prestige = int(prestige) if prestige is not None else 0

    cur_lv = 1
    cur_floor = 0
    next_floor = table[1][0] if len(table) > 1 else 150
    for i, (req_xp, lv) in enumerate(table):
        if xp >= req_xp:
            cur_lv = lv
            cur_floor = req_xp
            next_floor = table[i + 1][0] if i + 1 < len(table) else req_xp
        else:
            break
    is_max_lv = cur_lv >= max_lv
    in_lv = max(0, xp - cur_floor)
    need = max(1, next_floor - cur_floor) if not is_max_lv else 1
    can_prestige = allow_prestige and is_max_lv and cur_prestige < max_prestige
    is_true_max = (is_max_lv and cur_prestige >= max_prestige) if allow_prestige else is_max_lv
    total_xp = (cur_prestige * max_req_xp + xp) if allow_prestige else xp
    return {
        "level": cur_lv,
        "in_lv": in_lv,
        "need": need,
        "max_lv": max_lv,
        "is_max_lv": is_max_lv,
        "allow_prestige": allow_prestige,
        "prestige": cur_prestige,
        "max_prestige": max_prestige,
        "can_prestige": can_prestige,
        "is_true_max": is_true_max,
        "total_xp": total_xp,
    }


def _num(v: Any) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def build_stat_items(
    s: dict[str, Any], meta: Optional[dict[str, Any]] = None
) -> list[tuple[str, str, tuple[int, int, int], str]]:
    out: list[tuple[str, str, tuple[int, int, int], str]] = []
    played = s.get("played")
    wins = s.get("victories")
    kills = s.get("kills")
    deaths = s.get("deaths")
    fkills = s.get("final_kills")
    fdeaths = s.get("final_deaths")

    if _num(wins):
        out.append(("Wins", fmt_num(wins), GREEN, "trophy"))
    if _num(wins) and _num(played):
        losses = max(played - wins, 0)
        out.append(("Losses", fmt_num(losses), RED, "none"))
        if played > 0:
            out.append(("Win Rate", f"{wins / played * 100:.2f}%", AMBER, "none"))
    if _num(played):
        out.append(("Games Played", fmt_num(played), SKY, "none"))

    if _num(kills):
        out.append(("Kills", fmt_num(kills), GREEN, "sword"))
    if _num(deaths):
        out.append(("Deaths", fmt_num(deaths), RED, "skull"))
    if _num(kills) and _num(deaths):
        out.append(("KDR", fmt_num(round(ratio(kills, deaths), 2)), AMBER, "none"))
    if _num(fkills):
        out.append(("Final Kills", fmt_num(fkills), (167, 243, 208), "none"))
    if _num(fkills) and (_num(fdeaths) or (_num(wins) and _num(played))):
        denom = fdeaths if _num(fdeaths) else max(played - wins, 0)
        out.append(("FKLR", fmt_num(round(ratio(fkills, denom), 2)), (125, 211, 252), "none"))

    done = {
        "played", "victories", "kills", "deaths",
        "final_kills", "final_deaths", "xp", "prestige",
    } | SKIP_KEYS

    extra_palette = [
        (244, 180, 120), (125, 211, 252), (216, 180, 254),
        (163, 230, 53), (249, 168, 212), (94, 234, 212),
    ]
    idx = 0
    for k, v in s.items():
        if k in done:
            continue
        if not (_num(v) or isinstance(v, str)):
            continue
        label = STAT_LABELS.get(k, k.replace("_", " ").title())
        val = fmt_num(v) if _num(v) else str(v)
        icon = "none"
        out.append((label, val, extra_palette[idx % len(extra_palette)], icon))
        idx += 1

    return out[:14]


def build_analysis_text(s: dict[str, Any], diff: Optional[dict[str, int]]) -> Optional[str]:
    parts: list[str] = []
    played = s.get("played", 0)
    wins = s.get("victories", 0)
    kills = s.get("kills")
    if isinstance(played, int) and played > 0:
        avg_kills = f"・{kills / played:.2f}キル" if isinstance(kills, int) else ""
        cur_wr = wins / played * 100
        next_target = (int(cur_wr // 5) + 1) * 5
        advice = ""
        if 0 < next_target < 100:
            p_dec = next_target / 100.0
            needed = int(-(-((p_dec * played) - wins) // (1 - p_dec)))
            if 0 < needed <= 200:
                advice = f" ・ あと{needed}連勝で{next_target}%"
        parts.append(f"1戦平均 {s.get('xp', 0) / played:.1f}XP{avg_kills}{advice}")

    if diff:
        d_items = []
        if "victories" in diff:
            d_items.append(f"+{diff['victories']}勝")
        if "played" in diff:
            d_items.append(f"+{diff['played']}戦")
        if "kills" in diff:
            d_items.append(f"+{diff['kills']}キル")
        if "xp" in diff:
            d_items.append(f"+{diff['xp']:,}XP")
        if d_items:
            parts.append(f"前回から {' '.join(d_items)}")

    return "  |  ".join(parts) if parts else None


# ---------- フォント描画（英数字=Jersey 10 / 日本語=DotGothic16） ----------
class PixelFont:
    """ドットフォント。太字版の「W」は「M」とほぼ同じ形で読みにくいので、W/w だけ通常版の字形で描く。"""

    def __init__(self, font: Any, alt: Any = None, dot: int = 1):
        self.font = font
        self.latin = font  # getmetrics 用
        self.alt = alt
        self.dot = dot

    def segments(self, text: str):
        """(文字列, Wの特別描画かどうか) の並びにして返す。"""
        buf = ""
        for ch in text:
            if self.alt is not None and ch in "Ww":
                if buf:
                    yield buf, False
                    buf = ""
                yield ch, True
            else:
                buf += ch
        if buf:
            yield buf, False

    def seg_font(self, special: bool):
        return self.alt if special else self.font


class MixedDraw(ImageDraw.ImageDraw):
    """PixelFont を渡したときだけ専用の描き方をする ImageDraw。既存の d.text / d.textlength はそのまま使える。"""

    def textlength(self, text, font=None, *args, **kwargs):
        if isinstance(font, PixelFont):
            return sum(
                super(MixedDraw, self).textlength(seg, font=font.seg_font(sp))
                for seg, sp in font.segments(str(text))
            )
        return super().textlength(text, font=font, *args, **kwargs)

    def text(self, xy, text, fill=None, font=None, anchor=None, *args, **kwargs):
        if not isinstance(font, PixelFont):
            return super().text(xy, text, fill=fill, font=font, anchor=anchor, *args, **kwargs)
        text = str(text)
        x, y = xy
        anc = anchor or "la"
        total = self.textlength(text, font=font)
        if anc[0] == "m":
            x -= total / 2
        elif anc[0] == "r":
            x -= total
        try:
            asc, desc = font.latin.getmetrics()
        except Exception:
            asc, desc = 0, 0
        baseline = y + (asc - desc) / 2 if anc[1] == "m" else y + asc
        cx = x
        for seg, sp in font.segments(text):
            f = font.seg_font(sp)
            super().text((round(cx), round(baseline)), seg, fill=fill, font=f, anchor="ls")
            cx += super().textlength(seg, font=f)


def _snap_size(size: int) -> int:
    """希望サイズを、ドットがそろうサイズ(12/24/36)に丸める。"""
    if size <= 13:
        return 12
    for px in PIXEL_SIZES:
        if px >= size - 2:
            return px
    return PIXEL_SIZES[-1]


@lru_cache(maxsize=64)
def _load_font(size: int) -> PixelFont | ImageFont.ImageFont:
    if PIXEL_FONT_FILE.exists():
        try:
            px = _snap_size(size)
            f = ImageFont.truetype(str(PIXEL_FONT_FILE), px)
            alt = None
            if PIXEL_FONT_REGULAR.exists():
                try:
                    alt = ImageFont.truetype(str(PIXEL_FONT_REGULAR), px)
                except OSError:
                    alt = None
            return PixelFont(f, alt, max(1, px // 12))
        except OSError:
            pass
    if FONT_FALLBACK_PATH.exists():
        try:
            return PixelFont(ImageFont.truetype(str(FONT_FALLBACK_PATH), size))
        except OSError:
            pass
    return ImageFont.load_default()


# ---------- プレステージアイコン ----------
@lru_cache(maxsize=1)
def _prestige_sources() -> list[Image.Image]:
    return [
        Image.open(io.BytesIO(base64.b64decode(b))).convert("RGBA")
        for b in PRESTIGE_ICONS_B64
    ]


@lru_cache(maxsize=64)
def prestige_icon(p: int, height: int) -> Optional[Image.Image]:
    """プレステージ段階 p (1〜) のコイン画像。P0はNone。6以上は最上位(V)のコインを使う。"""
    if p <= 0:
        return None
    src = _prestige_sources()
    base = src[min(p, len(src)) - 1]
    w = max(1, round(base.width * height / base.height))
    return base.resize((w, height), Image.Resampling.NEAREST)


def prestige_width(d: ImageDraw.ImageDraw, p: int, ih: int, font: Any) -> int:
    ic = prestige_icon(p, ih)
    if not ic:
        return 0
    w = ic.width + 8
    if p > len(PRESTIGE_ICONS_B64):
        w += int(d.textlength(f"P{p}", font=font)) + 8
    return w


def draw_prestige(
    img: Image.Image, d: ImageDraw.ImageDraw, p: int, x: float, cy: float, ih: int, font: Any,
    col: tuple[int, int, int] = PRESTIGE_COL,
) -> int:
    """コインを描いて使った横幅を返す。6以上のときは "P6" のような数字を右に添える。"""
    ic = prestige_icon(p, ih)
    if not ic:
        return 0
    img.alpha_composite(ic, (int(x), int(cy - ic.height / 2)))
    w = ic.width + 8
    if p > len(PRESTIGE_ICONS_B64):
        txt = f"P{p}"
        d.text((x + w, cy), txt, font=font, fill=col, anchor="lm")
        w += int(d.textlength(txt, font=font)) + 8
    return w


def draw_icon_text(
    img: Image.Image, d: ImageDraw.ImageDraw, p: int, text: str, font: Any,
    fill: tuple[int, int, int], x: float, cy: float, align: str = "m", ih: int = 26,
):
    """[コイン] + テキスト をまとめて配置する。align: l / m / r。pが0ならテキストのみ。"""
    iw = prestige_width(d, p, ih, font)
    tw = int(d.textlength(text, font=font))
    total = iw + tw
    start = x - total / 2 if align == "m" else (x - total if align == "r" else x)
    if iw:
        draw_prestige(img, d, p, start, cy, ih, font)
    d.text((start + iw, cy), text, font=font, fill=fill, anchor="lm")


# ---------- 共通画像ユーティリティ ----------
PANEL_FILL = (22, 25, 33, 220)
PANEL_BORDER = (65, 72, 88, 180)
TXT_MAIN = (248, 250, 252)
TXT_MUTED = (186, 194, 208)


def _make_canvas_bg(w: int, h: int, map_bytes: Optional[bytes] = None) -> Image.Image:
    """マップ画像を拡大→ぼかし→暗幕。マップが無ければ共通背景、それも無ければ無地の暗色。"""
    global _DEFAULT_MAP
    if map_bytes and _DEFAULT_MAP is None:
        _DEFAULT_MAP = map_bytes
    src = map_bytes or _DEFAULT_MAP
    if src:
        try:
            bg = Image.open(io.BytesIO(src)).convert("RGB")
            scale = max(w / bg.width, h / bg.height)
            nw, nh = int(bg.width * scale) + 2, int(bg.height * scale) + 2
            bg = bg.resize((nw, nh), Image.Resampling.LANCZOS)
            left, top = (nw - w) // 2, (nh - h) // 2
            bg = bg.crop((left, top, left + w, top + h)).filter(ImageFilter.GaussianBlur(radius=7)).convert("RGBA")
            scrim = Image.new("RGBA", (w, h), (16, 18, 24, 150))
            return Image.alpha_composite(bg, scrim)
        except Exception:
            pass
    return Image.new("RGBA", (w, h), (26, 29, 36, 255))


def _fit_text(d: ImageDraw.ImageDraw, text: str, base_size: int, max_w: int, min_size: int = 12) -> tuple[str, Any]:
    sz = base_size
    f = _load_font(sz)
    while sz > min_size and d.textlength(text, font=f) > max_w:
        sz -= 1
        f = _load_font(sz)
    while len(text) > 1 and d.textlength(text, font=f) > max_w:
        text = text[:-2] + "…"
    return text, f


# ---------- ドット絵アイコン（20x20を2倍拡大。トロフィーと同じ「茶縁＋3段陰影」の画風） ----------
ICON_N = 20
ICON_PX = 40
_OUT = (134, 63, 5)  # 縁取りの茶色

_TROPHY_GRID = [
    "....................",
    "....kkkkkkkkkkkk....",
    "....koyccccyyyok....",
    "....koyccccyyyok....",
    "....koccccyyyyok....",
    ".kkkkooooooooookkkk.",
    ".kyyykddddddddkyykk.",
    ".kyydkooooooookdykk.",
    ".kyydkooooooookdykk.",
    ".koodkoyyyccookdokk.",
    "..kkokoyycccookok...",
    "....kkoccccyookk....",
    ".....kkcccyykkk.....",
    ".....kkcccyykkk.....",
    "......kkkookkk......",
    ".....nnnnnnnnnn.....",
    ".....nssswwsssn.....",
    ".....nnnnbbnnnn.....",
    ".....nnnnbbnnnn.....",
    "....................",
]
_TROPHY_PAL = {
    "k": _OUT, "o": (238, 157, 16), "y": (255, 229, 33), "n": (31, 41, 56),
    "c": (255, 247, 199), "d": (182, 96, 0), "s": (70, 91, 99), "b": (130, 157, 181), "w": (222, 240, 255),
}


def _draw_fit(d, xy, text: str, base: int, max_w: int, fill, anchor: str = "lm", min_size: int = 12):
    """はみ出す場合だけ文字サイズを下げて描く。"""
    t, f = _fit_text(d, text, base, max_w, min_size)
    d.text(xy, t, font=f, fill=fill, anchor=anchor)


_SWORD_B64 = "iVBORw0KGgoAAAANSUhEUgAAABAAAAAQCAYAAAAf8/9hAAABD0lEQVR42mNkIAPw2Zv9ZyAX8Nmb/V/y98P/JX8//OdQVfjPRKrmaft2MbSf2kO+zdrH18BtJ0uz8evTNNLMZ2/2Hz10+ezN/hu/Po1XM0ogsqvIw6OIz97sv+qaqQw/7jxk4FCRZ7hqFcrw4/YDRoLOlesq/8+hqvDf+PXp/3Jd5QT9zIhugIC3IwMDAwPD95v3GOQ6ygjazIjNFewq8gw/7zxk+PXsFUFno4SBp5no/yj+FwyfD51mYGBgYGCTEmMgFGUsyJodDIUY5m9/wvDj0VdGBgaG/zBDGBgY/uNyCQss1OX5XzDM3/6E4QZEM7LTCbuAXUWeYcGhVzCbUQChMGD59ewVw69nr8jOnQAPcZ16N2XJuwAAAABJRU5ErkJggg=="   # Kills: ダイヤの剣（16x16）
_SKULL_B64 = "iVBORw0KGgoAAAANSUhEUgAAACgAAAAoCAYAAACM/rhtAAAK5ElEQVR42tWYS4xU15nHf+ec+6q6XVVNGWMaGhgTY6yIBDFCtiLCxisvZhEpaiJvswDJs4oUMrOKhZWNQ6SsbdneogEjzWIkZMzIQjKKR8oYY3BwNzQ03Q3djaGqul637r3nkUV1lRu6m9hkNJq5q75X1ef8zv97ng/+jz/iaf/ROaeEEAbgnXfe8YFfOud+CyCEeAv44NixYznA6dOn1ZEjR8z/CuDqzd58802vWq2+HgTBrzzPO5DnOQCe5+Gcuwz88d69e6dOnDihnxZUPA3Y6dOnVbVafV1K+essy/YvLS2RZZndvHmzAKjVai7Pc4mDJEmuWGf/8Nlnn506c+aM+b6g4nsqJg8dOvQLKeXxKIoOaK3J89wYY4QQQiqlyPOcXtJjubFs6/W6E1KoKIpAcNk5d3JxcfHfTpw4Yb8rqHgS2MTEhBVCOOecuHjx4oQx5ngURQeNMSRJYpxzApBCfLtMnvUBpZT4gU+apbZerztrrVJKYYz5sxDi5NGjR88ADhCnT5+WG4GKJ4EBfPjhhz8P/OB4qVx6BaDT6Rida+H7vvQDHyEE1loApJQ45+glPbTWFIoFPM/DWkur1bK1Ws0555RSCmvtfznnTh47duzsStCJM2fOrAEV6wA7gE8++eRnaZL+Rmv9E601QggzWh0VjVpDdjodAJSnKBQLhGGItZYkSUh7Kc46hBCEUUihWEBKiZRyXdA8z/8kpfz9sWPH/v1xhnUV/Pjjj/9JSvkvQRD81DlHq9WyUki2jm2Vvu+jtWa5sczS/SW63S5SSkYro2R5RrvdHr6XK2W01lhrcc7hXB96ANpsNm29XkdKKYUQaK0/Nca8/cYbb/zHhgqeOnXq0/Hx8UNSSprNpgVQSg19rFgsUiwWCcOQNE25M3MHpRTFuIhzjnq9zsjICDt27EAIQaPR4JtvviGKIqSSOPst6CCg5ufnbZIklMtlaa1l5tbMpbd+99ZPB0zeasCFhYVDDx48sGNjY65SqSgpJb1ejzzPCcOQVqtFt9sFB+1WmyzNkGrlNyYnz3La7TZaa6IoYmFhAWMMQRBQKpUIw5DBYfM8Z2FhAa21DIKAubk5M3N7RnS73UOrmR4BzLLMJEmims0mo6OjVCoVAIwx+L5PuVymVqvR6XTwfZ9KuUKtXqPZbCKEoFqtsnnzZqSUaK3ZtGkTSZJgtaXb7pKlGcW4iJSSMAzZsWMHX331FdeuXqNWqykAJZXZEBBQK1WA5eVl7i/eJwxDtm3fRrVaRSlFqVRiZGSETZs2UalUGE/HmZmZoVAoMD4+jlKKVqtFq9UiDEOiKCJLM3q9HoVigSiKcM4xPz/P1atXmZubI+2l+L4/LKFPAgQg8H2MtWRp3/Hn787T7XUZ2zrGrl27KBQKGGNI05Qsy6hWqwzcwTlHkiSDzcjSjKSboJTCaMP8/DzXrl1jdnYW5xxKKnzfxzm3bj5+BFAIgbMW3/fx+xmYFm0Egka9wXJjmXq9zvPPP08YhnS7XbIsQwiBEIJer8fqpA1QKBSQSjJ7Z5bJqUkWFhZwzhEEQd99tNkQbg1gpVIhTdPh6UdGRhgplajVasMD3L17l8XFRSqVCmNjY5TLZZxzGGOGiRrA9/tJ/N69e1z+/DLzd+cRUqw25RoYay3W2ScrWCgUcM6t5DCH5ytw0Gl3qGyq4HkeeZ7TaDQYBNPWrVsplUo454ZJeWFhgatXr3Jj6gZZllEqlRBKDA8y2G8jsA19cHAyz/OGaUGI/sK+51OMi0PTGmOo1Wo0Gg1GR0cZGxuj1+vx5ZdfMnN7BmNNv9rIvs+arA8nw35ONNqQ63xDuA2DZAAqpSQIAgrFwtDxhRAUi0WiKKLX65GmKdZa6vU6tVqNyclJkiQhDEM830NrjfQlOu9XFaUUAoGxBp3rv9lurQEcFP+BXw2+hVGI8tQjZSuKIqQQ5HmONoaVmk0YhkPTSSmHpvR9v78GDoEAwRMDZA2gznPEShIdROUAcgD2uMpCCIIgQK8ovPr76nfP9x5pAxwObfSaIBnsN3jk48RJt8v58+eZnZ0liqKhIpVKP0AeV1sptS78+n4zaADEt5agb7E8z8nzfNi6raugMX2nvn37NpOTk+zevZsDBw7w3HPPEQTBGhAhBJ7vI5XCOsewDZMSx5PNJ0QfMtPZGqgNFaw9rNFpdYYpY3p6mrNnz3L79m2UUo8sPNh8YM44jimXywRBMLg0rQs18PEsy8iyR+GssWsCRz7qU5CmGS8ffJkf7fvRMHH3kgSd58P04nneGkWdcyilkELAyt9RodB3AfqHSLOUdrtNnuVYY9dN1E8MknanTbFQJI5jXtr7Env27GHyxhSe76OUQuc5WZaxtLSE53ns2rULYwx5ng+VRQiU5+ErhZSSPMvAQqvbot1qEwQBQSlYV92/aeJWq8X9b+4zdWOKdrtNHMe88vLLZFnG1NQUxlrCMOTBgwecPXuWc+fOsbS0RBAEQ/MppRArlWJQkbIsI0uzYR70PK8f1atcJl1pNJ6ooJSSLMv4y/W/MH1rmj179rB3716EENyZm2Pp/n22bds2zJFff/010zen2fvSXg4fPgzrqDDwUc/z2PzsZuJijDYaaeRKatN0Om2cc8TxyJMV3Dq21ZRKJTzl0Ww2+eKLL/joo4+YnZ3F97w+6J07aK3Zv38/cRyT5Rk3btwg6XYxWvcdeWA22Qdebi4zUhphy5YtKE9hrSVNU4w26DzHGMPKvYQk6W7csFZGK6pYLNrD4WF3/fp1de/ePR4+fEiz2aRQKFCtVimXyxQLRXb/w252bN/BrZlbPHz4sB+dg8ojBc46kk5CvVan1WrxzDPPYK2l1+vRaDQw2vSvRKJfYTzPN2naE1LKjRvWNEkvBWFwaOfOnWzZssXW63WuX78uZ2ZmaLVa9Ho9Hjx8QJZmlEtlyqUyP973Y9rtNknSo1CIcEDSSYbNq3V9v+ur0/9ure2ray1SKhtGHp5SyhiN1vrSIy3+6pcL/3nhg1dfffW/jTE7pZS7xsfHxYsvvmi3bNnisiwTjUaDLMtoLDdYXFzEWkscx8RxTNpL6XYTFhYWSJJkWIMH5TKKon5JNHrQYFjnnPM8TyqlhLHm0yzL/vnSpUv/uuG10zknBhOFt99++2elUuk3cRz/xPM8kiQxd+7cEVeuXJFzc3P9qqMU5XKZHzz/A3bu2Ik2mpvTN1mZ2fSvBHlGnuc8++yzxHFMq9my7Xbb+b6v+m5h/yQQvz93/tx3u7g/Pvp47733fu6cOx4EwSsD0JmZGXHlyhV59+5dcp0TRREH//EgcTHm1u1bdDqdftJ1oI3GD3zKpbKVUjqttUqSBOv6o4/z58+fHbBMTEzIwQTsOw2Pjhw5YgcDnvfff3/CGHM8DMODA9CbN2+Kzz//XDYaDfbt24eSirm5uWHbtdJL2kKh4KyxyjlH0kv+3Gq1Tl64cGE4PFoP7KnHb9u3b/+Fc+54sVg8oLVmaWnJLC4uCqONTJKEubk5lKeIi7ENo3BoyiRJLutMn/zhvh8Ox28TExNqI7C/a4A5MTGhXnvttdd7vd6v8zzfr6Si2+va5caymJ6exvM8F4ahBNBaXwH+kCTJcID5XcD+x0bAxWLxdeBXQRAcyLKMyclJjDE45y475/44Ozt76uLFi/r7gv3dQ/TVoEePHvVfeOGFX2qtfzs1NUWe52/FcfzBu+++mz8t2P+b569vfNaIJM8iyQAAAABJRU5ErkJggg=="   # Deaths: スケルトンの頭（40x40）


def _b64_image(b64: str) -> Image.Image:
    return Image.open(io.BytesIO(base64.b64decode(b64))).convert("RGBA")


def _build_sprites() -> dict[str, Image.Image]:
    sp: dict[str, Image.Image] = {}
    # Wins: トロフィー（20x20）
    im = Image.new("RGBA", (ICON_N, ICON_N), (0, 0, 0, 0))
    for y, row in enumerate(_TROPHY_GRID):
        for x, ch in enumerate(row):
            if ch in _TROPHY_PAL:
                im.putpixel((x, y), (*_TROPHY_PAL[ch], 255))
    sp["trophy"] = im.resize((ICON_N * 2, ICON_N * 2), Image.Resampling.NEAREST)   # 40x40（1ドット=2px）
    sp["sword"] = _b64_image(_SWORD_B64).resize((48, 48), Image.Resampling.NEAREST)  # 48x48（1ドット=3px）
    sp["skull"] = _b64_image(_SKULL_B64)                                              # 40x40
    return sp


_SPRITES = _build_sprites()


def _icon_w(kind: str) -> int:
    sp = _SPRITES.get(kind)
    return sp.width if sp is not None else 0


def _draw_mini_icon(img: Image.Image, kind: str, cx: int, cy: int):
    sp = _SPRITES.get(kind)
    if sp is None:
        return
    img.alpha_composite(sp, (int(cx - sp.width // 2), int(cy - sp.height // 2)))


def _paste_rounded(img: Image.Image, raw_bytes: Optional[bytes], x: int, y: int, w: int, h: int, radius: int = 10) -> bool:
    if not raw_bytes:
        return False
    try:
        sub = Image.open(io.BytesIO(raw_bytes)).convert("RGBA")
        scale = max(w / sub.width, h / sub.height)
        nw, nh = int(sub.width * scale) + 1, int(sub.height * scale) + 1
        sub = sub.resize((nw, nh), Image.Resampling.LANCZOS)
        left, top = max(0, (nw - w) // 2), max(0, (nh - h) // 2)
        sub = sub.crop((left, top, left + w, top + h))
        mask = Image.new("L", (w, h), 0)
        ImageDraw.Draw(mask).rounded_rectangle([0, 0, w, h], radius, fill=255)
        img.paste(sub, (x, y), mask)
        return True
    except Exception:
        return False


def _to_png(img: Image.Image) -> bytes:
    buf = io.BytesIO()
    img.convert("RGB").save(buf, "PNG", optimize=True)
    return buf.getvalue()


def _panel_drawer(layer: Image.Image):
    ld = ImageDraw.Draw(layer)

    def draw_panel(x: int, y: int, w: int, h: int, r: int = 12):
        ld.rounded_rectangle([x, y, x + w, y + h], radius=r, fill=PANEL_FILL, outline=PANEL_BORDER, width=1)

    return draw_panel



# ---------- マイクラ風 XPバー ----------
XP_BAR_W, XP_BAR_H = 182, 5  # マイクラ本家と同じドット数


@lru_cache(maxsize=256)
def _xp_bar_native(filled_px: int, magenta: bool) -> Image.Image:
    """182x5 のXPバー(+黒縁=184x7)をドット単位で作る。拡大はNEAREST。"""
    W, H = XP_BAR_W + 2, XP_BAR_H + 2
    im = Image.new("RGBA", (W, H), (0, 0, 0, 255))
    px = im.load()
    if magenta:
        fill_rows = [(245, 190, 255), (226, 110, 255), (226, 110, 255), (178, 70, 214), (120, 40, 150)]
    else:
        fill_rows = [(190, 255, 120), (128, 255, 32), (128, 255, 32), (96, 205, 26), (56, 140, 20)]
    empty_rows = [(74, 90, 76), (52, 66, 54), (46, 58, 48), (40, 51, 42), (32, 42, 34)]
    gap = (22, 30, 24)
    for x in range(XP_BAR_W):
        is_gap = x > 0 and (x * 18) % XP_BAR_W < 18  # 18分割の境目
        for y in range(XP_BAR_H):
            if x < filled_px:
                col = fill_rows[y] if not is_gap else tuple(int(c * 0.78) for c in fill_rows[y])
            else:
                col = gap if is_gap else empty_rows[y]
            px[x + 1, y + 1] = col + (255,)
    return im


def draw_xp_bar(img: Image.Image, cx: int, y: int, pct: float, scale: int = 4, magenta: bool = False) -> tuple[int, int, int]:
    """バーを中央cxに貼り、(左端x, 右端x, 高さ)を返す。pctは0〜1。"""
    filled = max(0, min(XP_BAR_W, int(pct * XP_BAR_W)))
    nat = _xp_bar_native(filled, magenta)
    big = nat.resize((nat.width * scale, nat.height * scale), Image.Resampling.NEAREST)
    x = cx - big.width // 2
    img.alpha_composite(big, (x, y))
    return x, x + big.width, big.height


# ---------- 1. ゲーム戦績カード ----------
def build_card(
    game_id: str,
    name: str,
    main: Optional[dict[str, Any]],
    s: dict[str, Any],
    meta: Optional[dict[str, Any]],
    period_label: str,
    avatar_bytes: Optional[bytes] = None,
    map_bytes: Optional[bytes] = None,
    monthly_rank: Optional[int] = None,
    analysis_note: Optional[str] = None,
) -> bytes:
    game_info = GAMES.get(game_id, {"name": game_id})
    stats = build_stat_items(s, meta)
    xp = int(pick(s, "xp", default=0))
    raw_prestige = s.get("prestige")
    prestige_opt = int(raw_prestige) if isinstance(raw_prestige, int) else None
    level_info = compute_level(xp, meta, prestige_opt)
    first_played = pick(s, "first_played", default=0) or pick(main, "first_played", default=0)
    rank = pick(main, "rank", default="")
    streak = pick(main, "daily_login_streak", default=None)

    rows_data: list[list[tuple[str, str, tuple[int, int, int], str]]] = []
    for i in range(0, len(stats), 4):
        rows_data.append(stats[i : i + 4])
    if len(rows_data) >= 2 and len(rows_data[-1]) == 1 and len(rows_data[-2]) == 4:
        moved = rows_data[-2].pop()
        rows_data[-1].insert(0, moved)

    W, M, G = 1040, 28, 12
    ch = 104
    top_y, top_h = 24, 62
    banner_y, banner_h = top_y + top_h + G, 68
    grid_y = banner_y + banner_h + G
    fp_y, fp_h = grid_y + len(rows_data) * (ch + G), 48
    pg_y = fp_y + fp_h + G
    pg_h = 118 if level_info else 0
    note_y = pg_y + (pg_h + G if pg_h else 0)
    note_h = 42 if analysis_note else 0
    ft_y = note_y + (note_h + G if note_h else 0)
    H = ft_y + 42

    img = _make_canvas_bg(W, H, map_bytes)
    layer = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    draw_panel = _panel_drawer(layer)

    period_w = 240
    draw_panel(M, top_y, period_w, top_h)
    draw_panel(M + period_w + G, top_y, W - 2 * M - period_w - G, top_h)
    draw_panel(M, banner_y, W - 2 * M, banner_h)

    for r_idx, row_items in enumerate(rows_data):
        cnt = len(row_items)
        cell_w = (W - 2 * M - (cnt - 1) * G) // cnt
        py = grid_y + r_idx * (ch + G)
        for c_idx in range(cnt):
            px = M + c_idx * (cell_w + G)
            w_adj = (W - M - px) if c_idx == cnt - 1 else cell_w
            draw_panel(px, py, w_adj, ch)

    draw_panel(M, fp_y, W - 2 * M, fp_h)
    if level_info:
        draw_panel(M, pg_y, W - 2 * M, pg_h)
    if analysis_note:
        draw_panel(M, note_y, W - 2 * M, note_h, r=10)
    draw_panel(M, ft_y, W - 2 * M, 34, r=10)

    img = Image.alpha_composite(img, layer)
    d = MixedDraw(img)

    f_period = _load_font(24)
    f_title = _load_font(28)
    f_banner = _load_font(28)
    f_label = _load_font(19)
    f_fp = _load_font(20)
    f_pg = _load_font(21)
    f_sm = _load_font(16)

    d.text((M + period_w // 2, top_y + top_h // 2), period_label, font=f_period, fill=(56, 189, 248), anchor="mm")
    title_cx = M + period_w + G + (W - 2 * M - period_w - G) // 2
    d.text((title_cx, top_y + top_h // 2), f"{game_info['name']}  Statistics", font=f_title, fill=TXT_MAIN, anchor="mm")

    # --- バナー: [コイン] Level N  名前 [RANK] [#月間順位] ---
    display_name = pick(main, "username_cc", default=name)
    banner_segs: list[tuple[str, Any, Optional[tuple[int, int, int]]]] = []  # ("t", 文字, 色) / ("i", 段階, None)
    if level_info:
        if level_info["allow_prestige"] and level_info["prestige"] > 0:
            banner_segs.append(("i", level_info["prestige"], None))
        banner_segs.append(("t", f"Level {level_info['level']}  ", GOLD))
    banner_segs.append(("t", display_name, TXT_MAIN))
    tag = rank_tag(rank)
    if tag:
        banner_segs.append(("t", f" [{tag[0]}]", tag[1]))
    if monthly_rank:
        banner_segs.append(("t", f"  [#{monthly_rank:,}]", RED))

    icon_h = 34

    def seg_width(kind: str, val: Any) -> int:
        if kind == "i":
            return prestige_width(d, val, icon_h, f_banner)
        return int(d.textlength(val, font=f_banner))

    total_text_w = sum(seg_width(k, v) for k, v, _ in banner_segs)
    av_sz = 42 if avatar_bytes else 0
    total_w = total_text_w + (av_sz + 14 if av_sz else 0)
    cur_x = int(W // 2 - total_w // 2)
    cy_banner = banner_y + banner_h // 2

    if avatar_bytes and _paste_rounded(img, avatar_bytes, cur_x, cy_banner - av_sz // 2, av_sz, av_sz, 8):
        cur_x += av_sz + 14

    for kind, val, col in banner_segs:
        if kind == "i":
            cur_x += draw_prestige(img, d, val, cur_x, cy_banner, icon_h, f_banner)
        else:
            d.text((cur_x, cy_banner), val, font=f_banner, fill=col, anchor="lm")
            cur_x += int(d.textlength(val, font=f_banner))

    # --- 統計セル ---
    for r_idx, row_items in enumerate(rows_data):
        cnt = len(row_items)
        cell_w = (W - 2 * M - (cnt - 1) * G) // cnt
        py = grid_y + r_idx * (ch + G)
        for c_idx, (lbl, val, col, icon) in enumerate(row_items):
            px = M + c_idx * (cell_w + G)
            w_adj = (W - M - px) if c_idx == cnt - 1 else cell_w
            cx = px + w_adj // 2
            lbl_fit, fl = _fit_text(d, lbl, 19, w_adj - 24, 13)
            d.text((cx, py + 30), lbl_fit, font=fl, fill=TXT_MUTED, anchor="mm")

            slot = _icon_w(icon) + 8 if icon in _SPRITES else 0
            val_fit, fv = _fit_text(d, val, 32, w_adj - 24 - slot, 15)
            if slot:
                vw = int(d.textlength(val_fit, font=fv))
                sx = cx - (vw + slot) // 2
                _draw_mini_icon(img, icon, sx + _icon_w(icon) // 2, py + 72)
                d.text((sx + slot, py + 72), val_fit, font=fv, fill=col, anchor="lm")
            else:
                d.text((cx, py + 72), val_fit, font=fv, fill=col, anchor="mm")

    d.text((W // 2, fp_y + fp_h // 2), f"初プレイ日: {fmt_date(first_played)}", font=f_fp, fill=(125, 211, 252), anchor="mm")

    # --- レベル / プレステージ進捗 ---
    if level_info:
        lv = level_info["level"]
        cur_xp = level_info["in_lv"]
        need_xp = level_info["need"]
        is_max_lv = level_info["is_max_lv"]
        allow_p = level_info["allow_prestige"]
        p_cur = level_info["prestige"]
        p_max = level_info["max_prestige"]
        can_p = level_info["can_prestige"]
        is_true_max = level_info["is_true_max"]

        pg_title = (
            f"レベル＆プレステージ進捗  ・  プレステージ {p_cur} / {p_max}"
            if allow_p else "レベル進捗"
        )
        draw_icon_text(img, d, p_cur if allow_p else 0, pg_title, f_pg, TXT_MAIN, W // 2, pg_y + 22, "m", 26)

        if can_p:
            xp_txt = f"プレステージ {p_cur + 1} へ昇格可能！"
            xp_col = PRESTIGE_COL
        elif is_true_max:
            xp_txt = "プレステージ・レベルとも最大！" if allow_p else "最大レベル！"
            xp_col = GOLD
        else:
            xp_txt = f"{int(cur_xp / need_xp * 100)}%"
            xp_col = GREEN
        xp_fit, f_xp = _fit_text(d, xp_txt, 19, W - 2 * M - 40, 13)
        d.text((W // 2, pg_y + 52), xp_fit, font=f_xp, fill=xp_col, anchor="mm")

        pct = 1.0 if is_max_lv else cur_xp / need_xp
        bar_scale = 4
        bar_h = (XP_BAR_H + 2) * bar_scale
        by = pg_y + 76
        bx, bx_end, _ = draw_xp_bar(img, W // 2, by, pct, bar_scale, magenta=can_p)
        blocks_w = bx_end - bx
        bh = bar_h

        lv_col = (216, 180, 254)
        by_c = by + bh // 2
        if allow_p:
            draw_icon_text(img, d, p_cur, f"Lv.{lv}", f_label, lv_col, bx - 16, by_c, "r", 24)
        else:
            d.text((bx - 16, by_c), f"Level {lv}", font=f_label, fill=lv_col, anchor="rm")
        if can_p:
            draw_icon_text(img, d, p_cur + 1, "昇格！", f_label, lv_col, bx_end + 16, by_c, "l", 24)
        elif is_true_max:
            d.text((bx_end + 16, by_c), "最大", font=f_label, fill=lv_col, anchor="lm")
        else:
            nxt = f"Lv.{lv + 1}" if allow_p else f"Level {lv + 1}"
            d.text((bx_end + 16, by_c), nxt, font=f_label, fill=lv_col, anchor="lm")

    if analysis_note:
        note_fit, f_note = _fit_text(d, analysis_note, 17, W - 2 * M - 32, 13)
        d.text((W // 2, note_y + note_h // 2), note_fit, font=f_note, fill=GOLD, anchor="mm")

    ft_parts = ["ぴーBOT"]
    if streak is not None:
        ft_parts.append(f"Login Streak: {streak}d")
    d.text((W // 2, ft_y + 17), "  ・  ".join(ft_parts), font=f_sm, fill=TXT_MUTED, anchor="mm")

    return _to_png(img)


# ---------- 2. 総合プロフィール画像 ----------
def build_overview_card(
    gamertag: str,
    allstats: dict[str, Any],
    main: dict[str, Any],
    avatar_bytes: Optional[bytes] = None,
    map_bytes: Optional[bytes] = None,
) -> bytes:
    display_name = pick(main, "username_cc", default=gamertag)
    rank_info = rank_tag(pick(main, "rank", default=""))
    paid_ranks = "、".join(main.get("paid_ranks") or []) or "なし"
    first_played = fmt_date(pick(main, "first_played", default=0))
    player_no = fmt_num(pick(main, "player_number", default=0))

    played_games: list[tuple[str, int, int, int, int, tuple[int, int, int]]] = []
    for gid, ginfo in GAMES.items():
        gs = allstats.get(gid)
        if isinstance(gs, dict) and (gs.get("played", 0) > 0 or gs.get("xp", 0) > 0):
            played_games.append((
                ginfo["name"],
                int(gs.get("victories", 0)),
                int(gs.get("played", 0)),
                int(gs.get("xp", 0)),
                int(gs.get("prestige") or 0),
                ginfo["accent"],
            ))
    played_games.sort(key=lambda x: x[2], reverse=True)
    shown_games = played_games[:12]

    W, M, G = 1040, 28, 12
    top_y, top_h = 24, 96
    info_y, info_h = top_y + top_h + G, 124
    games_rows = max(1, -(-len(shown_games) // 3))
    games_y = info_y + info_h + G
    game_ch = 78
    ft_y = games_y + 38 + games_rows * (game_ch + 10) + G
    H = ft_y + 42

    img = _make_canvas_bg(W, H, map_bytes)
    layer = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    draw_panel = _panel_drawer(layer)

    draw_panel(M, top_y, W - 2 * M, top_h)
    half_w = (W - 2 * M - G) // 2
    draw_panel(M, info_y, half_w, info_h)
    draw_panel(M + half_w + G, info_y, W - 2 * M - half_w - G, info_h)
    draw_panel(M, games_y, W - 2 * M, 38 + games_rows * (game_ch + 10))
    draw_panel(M, ft_y, W - 2 * M, 34, r=10)

    img = Image.alpha_composite(img, layer)
    d = MixedDraw(img)

    f_name = _load_font(30)
    f_sub = _load_font(17)
    f_sec = _load_font(18)
    f_sm = _load_font(16)

    av_x, av_y, av_sz = M + 18, top_y + 14, 68
    if not _paste_rounded(img, avatar_bytes, av_x, av_y, av_sz, av_sz, 12):
        d.rounded_rectangle([av_x, av_y, av_x + av_sz, av_y + av_sz], 12, fill=(45, 52, 68))
        d.text((av_x + av_sz // 2, av_y + av_sz // 2), display_name[:2].upper(), font=f_name, fill=AMBER, anchor="mm")

    tx = av_x + av_sz + 18
    title_str = f"{display_name} - Overview"
    if rank_info:
        title_str += f"  [{rank_info[0]}]"
    t_fit, ft_f = _fit_text(d, title_str, 28, W - M - tx - 20, 18)
    d.text((tx, top_y + 32), t_fit, font=ft_f, fill=TXT_MAIN, anchor="lm")
    _draw_fit(d, (tx, top_y + 68), f"Player #{player_no}  ・  初プレイ日: {first_played}  ・  Paid Ranks: {paid_ranks}", 20, W - M - tx - 20, TXT_MUTED)

    d.text((M + 18, info_y + 24), "Login & Friends", font=f_sec, fill=AMBER, anchor="lm")
    _draw_fit(
        d, (M + 18, info_y + 62),
        f"Streak: {main.get('daily_login_streak', 0)}d (Best: {main.get('longest_daily_login_streak', 0)}d)",
        24, half_w - 36, TXT_MAIN,
    )
    _draw_fit(d, (M + 18, info_y + 94), f"Friends: {fmt_num(main.get('friend_count', 0))}", 24, half_w - 36, TXT_MAIN)

    rx = M + half_w + G + 18
    d.text((rx, info_y + 24), "Cosmetics", font=f_sec, fill=(56, 189, 248), anchor="lm")
    _draw_fit(
        d, (rx, info_y + 62),
        f"Titles {main.get('hub_title_count', 0)} ・ Avatars {main.get('avatar_count', 0)} ・ Costumes {main.get('costume_count', 0)}",
        24, half_w - 36, TXT_MAIN,
    )
    _draw_fit(
        d, (rx, info_y + 94),
        f"Hats {main.get('hat_count', 0)} ・ Pets {len(main.get('pets') or [])}",
        24, half_w - 36, TXT_MAIN,
    )

    d.text((M + 18, games_y + 22), f"Games Summary ({len(played_games)} games played)", font=f_sec, fill=TXT_MAIN, anchor="lm")
    if not shown_games:
        d.text((W // 2, games_y + 75), "まだプレイ記録がありません", font=f_sec, fill=TXT_MUTED, anchor="mm")
    else:
        gw = (W - 2 * M - 36 - 2 * 10) // 3
        for idx, (gname, wins, played, xp, prest, acc) in enumerate(shown_games):
            r, c = divmod(idx, 3)
            gx = M + 18 + c * (gw + 10)
            gy = games_y + 42 + r * (game_ch + 10)
            d.rounded_rectangle([gx, gy, gx + gw, gy + game_ch], 10, fill=(32, 36, 46, 220), outline=PANEL_BORDER, width=1)
            d.rectangle([gx, gy + 12, gx + 4, gy + game_ch - 12], fill=acc)

            # プレステージコインはカード右上に表示（6以上は下に P数字）
            pic = prestige_icon(prest, 28)
            ptxt = f"P{prest}" if prest > len(PRESTIGE_ICONS_B64) else ""
            ptxt_w = (int(d.textlength(ptxt, font=f_sm)) + 8) if ptxt else 0
            name_max = gw - 28 - ((pic.width + 12 + ptxt_w) if pic else 0)
            gn_fit, fg = _fit_text(d, gname, 19, name_max, 13)
            d.text((gx + 16, gy + 24), gn_fit, font=fg, fill=TXT_MAIN, anchor="lm")
            if pic:
                img.alpha_composite(pic, (gx + gw - 12 - pic.width, gy + 24 - pic.height // 2))
                if ptxt:
                    d.text((gx + gw - 12 - pic.width - 8, gy + 24), ptxt, font=f_sm, fill=PRESTIGE_COL, anchor="rm")

            wr = f" ({wins / played * 100:.1f}%)" if played else ""
            _draw_fit(d, (gx + 16, gy + 54), f"{wins:,} W / {played:,} P{wr}", 24, gw - 28, GREEN)

    d.text((W // 2, ft_y + 17), "ぴーBOT  ・  下のメニューから各ゲームの詳細カードを開けます", font=f_sm, fill=TXT_MUTED, anchor="mm")
    return _to_png(img)


# ---------- 3. 持ち物・コスメティック画像 ----------
def build_cosmetics_card(
    gamertag: str,
    main: dict[str, Any],
    avatar_bytes: Optional[bytes] = None,
    map_bytes: Optional[bytes] = None,
) -> bytes:
    display_name = pick(main, "username_cc", default=gamertag)
    av_name = pick(main, "equipped_avatar", default={}).get("name", "未設定") if isinstance(main.get("equipped_avatar"), dict) else "未設定"

    hats = [f"{h.get('name')} ({h.get('rarity', '-')})" for h in (main.get("hat_unlocked") or [])]
    backs = [f"{b.get('name')} ({b.get('rarity', '-')})" for b in (main.get("cosmetics.backbling") or [])]
    costumes = main.get("costume_unlocked") or []
    titles = main.get("hub_title_unlocked") or []
    pets = main.get("pets") or []
    mounts = main.get("mounts") or []

    sections = [
        (f"Hats ({len(hats)})", ", ".join(hats[:8]) + (" ..." if len(hats) > 8 else "") or "None"),
        (f"Backbling ({len(backs)})", ", ".join(backs[:8]) + (" ..." if len(backs) > 8 else "") or "None"),
        (f"Costumes ({len(costumes)})", ", ".join(costumes[:12]) + (" ..." if len(costumes) > 12 else "") or "None"),
        (f"Pets & Mounts (Pets: {len(pets)} / Mounts: {len(mounts)})", f"Pets: {', '.join(pets[:8]) or 'None'}  /  Mounts: {', '.join(mounts[:8]) or 'None'}"),
        (f"Hub Titles ({main.get('hub_title_count', len(titles))})", ", ".join(titles[:12]) + (" ..." if len(titles) > 12 else "") or "None"),
    ]

    W, M, G = 1040, 28, 12
    top_y, top_h = 24, 92
    sec_y = top_y + top_h + G
    sec_h = 84
    ft_y = sec_y + len(sections) * (sec_h + 10) + 4
    H = ft_y + 42

    img = _make_canvas_bg(W, H, map_bytes)
    layer = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    draw_panel = _panel_drawer(layer)

    draw_panel(M, top_y, W - 2 * M, top_h)
    for i in range(len(sections)):
        draw_panel(M, sec_y + i * (sec_h + 10), W - 2 * M, sec_h)
    draw_panel(M, ft_y, W - 2 * M, 34, r=10)

    img = Image.alpha_composite(img, layer)
    d = MixedDraw(img)

    f_title = _load_font(28)
    f_sec = _load_font(19)
    f_sm = _load_font(16)

    av_x, av_y, av_sz = M + 18, top_y + 14, 64
    if not _paste_rounded(img, avatar_bytes, av_x, av_y, av_sz, av_sz, 10):
        d.rounded_rectangle([av_x, av_y, av_x + av_sz, av_y + av_sz], 10, fill=(45, 52, 68))

    tx = av_x + av_sz + 18
    d.text((tx, top_y + 32), f"{display_name} - Cosmetics", font=f_title, fill=TXT_MAIN, anchor="lm")
    _draw_fit(d, (tx, top_y + 66), f"Equipped Avatar: {av_name}  ・  Unlocked Avatars: {main.get('avatar_count', 0)}", 24, W - M - tx - 20, TXT_MUTED)

    for i, (heading, body) in enumerate(sections):
        sy = sec_y + i * (sec_h + 10)
        d.text((M + 20, sy + 24), heading, font=f_sec, fill=AMBER, anchor="lm")
        body_fit, fb = _fit_text(d, body, 17, W - 2 * M - 40, 12)
        d.text((M + 20, sy + 56), body_fit, font=fb, fill=TXT_MAIN, anchor="lm")

    d.text((W // 2, ft_y + 17), "ぴーBOT  ・  The Hive Cosmetics", font=f_sm, fill=TXT_MUTED, anchor="mm")
    return _to_png(img)


# ---------- 4. マップ一覧画像（マップサムネイル付き） ----------
def build_maps_card(
    game_id: str,
    maps: list[dict[str, Any]],
    thumb_bytes_list: list[Optional[bytes]],
) -> bytes:
    ginfo = GAMES.get(game_id, {"name": game_id})
    shown = maps[:12]
    cols = 3
    rows = max(1, -(-len(shown) // cols))

    W, M, G = 1040, 28, 12
    top_y, top_h = 24, 66
    grid_y = top_y + top_h + G
    cw = (W - 2 * M - (cols - 1) * G) // cols
    ch = 166
    ft_y = grid_y + (rows * (ch + G) if shown else 120)
    H = ft_y + 42

    bg_map = next((b for b in thumb_bytes_list if b), None)
    img = _make_canvas_bg(W, H, bg_map)
    layer = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    draw_panel = _panel_drawer(layer)

    draw_panel(M, top_y, W - 2 * M, top_h)
    for i in range(len(shown)):
        r, c = divmod(i, cols)
        draw_panel(M + c * (cw + G), grid_y + r * (ch + G), cw, ch)
    if not shown:
        draw_panel(M, grid_y, W - 2 * M, 108)
    draw_panel(M, ft_y, W - 2 * M, 34, r=10)

    img = Image.alpha_composite(img, layer)
    d = MixedDraw(img)

    f_title = _load_font(26)
    f_sm = _load_font(15)
    d.text((W // 2, top_y + top_h // 2), f"{ginfo['name']}  Maps ({len(maps)})", font=f_title, fill=TXT_MAIN, anchor="mm")

    if not shown:
        d.text((W // 2, grid_y + 54), "公開されているマップ情報がありません", font=_load_font(20), fill=TXT_MUTED, anchor="mm")
    else:
        for i, m in enumerate(shown):
            r, c = divmod(i, cols)
            px = M + c * (cw + G)
            py = grid_y + r * (ch + G)
            tb = thumb_bytes_list[i] if i < len(thumb_bytes_list) else None
            if not _paste_rounded(img, tb, px + 8, py + 8, cw - 16, 104, 8):
                d.rounded_rectangle([px + 8, py + 8, px + cw - 8, py + 112], 8, fill=(40, 46, 58))
            mname, fn = _fit_text(d, str(m.get("name", "Unknown")), 18, cw - 20, 13)
            d.text((px + 12, py + 126), mname, font=fn, fill=TXT_MAIN, anchor="lm")
            meta_txt = f"{m.get('variant', 'Standard')} ・ {m.get('season', '-')}"
            mmeta, fm = _fit_text(d, meta_txt, 14, cw - 20, 11)
            d.text((px + 12, py + 149), mmeta, font=fm, fill=TXT_MUTED, anchor="lm")

    d.text((W // 2, ft_y + 17), "ぴーBOT  ・  Showing top 12 maps", font=f_sm, fill=TXT_MUTED, anchor="mm")
    return _to_png(img)


# ---------- 5. ランキング画像 ----------
def build_leaderboard_card(
    game_id: str,
    period: str,
    entries: list[dict[str, Any]],
    page: int,
    per_page: int = 10,
    map_bytes: Optional[bytes] = None,
) -> bytes:
    ginfo = GAMES.get(game_id, {"name": game_id})
    total_pages = max(1, -(-len(entries) // per_page))
    chunk = entries[page * per_page : (page + 1) * per_page]

    W, M, G = 1040, 28, 12
    top_y, top_h = 24, 66
    list_y = top_y + top_h + G
    row_h = 52
    ft_y = list_y + max(1, len(chunk)) * row_h + G
    H = ft_y + 42

    img = _make_canvas_bg(W, H, map_bytes)
    layer = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    ld = ImageDraw.Draw(layer)

    ld.rounded_rectangle([M, top_y, W - M, top_y + top_h], 12, fill=PANEL_FILL, outline=PANEL_BORDER, width=1)
    for i in range(max(1, len(chunk))):
        ry = list_y + i * row_h
        ld.rounded_rectangle([M, ry, W - M, ry + row_h - 6], 10, fill=PANEL_FILL, outline=PANEL_BORDER, width=1)
    ld.rounded_rectangle([M, ft_y, W - M, ft_y + 34], 10, fill=PANEL_FILL, outline=PANEL_BORDER, width=1)

    img = Image.alpha_composite(img, layer)
    d = MixedDraw(img)

    f_title = _load_font(26)
    f_row = _load_font(20)
    f_sm = _load_font(16)

    p_label = "All-Time Top 100" if period == "all" else "Monthly Top 100"
    d.text((W // 2, top_y + top_h // 2), f"{ginfo['name']}  {p_label}", font=f_title, fill=TXT_MAIN, anchor="mm")

    if not chunk:
        d.text((W // 2, list_y + 22), "ランキングデータがありません", font=f_row, fill=TXT_MUTED, anchor="mm")
    else:
        for i, e in enumerate(chunk):
            ry = list_y + i * row_h + (row_h - 6) // 2
            rank = int(e.get("human_index", e.get("index", 0) + 1))
            r_col = (250, 204, 21) if rank == 1 else ((226, 232, 240) if rank == 2 else ((251, 146, 60) if rank == 3 else (96, 165, 250)))
            d.text((M + 26, ry), f"#{rank}", font=f_row, fill=r_col, anchor="lm")

            uname, fn = _fit_text(d, str(e.get("username", "Unknown")), 21, 320, 14)
            d.text((M + 110, ry), uname, font=fn, fill=TXT_MAIN, anchor="lm")

            wins = int(e.get("victories", 0))
            played = int(e.get("played", 0))
            xp = int(e.get("xp", 0))
            wr = f"({wins / played * 100:.1f}%)" if played else ""
            d.text((W - M - 260, ry), f"{wins:,} W / {played:,} P {wr}", font=f_sm, fill=GREEN, anchor="rm")
            d.text((W - M - 24, ry), f"{xp:,} XP", font=f_row, fill=AMBER, anchor="rm")

    d.text((W // 2, ft_y + 17), f"ぴーBOT  ・  Page {page + 1} / {total_pages}", font=f_sm, fill=TXT_MUTED, anchor="mm")
    return _to_png(img)


# ---------- 6. 全体統計画像 ----------
def build_global_card(up: dict[str, int]) -> bytes:
    total = up.get("global", up.get("main", 0))
    rows = sorted(
        [(gid, cnt) for gid, cnt in up.items() if gid not in ("global", "main")],
        key=lambda kv: kv[1],
        reverse=True,
    )

    W, M, G = 1040, 28, 12
    top_y, top_h = 24, 92
    list_y = top_y + top_h + G
    row_h = 46
    list_h = len(rows) * row_h + 12
    ft_y = list_y + list_h + G
    H = ft_y + 42

    img = _make_canvas_bg(W, H)
    layer = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    draw_panel = _panel_drawer(layer)

    draw_panel(M, top_y, W - 2 * M, top_h)
    draw_panel(M, list_y, W - 2 * M, list_h)
    draw_panel(M, ft_y, W - 2 * M, 34, r=10)

    img = Image.alpha_composite(img, layer)
    d = MixedDraw(img)

    f_title = _load_font(26)
    f_big = _load_font(32)
    f_row = _load_font(17)
    f_sm = _load_font(15)

    d.text((W // 2, top_y + 28), "The Hive - Global Unique Players", font=f_title, fill=TXT_MUTED, anchor="mm")
    d.text((W // 2, top_y + 66), f"{total:,}", font=f_big, fill=AMBER, anchor="mm")

    bar_x = M + 280
    bar_w = W - M - 220 - bar_x
    for i, (gid, cnt) in enumerate(rows):
        ry = list_y + 24 + i * row_h
        info = GAMES.get(gid, {"name": gid, "accent": (245, 158, 11)})
        pct = (cnt / total * 100) if total else 0
        gname, fg = _fit_text(d, info["name"], 17, 240, 12)
        d.text((M + 20, ry), gname, font=fg, fill=TXT_MAIN, anchor="lm")

        d.rounded_rectangle([bar_x, ry - 7, bar_x + bar_w, ry + 7], 7, fill=(45, 52, 68))
        fw = max(12, int(bar_w * (cnt / total))) if total else 12
        d.rounded_rectangle([bar_x, ry - 7, bar_x + fw, ry + 7], 7, fill=info["accent"])

        d.text((W - M - 20, ry), f"{cnt:,} ({pct:.1f}%)", font=f_row, fill=TXT_MAIN, anchor="rm")

    d.text((W // 2, ft_y + 17), "ぴーBOT  ・  Unique Players by Game", font=f_sm, fill=TXT_MUTED, anchor="mm")
    return _to_png(img)


# ---------- 7. 2人比較画像 ----------
def build_compare_card(
    game_id: str,
    p1_name: str,
    p1_s: dict[str, Any],
    p2_name: str,
    p2_s: dict[str, Any],
) -> bytes:
    game_info = GAMES.get(game_id, {"name": game_id})
    metrics = [
        ("Wins", "victories"),
        ("Games Played", "played"),
        ("Win Rate", "_winrate"),
        ("Kills", "kills"),
        ("Deaths", "deaths"),
        ("KDR", "_kd"),
        ("Final Kills", "final_kills"),
        ("Prestige", "prestige"),
        ("XP", "xp"),
    ]

    def get_val(s: dict[str, Any], key: str) -> Optional[float]:
        if key == "_winrate":
            p, w = s.get("played", 0), s.get("victories", 0)
            return round(w / p * 100, 2) if p else 0.0
        if key == "_kd":
            k, d_ = s.get("kills", 0), s.get("deaths", 0)
            return round(ratio(k, d_), 2)
        v = s.get(key)
        return float(v) if _num(v) else None

    rows = []
    for lbl, key in metrics:
        v1, v2 = get_val(p1_s, key), get_val(p2_s, key)
        if v1 is not None or v2 is not None:
            rows.append((lbl, key, v1 or 0.0, v2 or 0.0))

    W, M, G = 980, 28, 12
    top_y, top_h = 24, 100
    list_y = top_y + top_h + G
    row_h = 54
    ft_y = list_y + len(rows) * row_h + G
    H = ft_y + 42

    img = _make_canvas_bg(W, H)
    layer = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    draw_panel = _panel_drawer(layer)
    draw_panel(M, top_y, W - 2 * M, top_h)
    for i in range(len(rows)):
        draw_panel(M, list_y + i * row_h, W - 2 * M, row_h - 6, r=10)
    draw_panel(M, ft_y, W - 2 * M, 34, r=10)

    img = Image.alpha_composite(img, layer)
    d = MixedDraw(img)

    f_title = _load_font(25)
    f_player = _load_font(28)
    f_row = _load_font(22)
    f_sm = _load_font(17)

    d.text((W // 2, top_y + 26), f"{game_info['name']}  Compare", font=f_title, fill=TXT_MAIN, anchor="mm")
    p1_fit, fp1 = _fit_text(d, p1_name, 28, W // 2 - M - 90, 16)
    p2_fit, fp2 = _fit_text(d, p2_name, 28, W // 2 - M - 90, 16)
    d.text((M + 20, top_y + 68), p1_fit, font=fp1, fill=(96, 165, 250), anchor="lm")
    d.text((W // 2, top_y + 68), "VS", font=f_row, fill=(148, 163, 184), anchor="mm")
    d.text((W - M - 20, top_y + 68), p2_fit, font=fp2, fill=(251, 146, 60), anchor="rm")

    for idx, (lbl, key, v1, v2) in enumerate(rows):
        cy = list_y + idx * row_h + (row_h - 6) // 2
        s1 = f"{v1:.2f}%" if key == "_winrate" else fmt_num(int(v1) if float(v1).is_integer() else v1)
        s2 = f"{v2:.2f}%" if key == "_winrate" else fmt_num(int(v2) if float(v2).is_integer() else v2)
        c1 = GREEN if (v1 > v2 and key != "deaths") or (v1 < v2 and key == "deaths") else TXT_MAIN
        c2 = GREEN if (v2 > v1 and key != "deaths") or (v2 < v1 and key == "deaths") else TXT_MAIN
        d.text((M + 24, cy), s1, font=f_row, fill=c1, anchor="lm")
        d.text((W // 2, cy), lbl, font=f_sm, fill=TXT_MUTED, anchor="mm")
        d.text((W - M - 24, cy), s2, font=f_row, fill=c2, anchor="rm")

    d.text((W // 2, ft_y + 17), "ぴーBOT", font=f_sm, fill=TXT_MUTED, anchor="mm")
    return _to_png(img)


# ---------- 8. お知らせ・データなし画像 ----------
def build_notice_card(title: str, message: str, is_error: bool = False) -> bytes:
    W, H = 880, 220
    img = _make_canvas_bg(W, H)
    layer = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    ImageDraw.Draw(layer).rounded_rectangle([24, 24, W - 24, H - 24], 14, fill=PANEL_FILL, outline=PANEL_BORDER, width=1)
    img = Image.alpha_composite(img, layer)
    d = MixedDraw(img)

    t_fit, ft = _fit_text(d, title, 26, W - 96, 16)
    m_fit, fm = _fit_text(d, message, 20, W - 96, 13)
    d.text((W // 2, 76), t_fit, font=ft, fill=RED if is_error else AMBER, anchor="mm")
    d.text((W // 2, 128), m_fit, font=fm, fill=TXT_MAIN, anchor="mm")
    d.text((W // 2, 174), "ぴーBOT", font=_load_font(15), fill=TXT_MUTED, anchor="mm")
    return _to_png(img)


# ---------- 対話型 Discord UI View（すべて画像出力・誰でも操作可能） ----------
class HiveDashboardView(discord.ui.View):
    def __init__(
        self,
        gamertag: str,
        game_id: str,
        period: str,
        view_mode: str,
        allstats: dict[str, Any],
        main: dict[str, Any],
    ):
        super().__init__(timeout=300)
        self.gamertag = gamertag
        self.game_id = game_id
        self.period = period
        self.view_mode = view_mode  # "card" | "cosmetics" | "maps"
        self.allstats = allstats
        self.main = main

        options = [
            discord.SelectOption(
                label="Overview（総合プロフィール）",
                value="overview",
                description="Rank・ログイン日数・各ゲームの戦績一覧",
                default=(self.game_id == "overview"),
            )
        ]
        for gid, ginfo in GAMES.items():
            gs = allstats.get(gid)
            has_data = isinstance(gs, dict) and (gs.get("played", 0) > 0 or gs.get("xp", 0) > 0)
            p_tag = f"[P{gs.get('prestige')}] " if isinstance(gs, dict) and (gs.get("prestige") or 0) > 0 else ""
            desc = (
                f"{p_tag}{fmt_num(gs.get('victories', 0))} W / {fmt_num(gs.get('played', 0))} P"
                if has_data else "プレイ記録なし"
            )
            options.append(
                discord.SelectOption(
                    label=ginfo["name"],
                    value=gid,
                    description=desc[:90],
                    default=(self.game_id == gid),
                )
            )
        select = discord.ui.Select(
            placeholder="見たいゲームやプロフィールを選ぶ...",
            options=options[:25],
            row=0,
        )
        select.callback = self.on_game_select
        self.add_item(select)

    async def _render_png(self) -> tuple[bytes, str]:
        await wait_assets()
        av_url = pick(self.main, "equipped_avatar", default={}).get("url") if isinstance(self.main.get("equipped_avatar"), dict) else None
        meta_key = GAMES.get(self.game_id, {}).get("meta") if self.game_id != "overview" else "sky"
        q_name = quote(self.gamertag, safe="")
        py, pm, prev_lbl = prev_year_month()

        async with aiohttp.ClientSession(headers={"User-Agent": USER_AGENT}) as session:
            if self.view_mode == "maps":
                m_key = meta_key or self.game_id
                maps = await hive_get(session, PATH_MAPS.format(game=quote(m_key, safe="")), ttl=META_CACHE_SEC) or []
                thumbs = await asyncio.gather(*(fetch_image_bytes(session, m.get("image")) for m in maps[:12]))
                png = await asyncio.to_thread(build_maps_card, self.game_id, maps, list(thumbs))
                return png, "hive_maps.png"

            if self.view_mode == "cosmetics":
                av_bytes, map_bytes = await asyncio.gather(
                    fetch_image_bytes(session, av_url), get_bg_map_bytes(session, "sky")
                )
                png = await asyncio.to_thread(build_cosmetics_card, self.gamertag, self.main, av_bytes, map_bytes)
                return png, "hive_cosmetics.png"

            if self.game_id == "overview":
                av_bytes, map_bytes = await asyncio.gather(
                    fetch_image_bytes(session, av_url), get_bg_map_bytes(session, "sky")
                )
                png = await asyncio.to_thread(build_overview_card, self.gamertag, self.allstats, self.main, av_bytes, map_bytes)
                return png, "hive_overview.png"

            meta = await hive_get(session, PATH_META.format(game=quote(meta_key, safe="")), ttl=META_CACHE_SEC) if meta_key else None
            monthly_data = None
            if self.period == "monthly":
                monthly_data = await hive_get(session, PATH_MONTHLY_PLAYER.format(game=quote(self.game_id, safe=""), name=q_name))
            elif self.period == "prev_month":
                monthly_data = await hive_get(session, PATH_MONTHLY_PLAYER_YM.format(game=quote(self.game_id, safe=""), name=q_name, year=py, month=pm))
            av_bytes, map_bytes = await asyncio.gather(
                fetch_image_bytes(session, av_url), get_bg_map_bytes(session, meta_key)
            )

        s = monthly_data if self.period in ("monthly", "prev_month") else self.allstats.get(self.game_id)
        if not s:
            gname = GAMES.get(self.game_id, {}).get("name", self.game_id)
            p_lbl = "今月" if self.period == "monthly" else (prev_lbl if self.period == "prev_month" else "全期間")
            png = await asyncio.to_thread(
                build_notice_card,
                f"{self.gamertag} さん - {gname}（{p_lbl}）",
                f"{p_lbl}のプレイ記録がまだ見つかりませんでした。",
            )
            return png, "hive_notice.png"

        diff = update_snapshot_diff(self.gamertag, self.game_id, self.period, s)
        analysis = build_analysis_text(s, diff)
        monthly_rank = s.get("human_index") if self.period in ("monthly", "prev_month") else None
        p_lbl = "今月" if self.period == "monthly" else (f"先月 {prev_lbl}" if self.period == "prev_month" else "全期間")
        png = await asyncio.to_thread(
            build_card, self.game_id, self.gamertag, self.main, s, meta, p_lbl, av_bytes, map_bytes, monthly_rank, analysis
        )
        return png, "hive_stats.png"

    async def _render_and_update(self, interaction: discord.Interaction):
        await interaction.response.defer()
        try:
            png, fname = await self._render_png()
        except HiveRateLimited as e:
            png = await asyncio.to_thread(
                build_notice_card,
                "ただいまアクセスが集中しています",
                f"約{max(1, e.wait // 60)}分後にもう一度お試しください。",
                True,
            )
            fname = "hive_notice.png"
        await interaction.edit_original_response(
            content=None,
            embed=None,
            attachments=[discord.File(io.BytesIO(png), filename=fname)],
            view=self,
        )

    async def on_game_select(self, interaction: discord.Interaction):
        self.game_id = interaction.data["values"][0]
        if self.view_mode == "cosmetics":
            self.view_mode = "card"
        for child in self.children:
            if isinstance(child, discord.ui.Select):
                for opt in child.options:
                    opt.default = (opt.value == self.game_id)
        await self._render_and_update(interaction)

    @discord.ui.button(label="All-Time / Monthly / 先月", style=discord.ButtonStyle.primary, row=1)
    async def cycle_period(self, interaction: discord.Interaction, button: discord.ui.Button):
        order = ["all", "monthly", "prev_month"]
        self.period = order[(order.index(self.period) + 1) % len(order)]
        self.view_mode = "card"
        if self.game_id == "overview":
            self.game_id = "sky"
        await self._render_and_update(interaction)

    @discord.ui.button(label="Cosmetics", style=discord.ButtonStyle.secondary, row=1)
    async def show_cosmetics(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.view_mode = "cosmetics"
        await self._render_and_update(interaction)

    @discord.ui.button(label="Maps", style=discord.ButtonStyle.secondary, row=1)
    async def show_maps(self, interaction: discord.Interaction, button: discord.ui.Button):
        if self.game_id == "overview":
            self.game_id = "sky"
        self.view_mode = "maps"
        await self._render_and_update(interaction)


class LeaderboardPaginator(discord.ui.View):
    def __init__(self, game_id: str, period: str, entries: list[dict[str, Any]], map_bytes: Optional[bytes] = None):
        super().__init__(timeout=180)
        self.game_id = game_id
        self.period = period
        self.entries = entries
        self.map_bytes = map_bytes
        self.page = 0
        self.per_page = 10

    async def make_file(self) -> discord.File:
        png = await asyncio.to_thread(
            build_leaderboard_card, self.game_id, self.period, self.entries, self.page, self.per_page, self.map_bytes
        )
        return discord.File(io.BytesIO(png), filename="hive_leaderboard.png")

    @discord.ui.button(label="◀ 前の10件", style=discord.ButtonStyle.secondary)
    async def prev_page(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer()
        self.page = max(0, self.page - 1)
        await interaction.edit_original_response(content=None, embed=None, attachments=[await self.make_file()], view=self)

    @discord.ui.button(label="次の10件 ▶", style=discord.ButtonStyle.secondary)
    async def next_page(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer()
        max_page = max(0, -(-len(self.entries) // self.per_page) - 1)
        self.page = min(max_page, self.page + 1)
        await interaction.edit_original_response(content=None, embed=None, attachments=[await self.make_file()], view=self)


# ---------- ゲーマータグ自動補完 ----------
async def gamertag_autocomplete(
    interaction: discord.Interaction, current: str
) -> list[app_commands.Choice[str]]:
    q = current.strip()
    if len(q) < 4:
        return []
    try:
        async with aiohttp.ClientSession(headers={"User-Agent": USER_AGENT}) as session:
            res = await hive_get(session, PATH_SEARCH.format(query=quote(q, safe="")), ttl=SEARCH_CACHE_SEC)
        if isinstance(res, list):
            return [
                app_commands.Choice(
                    name=str(item.get("username_cc") or item.get("username"))[:100],
                    value=str(item.get("username"))[:100],
                )
                for item in res[:15]
                if item.get("username")
            ]
    except Exception:
        pass
    return []


# ---------- コマンド登録（すべて画像出力・誰でも利用可能） ----------
def setup_hive(bot: commands.Bot):
    global _assets_task
    # フォントとマップ背景の準備をバックグラウンドで開始（setup_hook内など、イベントループ上で呼ぶこと）
    _assets_task = asyncio.create_task(_prepare_assets())

    game_choices = [
        app_commands.Choice(name=v["name"], value=k)
        for k, v in GAMES.items()
    ][:25]

    def _file(png: bytes, name: str = "hive_notice.png") -> discord.File:
        return discord.File(io.BytesIO(png), filename=name)

    @bot.tree.command(name="hive_link", description="自分のMinecraftゲーマータグを登録します（次回から /hive で入力を省略できます）")
    @app_commands.describe(gamertag="普段使っているMinecraftのゲーマータグ")
    @app_commands.autocomplete(gamertag=gamertag_autocomplete)
    async def hive_link(interaction: discord.Interaction, gamertag: str):
        await wait_assets()
        clean = gamertag.strip()
        link_set(interaction.user.id, clean)
        png = await asyncio.to_thread(
            build_notice_card,
            "ゲーマータグを登録しました",
            f"「{clean}」を登録しました。次回から /hive だけで確認できます！",
        )
        await interaction.response.send_message(file=_file(png, "hive_link.png"), ephemeral=True)

    @bot.tree.command(name="hive_unlink", description="登録したゲーマータグを解除します")
    async def hive_unlink(interaction: discord.Interaction):
        await wait_assets()
        link_delete(interaction.user.id)
        png = await asyncio.to_thread(build_notice_card, "登録を解除しました", "ゲーマータグの登録を解除しました。")
        await interaction.response.send_message(file=_file(png, "hive_unlink.png"), ephemeral=True)

    @bot.tree.command(name="hive", description="Hiveの戦績やCosmeticsを画像で表示します")
    @app_commands.describe(
        gamertag="Minecraftのゲーマータグ（/hive_link 済みなら省略できます）",
        game="最初に見るゲーム（省略時はOverview）",
        period="集計する期間（All-Time / Monthly / 先月）",
    )
    @app_commands.autocomplete(gamertag=gamertag_autocomplete)
    @app_commands.choices(
        game=[app_commands.Choice(name="Overview（すべて）", value="overview")] + game_choices[:24],
        period=[
            app_commands.Choice(name="All-Time（全期間）", value="all"),
            app_commands.Choice(name="Monthly（今月）", value="monthly"),
            app_commands.Choice(name="Previous Month（先月）", value="prev_month"),
        ],
    )
    async def hive(
        interaction: discord.Interaction,
        gamertag: Optional[str] = None,
        game: Optional[app_commands.Choice[str]] = None,
        period: Optional[app_commands.Choice[str]] = None,
    ):
        target_tag = (gamertag or "").strip() or link_get(interaction.user.id)
        if not target_tag:
            await wait_assets()
            png = await asyncio.to_thread(
                build_notice_card,
                "ゲーマータグを指定してください",
                "ゲーマータグを入力するか、先に /hive_link で自分のタグを登録してください。",
            )
            await interaction.response.send_message(file=_file(png), ephemeral=True)
            return

        now = time.time()
        if now - _last_use.get(interaction.user.id, 0) < 4:
            await wait_assets()
            png = await asyncio.to_thread(build_notice_card, "少しお待ちください", "連続での実行を防ぐため、数秒空けてからお試しください。")
            await interaction.response.send_message(file=_file(png), ephemeral=True)
            return
        _last_use[interaction.user.id] = now
        await interaction.response.defer()
        await wait_assets()

        game_id = game.value if game else "overview"
        period_val = period.value if period else "all"
        q_name = quote(target_tag, safe="")

        try:
            async with aiohttp.ClientSession(headers={"User-Agent": USER_AGENT}) as session:
                allstats = await hive_get(session, PATH_ALL.format(name=q_name))
                main = allstats.get("main") if isinstance(allstats, dict) else None
                if allstats and not (isinstance(main, dict) and "player_number" in main):
                    mr = await hive_get(session, PATH_MAIN.format(name=q_name))
                    if isinstance(mr, dict):
                        main = mr.get("main", mr)
        except HiveRateLimited as e:
            png = await asyncio.to_thread(
                build_notice_card,
                "アクセスが集中しています",
                f"約{max(1, e.wait // 60)}分後にもう一度お試しください。",
                True,
            )
            await interaction.followup.send(file=_file(png))
            return
        except Exception as e:
            print(f"[hive] error: {e!r}")
            png = await asyncio.to_thread(
                build_notice_card,
                "データを読み込めませんでした",
                "しばらく時間を置いてからもう一度お試しください。",
                True,
            )
            await interaction.followup.send(file=_file(png))
            return

        if not allstats or not isinstance(allstats, dict):
            png = await asyncio.to_thread(
                build_notice_card,
                "プレイヤーが見つかりません",
                f"「{target_tag}」の戦績データは見つかりませんでした。",
                True,
            )
            await interaction.followup.send(file=_file(png))
            return

        view = HiveDashboardView(target_tag, game_id, period_val, "card", allstats, main or {})
        try:
            png, fname = await view._render_png()
        except HiveRateLimited as e:
            png = await asyncio.to_thread(
                build_notice_card,
                "アクセスが集中しています",
                f"約{max(1, e.wait // 60)}分後にもう一度お試しください。",
                True,
            )
            await interaction.followup.send(file=_file(png))
            return
        await interaction.followup.send(file=_file(png, fname), view=view)

    @bot.tree.command(name="hive_compare", description="2人のプレイヤーの戦績を画像でくらべます")
    @app_commands.describe(
        player1="1人目のゲーマータグ",
        player2="2人目のゲーマータグ",
        game="くらべるゲーム",
    )
    @app_commands.autocomplete(player1=gamertag_autocomplete, player2=gamertag_autocomplete)
    @app_commands.choices(game=game_choices)
    async def hive_compare(
        interaction: discord.Interaction,
        player1: str,
        player2: str,
        game: app_commands.Choice[str],
    ):
        await interaction.response.defer()
        await wait_assets()
        try:
            async with aiohttp.ClientSession(headers={"User-Agent": USER_AGENT}) as session:
                d1, d2 = await asyncio.gather(
                    hive_get(session, PATH_ALL.format(name=quote(player1.strip(), safe=""))),
                    hive_get(session, PATH_ALL.format(name=quote(player2.strip(), safe=""))),
                )
        except HiveRateLimited as e:
            png = await asyncio.to_thread(build_notice_card, "アクセスが集中しています", f"約{max(1, e.wait // 60)}分後に再度お試しください。", True)
            await interaction.followup.send(file=_file(png))
            return

        if not d1 or not d2:
            png = await asyncio.to_thread(build_notice_card, "プレイヤーが見つかりません", "指定されたプレイヤーのどちらかが見つかりませんでした。", True)
            await interaction.followup.send(file=_file(png))
            return
        s1, s2 = d1.get(game.value), d2.get(game.value)
        if not s1 or not s2:
            png = await asyncio.to_thread(build_notice_card, "比較できる記録がありません", f"{game.name} の比較に必要なプレイ記録が足りません。")
            await interaction.followup.send(file=_file(png))
            return
        png = await asyncio.to_thread(build_compare_card, game.value, player1.strip(), s1, player2.strip(), s2)
        await interaction.followup.send(file=_file(png, "hive_compare.png"))

    @bot.tree.command(name="hive_leaderboard", description="ゲームごとのトップ100ランキングを画像で表示します")
    @app_commands.describe(game="ゲーム", period="全期間 / 今月")
    @app_commands.choices(
        game=game_choices,
        period=[
            app_commands.Choice(name="全期間トップ100", value="all"),
            app_commands.Choice(name="今月トップ100", value="monthly"),
        ],
    )
    async def hive_leaderboard(
        interaction: discord.Interaction,
        game: app_commands.Choice[str],
        period: Optional[app_commands.Choice[str]] = None,
    ):
        await interaction.response.defer()
        await wait_assets()
        p_val = period.value if period else "all"
        path = PATH_LB_MONTHLY.format(game=quote(game.value, safe="")) if p_val == "monthly" else PATH_LB_ALL.format(game=quote(game.value, safe=""))
        meta_key = GAMES.get(game.value, {}).get("meta") or game.value
        try:
            async with aiohttp.ClientSession(headers={"User-Agent": USER_AGENT}) as session:
                entries, map_bytes = await asyncio.gather(
                    hive_get(session, path, ttl=CACHE_SEC),
                    get_bg_map_bytes(session, meta_key),
                )
        except HiveRateLimited as e:
            png = await asyncio.to_thread(build_notice_card, "アクセスが集中しています", f"約{max(1, e.wait // 60)}分後に再度お試しください。", True)
            await interaction.followup.send(file=_file(png))
            return
        if not isinstance(entries, list) or not entries:
            png = await asyncio.to_thread(build_notice_card, "ランキングを取得できません", f"{game.name} のランキングデータが見つかりませんでした。", True)
            await interaction.followup.send(file=_file(png))
            return
        paginator = LeaderboardPaginator(game.value, p_val, entries, map_bytes)
        await interaction.followup.send(file=await paginator.make_file(), view=paginator)

    @bot.tree.command(name="hive_global", description="Hive全体で遊んでいる累計プレイヤー数を画像で表示します")
    async def hive_global(interaction: discord.Interaction):
        await interaction.response.defer()
        await wait_assets()
        try:
            async with aiohttp.ClientSession(headers={"User-Agent": USER_AGENT}) as session:
                data = await hive_get(session, PATH_GLOBAL, ttl=META_CACHE_SEC)
        except HiveRateLimited as e:
            png = await asyncio.to_thread(build_notice_card, "アクセスが集中しています", f"約{max(1, e.wait // 60)}分後に再度お試しください。", True)
            await interaction.followup.send(file=_file(png))
            return
        up = data.get("unique_players", {}) if isinstance(data, dict) else {}
        if not up:
            png = await asyncio.to_thread(build_notice_card, "全体統計を取得できません", "しばらくしてからもう一度お試しください。", True)
            await interaction.followup.send(file=_file(png))
            return
        png = await asyncio.to_thread(build_global_card, up)
        await interaction.followup.send(file=_file(png, "hive_global.png"))

    @bot.tree.command(name="hive_maps", description="ゲームごとのマップ一覧を画像で表示します")
    @app_commands.describe(game="マップを見たいゲーム")
    @app_commands.choices(game=game_choices)
    async def hive_maps(interaction: discord.Interaction, game: app_commands.Choice[str]):
        await interaction.response.defer()
        await wait_assets()
        meta_key = GAMES.get(game.value, {}).get("meta") or game.value
        try:
            async with aiohttp.ClientSession(headers={"User-Agent": USER_AGENT}) as session:
                maps = await hive_get(session, PATH_MAPS.format(game=quote(meta_key, safe="")), ttl=META_CACHE_SEC) or []
                thumbs = await asyncio.gather(*(fetch_image_bytes(session, m.get("image")) for m in maps[:12]))
        except HiveRateLimited as e:
            png = await asyncio.to_thread(build_notice_card, "アクセスが集中しています", f"約{max(1, e.wait // 60)}分後に再度お試しください。", True)
            await interaction.followup.send(file=_file(png))
            return
        png = await asyncio.to_thread(build_maps_card, game.value, maps, list(thumbs))
        await interaction.followup.send(file=_file(png, "hive_maps.png"))









# =====================
# 起動
# =====================
# =====================
# Groq API 設定
# =====================
GROQ_API_KEY = os.environ.get("GROQ_API_KEY", "")
KEITO_API_KEY    = os.environ.get("KEITO_API_KEY", "")      # APIキー
KEITO_PROCESS_ID = os.environ.get("KEITO_PROCESS_ID", "")   # プロセスID

TOKEN = os.environ.get("DISCORD_TOKEN")
if not TOKEN:
    raise RuntimeError("環境変数 DISCORD_TOKEN が設定されていません。ホスティング先の環境変数設定でBotトークンを登録してください。")
bot.run(TOKEN)
