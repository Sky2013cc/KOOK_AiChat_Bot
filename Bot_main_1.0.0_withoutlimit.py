import sqlite3
import datetime
import random
import string
import asyncio
from khl import Bot, Message
from openai import AsyncOpenAI
import time
import psutil
import platform
import os
import sys

KOOK_TOKEN = '1/NDc3NTA=/5dugffUuDxyA2rMFmy2oNw=='
AI_API_KEY = 'sk-649fsGsn3ghGmvxR5uHorS8AG3Pfwc6pd6dfWRN9oURPPj64'
AI_BASE_URL = 'https://aiapi.hkmc.online/v1' 

ADMIN_IDS = ['259604719'] 

SUPPORTED_MODELS = ["gpt-5.2", "gpt-5.4-mini", "gpt-5.5", "deepseek-v4-pro", "grok-4.20-fast"]
DEFAULT_MODEL = "gpt-5.4-mini"

MODEL_RATES = {
    "gpt-5.5": 2.0,
    "deepseek-v4-pro": 0.5
}

SYSTEM_PROMPT = {"role": "system", "content": "你是一个高效的助手。请用最简洁的语言回答，拒绝废话，只保留核心关键内容。"}

bot = Bot(token=KOOK_TOKEN)
aclient = AsyncOpenAI(api_key=AI_API_KEY, base_url=AI_BASE_URL)

user_history = {}
MAX_HISTORY_TURNS = 8 

def init_db():
    conn = sqlite3.connect('bot_data.db')
    cursor = conn.cursor()
    cursor.execute('''CREATE TABLE IF NOT EXISTS users 
                     (id TEXT PRIMARY KEY, balance INTEGER, last_checkin TEXT, 
                      current_model TEXT, has_redeemed INTEGER, use_context INTEGER DEFAULT 0,
                      is_banned INTEGER DEFAULT 0, use_search INTEGER DEFAULT 0, invite_count INTEGER DEFAULT 0)''')
    
    for col, default in [("is_banned", "0"), ("use_search", "0"), ("invite_count", "0")]:
        try: cursor.execute(f"ALTER TABLE users ADD COLUMN {col} INTEGER DEFAULT {default}")
        except: pass

    cursor.execute('''CREATE TABLE IF NOT EXISTS coupons 
                     (code TEXT PRIMARY KEY, amount INTEGER, used INTEGER, type INTEGER DEFAULT 0)''')
    try: cursor.execute("ALTER TABLE coupons ADD COLUMN type INTEGER DEFAULT 0")
    except: pass

    cursor.execute('''CREATE TABLE IF NOT EXISTS usage_log 
                     (user_id TEXT, tokens INTEGER, cost INTEGER, timestamp REAL)''')

    cursor.execute("SELECT count(*) FROM coupons")
    if cursor.fetchone()[0] == 0:
        for _ in range(10):
            code = ''.join(random.choices(string.ascii_uppercase + string.digits, k=12))
            cursor.execute("INSERT INTO coupons VALUES (?, ?, 0, 0)", (code, 500000))
    
    conn.commit()
    conn.close()

def get_user(user_id):
    conn = sqlite3.connect('bot_data.db')
    cursor = conn.cursor()
    cursor.execute("SELECT balance, last_checkin, current_model, has_redeemed, use_context, is_banned, use_search, invite_count FROM users WHERE id=?", (user_id,))
    res = cursor.fetchone()
    if not res:
        cursor.execute("INSERT INTO users (id, balance, last_checkin, current_model, has_redeemed, use_context, is_banned, use_search, invite_count) VALUES (?, 0, '', ?, 0, 0, 0, 0, 0)", 
                       (user_id, DEFAULT_MODEL))
        conn.commit()
        res = (0, "", DEFAULT_MODEL, 0, 0, 0, 0, 0)
    conn.close()
    return res

def update_user(user_id, **kwargs):
    conn = sqlite3.connect('bot_data.db')
    cursor = conn.cursor()
    for key, value in kwargs.items():
        cursor.execute(f"UPDATE users SET {key}=? WHERE id=?", (value, user_id))
    conn.commit()
    conn.close()

@bot.command(name='help')
async def help_cmd(msg: Message):
    help_text = (
        "✨ **AI 助手指令菜单 (无限制统计版)** ✨\n"
        "━━━━━━━━━━━━━━━\n"
        "👤 **核心对话**\n"
        "• `/c [问题]` - 发起聊天 (已解除限制)\n"
        "• `/money` - 查看累计消耗统计\n"
        "\n"
        "⚙️ **功能切换**\n"
        "• `/models [名称]` - 更改 AI 模型\n"
        "• `/list` - 获取模型列表\n"
        "• `/上下文开启/关闭` - 记忆开关\n"
        "\n"
        "👑 **管理员专用**\n"
        "• `/usage` - 查看全服统计"
    )
    await msg.reply(help_text)

@bot.command(name='money')
async def check_balance(msg: Message):
    balance, _, model, _, use_context, _, _, _ = get_user(msg.author.id) 
    rate = MODEL_RATES.get(model, 1.0)
    ctx = "🟢 已开启" if use_context == 1 else "⚪ 已关闭"
    await msg.reply(f"👤 **用户**：{msg.author.username}\n📊 **账户余额**：{balance} (负数代表累计消耗)\n🤖 **模型**：{model} ({rate}x)\n🧠 **记忆**：{ctx}")

@bot.command(name='sign')
async def checkin(msg: Message):
    user_id = msg.author.id
    data = get_user(user_id)
    balance, last_date, _, status, _, _, _, _ = data
    today = str(datetime.date.today())
    if last_date == today:
        await msg.reply(f"🚫 今日已签到。")
    else:
        reward = random.randint(10000, 50000)
        update_user(user_id, balance=balance + reward, last_checkin=today)
        await msg.reply(f"✅ 签到成功！获得 **{reward}** 额度。")

@bot.command(name='redeem')
async def redeem(msg: Message, code: str):
    conn = sqlite3.connect('bot_data.db')
    cursor = conn.cursor()
    cursor.execute("SELECT amount, used, type FROM coupons WHERE code=?", (code,))
    res = cursor.fetchone()
    if res and res[1] == 0:
        amount, _, c_type = res
        user_data = get_user(msg.author.id)
        update_user(msg.author.id, balance=user_data[0] + amount)
        cursor.execute("UPDATE coupons SET used=1 WHERE code=?", (code,))
        conn.commit()
        await msg.reply(f"🎉 兑换成功！增加 {amount}")
    else: await msg.reply("❌ 无效。")
    conn.close()

@bot.command(name='list')
async def list_models(msg: Message):
    txt = "📜 **模型列表**：\n" + "\n".join([f"• `{m}` ({MODEL_RATES.get(m, 1.0)}x)" for m in SUPPORTED_MODELS])
    await msg.reply(txt)

@bot.command(name='models')
async def switch_model(msg: Message, model_name: str):
    if model_name in SUPPORTED_MODELS:
        update_user(msg.author.id, current_model=model_name)
        await msg.reply(f"🔄 切换至：`{model_name}`")
    else:
        await msg.reply("❌ 不支持。")

@bot.command(name='上下文开启')
async def enable_ctx(msg: Message):
    update_user(msg.author.id, use_context=1)
    await msg.reply("🧠 开启。")

@bot.command(name='上下文关闭')
async def disable_ctx(msg: Message):
    update_user(msg.author.id, use_context=0)
    user_history.pop(msg.author.id, None)
    await msg.reply("🧠 关闭。")

@bot.command(name='清除上下文')
async def clear_ctx(msg: Message):
    user_history.pop(msg.author.id, None)
    await msg.reply("🧹 已清空记忆。")

@bot.command(name='online')
async def toggle_search(msg: Message, switch: str):
    s = 1 if "开" in switch else 0
    update_user(msg.author.id, use_search=s)
    await msg.reply(f"🌐 联网搜索{'开启' if s else '关闭'}。")

def is_admin(user_id):
    return str(user_id) in ADMIN_IDS

@bot.command(name='usage')
async def admin_usage(msg: Message, hours: float = 1.0):
    if not is_admin(msg.author.id): return
    since = time.time() - (hours * 3600)
    conn = sqlite3.connect('bot_data.db')
    cursor = conn.cursor()
    cursor.execute("SELECT COUNT(*), SUM(tokens), SUM(cost) FROM usage_log WHERE timestamp > ?", (since,))
    count, total_tokens, total_cost = cursor.fetchone()
    conn.close()
    await msg.reply(f"📊 **全服统计 ({hours}h)**\n次数：{count or 0}\n消耗：{total_tokens or 0} Tokens")

@bot.command(name='status')
async def admin_sys_status(msg: Message):
    if not is_admin(msg.author.id): return
    cpu_usage = psutil.cpu_percent(interval=1)
    vm = psutil.virtual_memory()
    p = psutil.Process(os.getpid())
    uptime_seconds = time.time() - p.create_time()
    uptime_str = time.strftime("%H时%M分%S秒", time.gmtime(uptime_seconds))
    await msg.reply(f"🖥️ CPU: {cpu_usage}% | RAM: {vm.percent}% | 运行: {uptime_str}")

@bot.command(name='restart')
async def admin_restart(msg: Message):
    if not is_admin(msg.author.id): return
    await msg.reply("🔄 重启中...")
    await asyncio.sleep(2)
    python = sys.executable
    os.execv(python, [python] + sys.argv)

@bot.command(name='ban')
async def admin_ban(msg: Message, target: str):
    if not is_admin(msg.author.id): return
    update_user(target, is_banned=1)
    await msg.reply(f"🚫 已标记封禁 `{target}`。")

@bot.command(name='unban')
async def admin_unban(msg: Message, target: str):
    if not is_admin(msg.author.id): return
    update_user(target, is_banned=0)
    await msg.reply(f"🕊️ 已解封 `{target}`。")

async def ai_response_logic(msg: Message, content: str):
    user_id = msg.author.id
    balance, _, model, _, use_context, _, use_search, _ = get_user(user_id)
    
    if not content.strip(): return

    try:
        final_prompt = SYSTEM_PROMPT["content"]
        if use_search: final_prompt += " 请联网搜索最新信息回答。"
        
        messages = [{"role": "system", "content": final_prompt}]
        if use_context == 1:
            messages.extend(user_history.get(user_id, []))
        messages.append({"role": "user", "content": content})

        response = await aclient.chat.completions.create(model=model, messages=messages, timeout=60.0)
        
        answer = response.choices[0].message.content
        tokens = response.usage.total_tokens
        
        rate = MODEL_RATES.get(model, 1.0)
        if use_search: rate *= 1.1
        cost = int(tokens * rate)
        
        new_bal = balance - cost
        update_user(user_id, balance=new_bal)
        
        conn = sqlite3.connect('bot_data.db')
        conn.cursor().execute("INSERT INTO usage_log VALUES (?, ?, ?, ?)", (user_id, tokens, cost, time.time()))
        conn.commit(); conn.close()

        if use_context == 1:
            if user_id not in user_history: user_history[user_id] = []
            user_history[user_id].extend([{"role": "user", "content": content}, {"role": "assistant", "content": answer}])
            user_history[user_id] = user_history[user_id][-MAX_HISTORY_TURNS:]

        info = f"\n\n⚡ {model} | 消耗: {tokens} Tokens"
        await msg.reply(answer + info)

    except Exception as e:
        await msg.reply(f"❌ 出错：{str(e)[:50]}")

@bot.command(name='c')
async def c_cmd(msg: Message, *args):
    await ai_response_logic(msg, " ".join(args))

@bot.command(name='chat')
async def chat_cmd(msg: Message, *args):
    await ai_response_logic(msg, " ".join(args))

if __name__ == '__main__':
    init_db()
    print(">>> 就绪")
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        bot.run()
    except KeyboardInterrupt:
        pass
