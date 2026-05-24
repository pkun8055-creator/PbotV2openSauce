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
    lambda: "/helpでコマンドを確認!",
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
@tree.command(name="set_webhook", description="【管理者専用】コマンドログ用WebhookのURLを登録します")
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
    await interaction.response.send_message("✅ Webhook URLを登録しました。これからコマンド実行がログに送られます。", ephemeral=True)

@tree.command(name="remove_webhook", description="【管理者専用】Webhookログを解除します")
async def remove_webhook(interaction: discord.Interaction):
    if not interaction.guild_permissions.manage_guild:
        await interaction.response.send_message("❌ サーバー管理者のみ設定できます", ephemeral=True)
        return
    cursor.execute("DELETE FROM webhook_log WHERE guild_id=?", (interaction.guild.id,))
    conn.commit()
    await interaction.response.send_message("✅ Webhookログを解除しました", ephemeral=True)

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
    await interaction.response.send_message("✅ グローバルWebhookを設定しました。全サーバーのカウントゲームログがここに届きます", ephemeral=True)

@tree.command(name="remove_global_webhook", description="【BOT管理者専用】グローバルWebhookを解除")
async def remove_global_webhook(interaction: discord.Interaction):
    if interaction.user.id not in OWNER_ID:
        await interaction.response.send_message("❌ BOT管理者専用です", ephemeral=True)
        return
    cursor.execute("DELETE FROM global_webhook WHERE id=1")
    conn.commit()
    await interaction.response.send_message("✅ グローバルWebhookを解除しました", ephemeral=True)

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
@tree.command(name="coin", description="自分のコイン枚数を表示します（全サーバー共通）")
async def coin(interaction: discord.Interaction):
    coins = get_global_coins(interaction.user.id)
    await interaction.response.send_message(
        f"💰 {interaction.user.mention} さんのコイン枚数: **{coins}枚**（全サーバー共通）",
        ephemeral=True
    )

WORK_COOLDOWN_MIN = 60

@tree.command(name="work", description="/work でコインを稼ぎます（クールタイムあり）")
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

    reward = random.randint(200, 500)
    add_global_coins(user_id, reward)
    total = get_global_coins(user_id)

    next_time = now + datetime.timedelta(minutes=WORK_COOLDOWN_MIN)
    cursor.execute(
        "INSERT INTO reminders (user_id, channel_id, target_time, notification_type) VALUES (?, ?, ?, ?)",
        (user_id, interaction.channel_id, next_time.isoformat(), "work")
    )
    conn.commit()

    embed = discord.Embed(
        title="💼 お仕事完了！",
        color=discord.Color.green()
    )
    embed.set_author(name=interaction.user.display_name, icon_url=interaction.user.display_avatar.url)
    embed.add_field(name="獲得コイン", value=f"**+{reward}コイン** 💰", inline=True)
    embed.add_field(name="所持コイン", value=f"**{total}コイン**", inline=True)
    embed.add_field(name="次回使用可能", value=f"{next_time.strftime('%H:%M')}", inline=True)
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
        await interaction.response.send_message(f"✅ ユーザー {uid} に {amount}コイン付与しました", ephemeral=True)

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
        await interaction.response.send_message(f"✅ ユーザー {uid} のコインをリセットしました", ephemeral=True)

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

async def add_xp(member: discord.Member, guild: discord.Guild, amount: int):
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

    # クールタイム 10秒
    if now - last < 10:
        return

    xp += amount
    new_level = get_level(xp)

    cursor.execute("""
        INSERT INTO user_data (user_id, guild_id, xp, level, last_message)
        VALUES (?, ?, ?, ?, ?)
        ON CONFLICT(user_id, guild_id)
        DO UPDATE SET xp=?, level=?, last_message=?
    """, (user_id, guild_id, xp, new_level, now, xp, new_level, now))
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
    pr_entry = get_next_pr()
    if pr_entry:
        pr_uid, pr_text = pr_entry
        pr_user = bot.get_user(pr_uid)
        pr_name = pr_user.display_name if pr_user else f"ID:{pr_uid}"
        embed.add_field(
            name="📣 PR",
            value=f"{pr_text}\n-# pr者： {pr_name}　⚠️ こちらのリンクは危険な可能性もあります",
            inline=False
        )

    await interaction.followup.send(embed=embed)

@tree.command(name="rank", description="レベルランキング（サーバー内TOP10）")
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
        await interaction.response.send_message(f"✅ 通知チャンネルを <#{ch_id}> に設定しました", ephemeral=True)

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
        await interaction.response.send_message(f"✅ レベルアップ通知を **{state}** にしました", ephemeral=True)

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
    await interaction.response.send_message(f"✅ {user.display_name} を Lv {level} にしました")

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
        await interaction.response.send_message(f"✅ 荒らし文を追加しました：\n```{text}```", ephemeral=True)
    except sqlite3.IntegrityError:
        await interaction.response.send_message("⚠ その文はすでに登録されています。", ephemeral=True)

# =====================
# on_ready
# =====================
@bot.event
async def on_ready():
    print(f"BOT起動完了: {bot.user}")
    try:
        await bot.tree.sync()
        print("スラッシュコマンド同期完了")
    except Exception as e:
        print("同期失敗:", e)

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

    if not reminder_loop.is_running():
        reminder_loop.start()
    if not pin_loop.is_running():
        pin_loop.start()
    if not status_loop.is_running():
        status_loop.start()

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

    # 荒らし検知
    if is_ozeu(content) and not is_staff:
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

                # 数字ミス → リセット
                if number != current:
                    await message.add_reaction("❌")
                    reset_global_coins(message.author.id)
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
                await message.add_reaction("✅")
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
    await interaction.response.send_message("✅ このメッセージをPINに設定しました。", ephemeral=True)
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
    await interaction.response.send_message("✅ PINを解除しました。", ephemeral=True)

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
        status = "✅ 所持済み" if owned else ("💰 " + str(item["price"]) + "コイン")
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
                "✅ すでにPR権限を所持しています。`/setpr` で宣伝文を設定できます",
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
            "✅ **PR権限**を購入しました！`/setpr [宣伝文]` で宣伝文を設定してください",
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
        "✅ 宣伝文を設定しました！\n📣 PR: " + pr_value,
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
    await interaction.response.send_message("✅ ゲーム開始！", ephemeral=True)

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
    report_channel = discord.utils.get(guild.text_channels, name=REPORT_CHANNEL_NAME)

    if not report_channel:
        await interaction.response.send_message("⚠ 通報用チャンネルが見つかりません", ephemeral=True)
        return

    embed = discord.Embed(title="🚨 メッセージ通報", color=discord.Color.red())
    embed.add_field(name="通報者", value=f"{interaction.user} ({interaction.user.id})", inline=False)
    embed.add_field(name="投稿者", value=f"{message.author} ({message.author.id})", inline=False)
    embed.add_field(name="チャンネル", value=message.channel.mention, inline=False)
    embed.add_field(name="メッセージ内容", value=message.content or "（内容なし）", inline=False)
    embed.add_field(name="メッセージリンク", value=f"[ジャンプ]({message.jump_url})", inline=False)
    await report_channel.send(embed=embed)

    await send_webhook_log(guild.id, embed)

    await interaction.response.send_message("✅ メッセージを通報しました。管理者が確認します。", ephemeral=True)

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
    pr_entry = get_next_pr()
    if pr_entry:
        pr_uid, pr_text = pr_entry
        pr_user = bot.get_user(pr_uid)
        pr_name = pr_user.display_name if pr_user else f"ID:{pr_uid}"
        embed.add_field(
            name="📣 PR",
            value=f"{pr_text}\n-# pr者： {pr_name}　⚠️ こちらのリンクは危険な可能性もあります",
            inline=False
        )
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
    pr_entry = get_next_pr()
    if pr_entry:
        pr_uid, pr_text = pr_entry
        pr_user = bot.get_user(pr_uid)
        pr_name = pr_user.display_name if pr_user else f"ID:{pr_uid}"
        embed.add_field(
            name="📣 PR",
            value=f"{pr_text}\n-# pr者： {pr_name}　⚠️ こちらのリンクは危険な可能性もあります",
            inline=False
        )
    if interaction.guild:
        log_embed = make_action_embed("/slot", interaction.user, color, 結果=view_str, 詳細=msg)
        await send_webhook_log(interaction.guild.id, log_embed)
    await interaction.followup.send(embed=embed)

# =====================
# 挨拶ON/OFF
# =====================
@tree.command(name="greeting", description="挨拶機能のON/OFFを切り替えます")
@app_commands.describe(mode="on または off")
async def greeting_toggle(interaction: discord.Interaction, mode: str):
    await interaction.response.defer(ephemeral=True)
    if not interaction.guild_permissions.manage_guild:
        await interaction.followup.send("❌ サーバー管理者のみ変更できます", ephemeral=True)
        return
    if mode.lower() not in ["on", "off"]:
        await interaction.followup.send("使い方: /greeting on または /greeting off", ephemeral=True)
        return
    guild_id = str(interaction.guild.id)
    greeting_settings[guild_id] = (mode.lower() == "on")
    save_greeting_settings(greeting_settings)
    state = "ON" if greeting_settings[guild_id] else "OFF"

    await interaction.followup.send(f"✅ このサーバーの挨拶機能を **{state}** にしました")
    log_embed = make_action_embed(
        "💬 挨拶機能変更",
        interaction.user,
        discord.Color.blurple(),
        変更後=state
    )
    if interaction.guild:
        await send_webhook_log(interaction.guild.id, log_embed)

# =====================
# サーバーリスト
# =====================
@tree.command(name="serverlist", description="参加サーバー一覧（BOT管理者専用）")
async def serverlist(interaction: discord.Interaction):
    if interaction.user.id not in OWNER_ID:
        await interaction.response.send_message("❌ このコマンドは管理者専用です", ephemeral=True)
        return
    await interaction.response.defer(ephemeral=True)
    text = ""
    for guild in bot.guilds:
        invite_url = "取得失敗"
        try:
            for ch in guild.text_channels:
                if ch.permissions_for(guild.me).create_instant_invite:
                    invite = await ch.create_invite(max_age=0, max_uses=0)
                    invite_url = invite.url
                    break
        except:
            pass
        text += f"**📌 {guild.name}**\n人数: {guild.member_count}\nオーナー: {guild.owner}\n招待: {invite_url}\n\n"
    await interaction.followup.send(text[:2000] or "なし", ephemeral=True)

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
        f"✅ 送信完了\n成功: {sent}\n失敗: {failed}\nスキップ（無効）: {skipped}",
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
    await interaction.response.send_message(f"✅ お知らせチャンネルを {channel.mention} に設定しました", ephemeral=True)
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
    await interaction.response.send_message("✅ このサーバーへのお知らせを無効にしました。再度有効にするには `/set_announce_channel` を使ってください", ephemeral=True)
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
            "✅ `pkun.py` を更新しました。\n反映するには `/restart` を実行してください。",
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

KEITO_BASE = "https://keitocloud.jp/api/v1"

async def keito_get_process_id() -> str | None:
    """KEITO Cloudで動いているプロセスIDを自動取得する"""
    if not KEITO_API_KEY:
        return None
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(
                f"{KEITO_BASE}/processes",
                headers={"Authorization": f"Bearer {KEITO_API_KEY}"},
                timeout=aiohttp.ClientTimeout(total=10)
            ) as resp:
                if resp.status != 200:
                    return None
                data = await resp.json()
        # リスト形式 or dict形式に対応
        processes = data if isinstance(data, list) else data.get("processes", [])
        if not processes:
            return None
        # 最初のプロセスIDを返す（複数ある場合は最新のものを使う）
        p = processes[-1]
        return str(p.get("id") or p.get("process_id") or p.get("pid") or "")
    except Exception as e:
        print(f"[KEITO] プロセス一覧取得エラー: {e}")
        return None

@tree.command(name="logs", description="【BOT管理者専用】KEITO CloudのBOTログを表示します")
@app_commands.describe(lines="取得する行数（デフォルト30）")
@is_authorized()
async def show_logs(interaction: discord.Interaction, lines: int = 30):
    await interaction.response.defer(ephemeral=True)
    if not KEITO_API_KEY:
        await interaction.followup.send("❌ `KEITO_API_KEY` が設定されていません", ephemeral=True)
        return
    try:
        # プロセスID: 環境変数 > 自動取得
        pid = KEITO_PROCESS_ID or await keito_get_process_id()
        if not pid:
            await interaction.followup.send("❌ 実行中のプロセスが見つかりません", ephemeral=True)
            return

        async with aiohttp.ClientSession() as session:
            async with session.get(
                f"{KEITO_BASE}/processes/{pid}/logs",
                headers={"Authorization": f"Bearer {KEITO_API_KEY}"},
                timeout=aiohttp.ClientTimeout(total=10)
            ) as resp:
                if resp.status != 200:
                    await interaction.followup.send(f"❌ API エラー: HTTP {resp.status} (process_id={pid})", ephemeral=True)
                    return
                data = await resp.json()

        # レスポンス形式に対応
        if isinstance(data, str):
            log_text = data
        elif isinstance(data, dict):
            log_text = data.get("logs") or data.get("output") or data.get("data") or str(data)
        elif isinstance(data, list):
            log_text = "\n".join(str(x) for x in data)
        else:
            log_text = str(data)

        log_lines = log_text.strip().splitlines()
        tail = log_lines[-lines:] if len(log_lines) > lines else log_lines
        result = "\n".join(tail)
        if len(result) > 1900:
            result = result[-1900:]

        await interaction.followup.send(f"```\n{result or "(ログなし)"}\n```", ephemeral=True)
    except Exception as e:
        await interaction.followup.send(f"❌ 取得失敗: {e}", ephemeral=True)

@tree.command(name="keito_processes", description="【BOT管理者専用】KEITO Cloudのプロセス一覧を表示します")
@is_authorized()
async def keito_processes(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)
    if not KEITO_API_KEY:
        await interaction.followup.send("❌ `KEITO_API_KEY` が設定されていません", ephemeral=True)
        return
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(
                f"{KEITO_BASE}/processes",
                headers={"Authorization": f"Bearer {KEITO_API_KEY}"},
                timeout=aiohttp.ClientTimeout(total=10)
            ) as resp:
                data = await resp.json()
        await interaction.followup.send(f"```json\n{str(data)[:1900]}\n```", ephemeral=True)
    except Exception as e:
        await interaction.followup.send(f"❌ エラー: {e}", ephemeral=True)

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
        embed = discord.Embed(title="⭐ レベルシステム", description="メッセージでXPが増えてレベルアップ！", color=discord.Color.blue())
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
    """interaction_metadata か mentions からユーザーを取得する"""
    # 方法1: interaction_metadata から user_id を取得
    if message.interaction_metadata:
        try:
            # discord.py によって .user か .user_id のどちらかを持つ
            meta = message.interaction_metadata
            uid = getattr(meta, "user_id", None)
            if uid is None and hasattr(meta, "user"):
                uid = meta.user.id
            if uid:
                return await bot.fetch_user(int(uid))
        except Exception as e:
            print(f"[takasumibot] interaction_metadata取得失敗: {e}")
    # 方法2: message.interaction (古いAPI)
    if hasattr(message, "interaction") and message.interaction:
        try:
            return message.interaction.user
        except Exception:
            pass
    # 方法3: mentions
    if message.mentions:
        return message.mentions[0]
    return None

async def detect_takasumibot_work(message: discord.Message):
    """takasumibot からのメッセージを検知して各種リマインダーを登録する"""
    if message.author.id != TAKASUMIBOT_ID:
        return

    # デバッグ: takasumibotのメッセージ内容をprint
    print(f"[takasumibot] メッセージ受信: embeds={len(message.embeds)}, content={message.content!r}")
    for i, e in enumerate(message.embeds):
        print(f"  embed[{i}] title={e.title!r} desc={e.description!r}")

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
            await message.channel.send(embed=res_embed)
            return

# =====================
# ひろゆきAI
# =====================
HIROYUKI_SYSTEM = """あなたは西村博之（ひろゆき）として返答してください。

【絶対ルール】
- 必ず日本語のみで返答する。韓国語・中国語・英語は一切使わない
- 返答は1〜2文のみ。それ以上は書かない
- 絵文字は使わない

【口調・スタイル】
- 淡々と、感情を見せずに相手の論理の穴を突く
- 「〜ですよね？」「〜じゃないですか」で締める
- 相手の主張を一言で否定してから理由を言う
- 上から目線で、でも怒らず冷静に論破する

【例】
ユーザー:「俺は天才だ」
返答:「それって自分で言ってるだけですよね。他人に認められてから言うことじゃないですか。」

ユーザー:「絶対に勝てる」
返答:「証拠ないですよね。まあ、思い込みって怖いですよね。」"""

async def hiroyuki_respond(message: discord.Message):
    if not GROQ_API_KEY:
        return
    if not message.content or message.content.startswith("/"):
        return
    try:
        async with message.channel.typing():
            async with aiohttp.ClientSession() as session:
                payload = {
                    "model": "llama-3.3-70b-versatile",
                    "max_tokens": 150,
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
                    if resp.status != 200:
                        print(f"[ひろゆきAI] API エラー: {resp.status} {await resp.text()}")
                        return
                    data = await resp.json()
            reply = data["choices"][0]["message"]["content"]
            await message.reply(reply)
    except Exception as e:
        print(f"[ひろゆきAI] エラー: {e}")

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
        f"✅ {channel.mention} でひろゆきAIが有効になりました",
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
    await interaction.response.send_message("✅ ひろゆきAIを無効にしました", ephemeral=True)

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
# KEITO Cloud 設定
# =====================
KEITO_API_KEY    = os.environ.get("KEITO_API_KEY", "")      # APIキー
KEITO_PROCESS_ID = os.environ.get("KEITO_PROCESS_ID", "")   # プロセスID

TOKEN = os.environ.get("DISCORD_TOKEN") or "ここにTOKENを直接貼り付けてください"  # ← BOTトークンを貼り付け
bot.run(TOKEN)
