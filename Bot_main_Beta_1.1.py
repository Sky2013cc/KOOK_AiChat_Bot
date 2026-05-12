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
import os,requests
import sys 
import json
import threading
from flask import Flask, render_template_string, request, redirect
import io, sys, queue
log_queue = queue.Queue(maxsize=100)

class LogCapture(io.StringIO):
    def write(self, s):
        if s.strip():
            if log_queue.full(): log_queue.get()
            log_queue.put(f"[{datetime.datetime.now().strftime('%H:%M:%S')}] {s.strip()}")
        super().write(s)

sys.stdout = LogCapture() # 开始捕获

# ==========================================
#               ⚙️ 核心全局配置
# ==========================================
SYSTEM_PROMPT = {"role": "system", "content": "你是一个高效的助手。请用最简洁的语言回答，拒绝废话，只保留核心关键内容。"}
user_history = {}
MAX_HISTORY_TURNS = 8
SUPPORTED_MODELS = ["gpt-3.5-turbo"] # 初始值
DEFAULT_MODEL = "gpt-3.5-turbo"
MODEL_RATES = {}

# ==========================================
#               📁 配置管理逻辑 (新增)
# ==========================================
CONFIG_FILE = 'config.json'

def load_config():
    default_config = {
        "KOOK_TOKEN": "这里填写你的token",
        "AI_API_KEY": "填写你的apikey",
        "AI_BASE_URL": "你的api地址",
        "ADMIN_IDS": ["你的ID"],
        "DEFAULT_MODEL": "gpt-3.5-turbo",
        "SUPPORTED_MODELS": ["gpt-3.5-turbo", "gpt-4-turbo"]
    }
    if not os.path.exists(CONFIG_FILE):
        with open(CONFIG_FILE, 'w', encoding='utf-8') as f:
            json.dump(default_config, f, indent=4, ensure_ascii=False)
        return default_config
    with open(CONFIG_FILE, 'r', encoding='utf-8') as f:
        return json.load(f)

def save_config(new_config):
    with open(CONFIG_FILE, 'w', encoding='utf-8') as f:
        json.dump(new_config, f, indent=4, ensure_ascii=False)

# 核心：启动时先加载一次配置
config = load_config()

# 将配置分配给原有变量，确保后续逻辑不崩溃
KOOK_TOKEN = config['KOOK_TOKEN']
AI_API_KEY = config['AI_API_KEY']
AI_BASE_URL = config['AI_BASE_URL']
ADMIN_IDS = config['ADMIN_IDS']
SUPPORTED_MODELS = config['SUPPORTED_MODELS']
DEFAULT_MODEL = config['DEFAULT_MODEL']
# ==========================================
#           🎯 系统提示词 
# ==========================================
# 这个变量定义了 AI 的性格和回复风格
SYSTEM_PROMPT = {
    "role": "system", 
    "content": "你是一个高效的助手。请用最简洁的语言回答，拒绝废话，只保留核心关键内容。"
}
# 初始化客户端
bot = Bot(token=KOOK_TOKEN)
aclient = AsyncOpenAI(api_key=AI_API_KEY, base_url=AI_BASE_URL)
user_history = {}      # 用于存储每个用户的对话历史
MAX_HISTORY_TURNS = 8  


# ==========================================
#               🗄️ 数据库操作
# ==========================================
def init_db():
    conn = sqlite3.connect('bot_data.db')
    cursor = conn.cursor()
    # 创建用户表（新增了 is_banned, use_search, invite_count）
    cursor.execute('''CREATE TABLE IF NOT EXISTS users 
                     (id TEXT PRIMARY KEY, balance INTEGER, last_checkin TEXT, 
                      current_model TEXT, has_redeemed INTEGER, use_context INTEGER DEFAULT 0,
                      is_banned INTEGER DEFAULT 0, use_search INTEGER DEFAULT 0, invite_count INTEGER DEFAULT 0)''')
    

    for col, default in [("is_banned", "0"), ("use_search", "0"), ("invite_count", "0")]:
        try: cursor.execute(f"ALTER TABLE users ADD COLUMN {col} INTEGER DEFAULT {default}")
        except: pass

    # 兑换码表
    cursor.execute('''CREATE TABLE IF NOT EXISTS coupons 
                     (code TEXT PRIMARY KEY, amount INTEGER, used INTEGER, type INTEGER DEFAULT 0)''')
    try: cursor.execute("ALTER TABLE coupons ADD COLUMN type INTEGER DEFAULT 0")
    except: pass

    # 新增：使用记录表（用于统计功能）
    cursor.execute('''CREATE TABLE IF NOT EXISTS usage_log 
                     (user_id TEXT, tokens INTEGER, cost INTEGER, timestamp REAL)''')

    # 初始 50 个码逻辑（仅在数据库为空时执行）
    cursor.execute("SELECT count(*) FROM coupons")
    if cursor.fetchone()[0] == 0:
        print("======== 🎟️ 正在初始化生成 10 个兑换码 ========")
        for _ in range(10):
            code = ''.join(random.choices(string.ascii_uppercase + string.digits, k=12))
            cursor.execute("INSERT INTO coupons VALUES (?, ?, 0, 0)", (code, 500000))
            print(f"卡密: {code}")
        print("===============================================")
    
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

# ==========================================
#               🎮 机器人指令集
# ==========================================
@bot.command(name='help')
async def help_cmd(msg: Message):
    help_text = (
        "✨ **AI 助手指令菜单** ✨\n"
        "━━━━━━━━━━━━━━━\n"
        "👤 **核心对话**\n"
        "• `/c [问题]` - 发起聊天\n"
        "• `/money` - 查看 Token 余额、模型及功能开关\n"
        "• `/sign` - 领随机 Token (邀请用户不可用)\n"
        "• `/redeem [卡密]` - 充值或激活账号\n"
        "\n"
        "⚙️ **功能切换**\n"
        "• `/models [名称]` - 更改 AI 大脑\n"
        "• `/list` - 获取模型列表\n"
        "• `/上下文开启/关闭` - 是否开启记忆\n"
        "• `/联网搜索 [开/关]` - 开启后 1.1x 计费，暂时有些问题，建议别开\n"
        "\n"
        "📩 **账号与社交**\n"
        "• `/invite` - 耗费 1.2w 生成一个邀请码\n"
        "• `/注销账户 确认` - 清空所有个人数据\n"
        "\n"
        "👑 **管理员专用**\n"
        "• `/调费率` | `/封禁` | `/使用情况`\n"
        "━━━━━━━━━━━━━━━"
    )
    await msg.reply(help_text)


@bot.command(name='money')
async def check_balance(msg: Message):
    balance, _, model, _, use_context, _, _, _ = get_user(msg.author.id) 
    rate = MODEL_RATES.get(model, 1.0)
    ctx = "🟢 已开启" if use_context == 1 else "⚪ 已关闭"
    await msg.reply(f"👤 **用户**：{msg.author.username}\n💰 **余额**：{balance} token\n🤖 **模型**：{model} ({rate}x)\n🧠 **记忆**：{ctx}")

@bot.command(name='sign')
async def checkin(msg: Message):
    user_id = msg.author.id
    data = get_user(user_id)
    balance, last_date, _, status, _, _, _, _ = data
    if status == 0: return await msg.reply("🚫 请先兑换激活。")
    if status == 2: return await msg.reply("🚫 邀请码用户无法签到。")
    today = str(datetime.date.today())
    if last_date == today:
        await msg.reply(f"🚫 今日已签到。余额：{balance}")
    else:
        reward = random.randint(10000, 50000)
        update_user(user_id, balance=balance + reward, last_checkin=today)
        await msg.reply(f"✅ 签到成功！随机获得 **{reward}** token。\n当前余额：{balance + reward}")

@bot.command(name='redeem')
async def redeem(msg: Message, code: str):
    conn = sqlite3.connect('bot_data.db')
    cursor = conn.cursor()
    cursor.execute("SELECT amount, used, type FROM coupons WHERE code=?", (code,))
    res = cursor.fetchone()
    if res and res[1] == 0:
        amount, _, c_type = res
        user_data = get_user(msg.author.id)
        new_status = user_data[3]
        if user_data[3] == 0:
            new_status = 2 if c_type == 1 else 1
        update_user(msg.author.id, balance=user_data[0] + amount, has_redeemed=new_status)
        cursor.execute("UPDATE coupons SET used=1 WHERE code=?", (code,))
        conn.commit()
        await msg.reply(f"🎉 兑换成功！增加 {amount}")
    else: await msg.reply("❌ 卡密无效。")
    conn.close()


@bot.command(name='list')
async def list_models(msg: Message):
    txt = "📜 **可用模型及费率**：\n" + "\n".join([f"• `{m}` ({MODEL_RATES.get(m, 1.0)}x)" for m in SUPPORTED_MODELS])
    await msg.reply(txt)

@bot.command(name='models')
async def switch_model(msg: Message, model_name: str):
    if model_name in SUPPORTED_MODELS:
        update_user(msg.author.id, current_model=model_name)
        await msg.reply(f"🔄 模型已切换至：`{model_name}`")
    else:
        await msg.reply("❌ 不支持该模型。")

@bot.command(name='上下文开启')
async def enable_ctx(msg: Message):
    update_user(msg.author.id, use_context=1)
    await msg.reply("🧠 上下文模式：**开启**")

@bot.command(name='上下文关闭')
async def disable_ctx(msg: Message):
    update_user(msg.author.id, use_context=0)
    user_history.pop(msg.author.id, None)
    await msg.reply("🧠 上下文模式：**关闭**")

@bot.command(name='清除上下文')
async def clear_ctx(msg: Message):
    user_history.pop(msg.author.id, None)
    await msg.reply("🧹 记忆已清空。")
# --- 用户新功能 ---
@bot.command(name='online')
async def toggle_search(msg: Message, switch: str):
    s = 1 if "开" in switch else 0
    update_user(msg.author.id, use_search=s)
    await msg.reply(f"🌐 联网搜索已{'开启 (计费 1.1x)' if s else '关闭'}。")

@bot.command(name='delete')
async def delete_account(msg: Message, confirm: str = ""):
    if confirm != "confirm": return await msg.reply("⚠️ 请输入 `/delete confirm` 以清空所有数据！")
    update_user(msg.author.id, balance=0, has_redeemed=0, last_checkin="", use_context=0, invite_count=0)
    await msg.reply("💨 账户已重置。")

@bot.command(name='invite')
async def get_invite(msg: Message):
    user_id = msg.author.id
    bal, _, _, _, _, _, _, inv_count = get_user(user_id)
    if inv_count >= 3: return await msg.reply("❌ 最多只能生成 3 次邀请码。")
    if bal < 12000: return await msg.reply("❌ 余额不足 (需 12,000)。")
    
    code = "INV-" + ''.join(random.choices(string.ascii_uppercase + string.digits, k=8))
    conn = sqlite3.connect('bot_data.db')
    cursor = conn.cursor()
    cursor.execute("INSERT INTO coupons VALUES (?, 10000, 0, 1)", (code,))
    conn.commit(); conn.close()
    
    update_user(user_id, balance=bal-12000, invite_count=inv_count+1)
    await msg.reply(f"🎁 邀请码：`{code}` (面额 10,000)\n*注：使用此码的用户无法签到。*")




    # ==========================================
#               👑 管理员专用指令
# ==========================================

# 校验是否为管理员的装饰器函数
def is_admin(user_id):
    return str(user_id) in ADMIN_IDS

@bot.command(name='add')
async def add_balance(msg: Message, target_id: str, amount: int):
    if not is_admin(msg.author.id):
        return await msg.reply("❌ 权限不足。")
    
    # 获取当前数据（解包 8 个值）
    data = get_user(target_id)
    old_balance = data[0]
    
    # 更新余额
    new_balance = old_balance + amount
    update_user(target_id, balance=new_balance)
    
    await msg.reply(f"✅ 已为用户 `{target_id}` 充值 {amount} token。\n当前余额：{new_balance}")
    print(f"LOG - [管理员充值] 目标: {target_id} 数额: {amount}")
@bot.command(name='addmodel')
async def admin_add_model(msg: Message, model_name: str, rate: float = 1.0):
    """
    管理员手动添加新模型支持
    格式：/添加模型 [模型ID] [费率]
    例子：/添加模型 o1-mini 1.5
    """
    if not is_admin(msg.author.id):
        return await msg.reply("❌ 权限不足。")

    # 1. 检查是否已经存在
    if model_name in SUPPORTED_MODELS:
        return await msg.reply(f"⚠️ 模型 `{model_name}` 已经存在，如需修改费率请使用 `/调费率`。")

    # 2. 更新全局列表和费率字典
    SUPPORTED_MODELS.append(model_name)
    MODEL_RATES[model_name] = rate

    # 3. 反馈结果
    await msg.reply(
        f"✅ **模型添加成功**\n"
        f"🆔 名称：`{model_name}`\n"
        f"💰 初始费率：{rate}x\n"
        f"ℹ️ 现在用户可以使用 `/切换模型 {model_name}` 来使用它了。"
    )
    print(f"LOG - [管理员添加模型] 模型: {model_name} 费率: {rate}")

@bot.command(name='status')
async def admin_sys_status(msg: Message):
    """
    获取服务器主机的实时运行状态
    """
    if not is_admin(msg.author.id): # 校验管理员权限
        return await msg.reply("❌ 权限不足。")

    # --- 获取 CPU 信息 ---
    cpu_usage = psutil.cpu_percent(interval=1) # 获取最近1秒的平均占用
    cpu_count = psutil.cpu_count(logical=True)

    # --- 获取内存信息 ---
    vm = psutil.virtual_memory()
    total_ram = round(vm.total / (1024**3), 2)  # 转为 GB
    used_ram = round(vm.used / (1024**3), 2)
    ram_percent = vm.percent

    # --- 获取系统基本信息 ---
    sys_name = platform.system()
    sys_ver = platform.release()
    py_ver = platform.python_version()
    
    import time
    p = psutil.Process(os.getpid())
    uptime_seconds = time.time() - p.create_time()
    uptime_str = time.strftime("%H时%M分%S秒", time.gmtime(uptime_seconds))

    status_text = (
        "🖥️ **主机运行状态监控**\n"
        "━━━━━━━━━━━━━━━\n"
        f"📌 **系统环境**: {sys_name} {sys_ver}\n"
        f"🐍 **Python版本**: v{py_ver}\n"
        f"⏱️ **机器人运行**: {uptime_str}\n"
        "\n"
        f"📉 **CPU 占用**: {cpu_usage}% ({cpu_count} 核心)\n"
        f"💾 **内存 占用**: {ram_percent}% ({used_ram}G / {total_ram}G)\n"
        "━━━━━━━━━━━━━━━"
    )
    
    await msg.reply(status_text)
    print(f"LOG - [管理员查看状态] 由 {msg.author.username} 发起")

@bot.command(name='dashboard')
async def admin_dashboard(msg: Message):
    """远程查看机器人的整体经济和注册情况"""
    if not is_admin(msg.author.id): return
    
    conn = sqlite3.connect('bot_data.db')
    cursor = conn.cursor()
    
    # 统计总用户数和总发行余额
    cursor.execute("SELECT COUNT(*), SUM(balance) FROM users")
    user_count, total_bal = cursor.fetchone()
    
    # 统计未使用的卡密数量
    cursor.execute("SELECT COUNT(*) FROM coupons WHERE used=0")
    unused_coupons = cursor.fetchone()[0]
    
    # 查询余额前 3 名的大户 (抓出谁在疯狂消耗)
    cursor.execute("SELECT id, balance FROM users ORDER BY balance DESC LIMIT 3")
    top_users = cursor.fetchall()
    conn.close()

    top_str = "\n".join([f"TOP {i+1}: `{uid}` ({bal})" for i, (uid, bal) in enumerate(top_users)])
    
    await msg.reply(
        f"📊 **全局数据大盘**\n"
        f"━━━━━━━━━━━━━━━\n"
        f"👥 **总注册用户**: {user_count or 0} 人\n"
        f"💰 **全服总余额**: {total_bal or 0}\n"
        f"🎟️ **剩余可用卡密**: {unused_coupons} 张\n\n"
        f"🏆 **全服财富榜**:\n{top_str or '暂无数据'}\n"
        f"━━━━━━━━━━━━━━━"
    )
@bot.command(name='restart')
async def admin_restart(msg: Message):
    """
    远程重新启动机器人进程
    """
    if not is_admin(msg.author.id):
        return await msg.reply("❌ 权限不足。")

    await msg.reply("🔄 正在尝试重新启动机器人，请稍候...\n(若 10 秒内无响应请检查服务器控制台)")
    
    # 打印日志到控制台
    print(f"LOG - [管理员操作] 重启指令由 {msg.author.username} 发起")
    
    # 稍微延迟一下，确保“正在重启”的消息能成功发送到 KOOK 服务器
    await asyncio.sleep(2)
    
    python = sys.executable
    os.execv(python, [python] + sys.argv)


@bot.command(name='clean')
async def admin_clean_coupons(msg: Message):
    """清理已经使用过的废弃卡密，给数据库瘦身"""
    if not is_admin(msg.author.id): return
    
    conn = sqlite3.connect('bot_data.db')
    cursor = conn.cursor()
    # 仅删除已使用的卡密
    cursor.execute("DELETE FROM coupons WHERE used=1")
    deleted_count = cursor.rowcount
    conn.commit()
    conn.close()
    
    await msg.reply(f"🧹 数据库清理完毕，已永久删除 **{deleted_count}** 条失效的兑换码记录。")


@bot.command(name='check')
async def admin_check_user(msg: Message, target_id: str):
    if not is_admin(msg.author.id): return

    balance, checkin, model, status, ctx, banned, search, inv_count = get_user(target_id)
    info = (
        f"🔍 **查询** ({target_id})\n"
        f"💰 余额：{balance}\n"
        f"🚫 封禁：{'是' if banned else '否'}\n"
        f"🌐 联网：{'开' if search else '关'}\n"
        f"🎟️ 激活：{'邀请用户' if status==2 else ('普通' if status==1 else '未激活')}\n"
        f"🎁 邀请次数：{inv_count}/3"
    )
    await msg.reply(info)

@bot.command(name='reset')
async def admin_reset_checkin(msg: Message, target_id: str):
    """强制清除指定用户的签到记录，让他能再签一次"""
    if not is_admin(msg.author.id): return
    
    update_user(target_id, last_checkin="")
    await msg.reply(f"✅ 已重置用户 `{target_id}` 的签到状态。")

@bot.command(name='broadcast')
async def broadcast(msg: Message, *args):
    """管理员发布全局公告"""
    if not is_admin(msg.author.id): return
    
    content = " ".join(args)
    announcement = f"📢 **系统公告**\n━━━━━━━━━━━━━━━\n{content}"
    await msg.reply(announcement)
@bot.command(name='create')
async def create_coupon(msg: Message, amount: int = 800000, count: int = 1):
    if not is_admin(msg.author.id): return
    new_codes = []
    conn = sqlite3.connect('bot_data.db')
    cursor = conn.cursor()
    for _ in range(count):
        code = ''.join(random.choices(string.ascii_uppercase + string.digits, k=12))

        cursor.execute("INSERT INTO coupons VALUES (?, ?, 0, 0)", (code, amount))
        new_codes.append(code)
    conn.commit(); conn.close()


    # 组装回复消息
    codes_str = "\n".join([f"`{c}`" for c in new_codes])
    await msg.reply(f"✅ **成功生成 {count} 个兑换码**\n💰 面额：{amount}\n━━━━━━━━━━━━━━━\n{codes_str}\n━━━━━━━━━━━━━━━\n*请妥善保管，私发给用户使用。*")
    print(f"LOG - [管理员生成码] 用户: {msg.author.username} 数量: {count} 面额: {amount}")
@bot.command(name='ban')
async def admin_ban(msg: Message, target: str):
    if not is_admin(msg.author.id): return
    update_user(target, is_banned=1)
    await msg.reply(f"🚫 用户 `{target}` 已封禁。")
@bot.command(name='unban')
async def admin_unban(msg: Message, target: str):
    """解除用户封禁状态"""
    if not is_admin(msg.author.id): return
    
    update_user(target, is_banned=0)
    await msg.reply(f"🕊️ 用户 `{target}` 已解除封禁，恢复正常使用权限。")

@bot.command(name='usage')
async def admin_usage(msg: Message, hours: float = 1.0):
    if not is_admin(msg.author.id): return
    import time
    since = time.time() - (hours * 3600)
    conn = sqlite3.connect('bot_data.db')
    cursor = conn.cursor()
    cursor.execute("SELECT COUNT(*), SUM(cost) FROM usage_log WHERE timestamp > ?", (since,))
    count, total = cursor.fetchone()
    conn.close()
    await msg.reply(f"📊 **统计 ({hours}h)**\n次数：{count or 0}\n总消耗：{total or 0}")

@bot.command(name='rate')
async def admin_rate(msg: Message, model: str, rate: float):
    if not is_admin(msg.author.id): return
    MODEL_RATES[model] = rate
    await msg.reply(f"✅ `{model}` 费率设为 `{rate}x`。")
# ==========================================
#               🌐 旅法魔女的控制枢纽 (修复版)
# ==========================================
import requests, os, signal, sqlite3, json

app = Flask(__name__)

# --- 核心布局：定义一次，所有页面共享 ---
def render_witch_page(content_html, **kwargs):
    conf = load_config()
    # 基础框架包含侧边栏
    base_html = f'''
    <!DOCTYPE html>
    <html>
    <head>
        <meta charset="UTF-8">
        <title>旅法魔女的秘密工坊</title>
        <link href="https://cdn.bootcdn.net/ajax/libs/twitter-bootstrap/5.3.0/css/bootstrap.min.css" rel="stylesheet">
        <link href="https://cdn.bootcdn.net/ajax/libs/font-awesome/6.0.0/css/all.min.css" rel="stylesheet">
        <style>
            :root {{ --witch-purple: #9b87bd; --witch-dark: #1a1a2e; --witch-glass: rgba(255,255,255,0.05); }}
            body {{ background: #0f0f1a; color: #e6e1f0; font-family: serif; min-height: 100vh; margin: 0; }}
            .sidebar {{ width: 240px; position: fixed; top: 0; left: 0; height: 100vh; background: rgba(20,20,40,0.9); border-right: 1px solid var(--witch-purple); padding-top: 20px; z-index: 100; }}
            .main-content {{ margin-left: 240px; padding: 40px; }}
            .nav-link {{ color: #b5bac1; padding: 12px 25px; transition: 0.3s; border-left: 4px solid transparent; text-decoration: none; display: block; }}
            .nav-link:hover {{ background: var(--witch-glass); color: var(--witch-purple); }}
            .glass-card {{ background: var(--witch-glass); backdrop-filter: blur(10px); border: 1px solid rgba(155,135,189,0.2); border-radius: 15px; padding: 25px; margin-bottom: 25px; }}
            .form-control {{ background: rgba(0,0,0,0.3); border: 1px solid rgba(155,135,189,0.3); color: #fff; }}
            .form-control:focus {{ background: rgba(0,0,0,0.4); border-color: var(--witch-purple); color: #fff; box-shadow: none; }}
            .btn-magic {{ background: linear-gradient(45deg, #7b68ee, #9b87bd); border: none; color: white; padding: 10px 20px; border-radius: 8px; }}
            .log-area {{ background: #000; color: #0f0; font-family: monospace; height: 500px; overflow-y: auto; padding: 15px; border-radius: 10px; font-size: 0.9rem; border: 1px solid #333; }}
        </style>
    </head>
    <body>
        <div class="sidebar">
            <h4 class="text-center mb-4" style="color: var(--witch-purple)"><i class="fas fa-hat-wizard"></i> 工坊菜单</h4>
            <nav>
                <a class="nav-link" href="/"><i class="fas fa-home me-2"></i> 核心配置</a>
                <a class="nav-link" href="/users"><i class="fas fa-users me-2"></i> 旅人名册</a>
                <a class="nav-link" href="/models"><i class="fas fa-scroll me-2"></i> 魔法库</a>
                <a class="nav-link" href="/logs"><i class="fas fa-crystal-ball me-2"></i> 预言水晶</a>
                <hr style="opacity: 0.2">
                <a class="nav-link text-danger" href="javascript:restartBot()"><i class="fas fa-redo me-2"></i> 重启仪式</a>
            </nav>
        </div>
        <div class="main-content">
            {content_html}
        </div>
        <script>
            function restartBot() {{
                if(confirm("确定要重新启动机器人吗？")) {{
                    fetch('/api/restart', {{method: 'POST'}}).then(() => alert("🔮 信号已发出，请手动重启或等待自动拉起。"));
                }}
            }}
        </script>
    </body>
    </html>
    '''
    return render_template_string(base_html, c=conf, **kwargs)

# --- 1. 核心配置页面 ---
@app.route('/')
def index():
    content = '''
    <div class="glass-card">
        <h3>✨ 核心契约配置</h3>
        <form action="/save_config" method="POST" class="row g-3 mt-3">
            <div class="col-12"><label class="mb-2">KOOK Token</label><input type="text" name="KOOK_TOKEN" class="form-control" value="{{c.KOOK_TOKEN}}"></div>
            <div class="col-md-6"><label class="mb-2">API Key</label><input type="password" name="AI_API_KEY" class="form-control" value="{{c.AI_API_KEY}}"></div>
            <div class="col-md-6"><label class="mb-2">Base URL</label><input type="text" name="AI_BASE_URL" class="form-control" value="{{c.AI_BASE_URL}}"></div>
            <div class="col-12"><label class="mb-2">管理员 IDs (英文逗号分隔)</label><input type="text" name="ADMIN_IDS" class="form-control" value="{{c.ADMIN_IDS|join(',')}}"></div>
            <div class="col-12 mt-4"><button type="submit" class="btn btn-magic w-100">保存并铭刻</button></div>
        </form>
    </div>
    '''
    return render_witch_page(content)

# --- 2. 旅人名册 (数据库管理) ---
@app.route('/users')
def user_manager():
    conn = sqlite3.connect('bot_data.db')
    users = conn.execute("SELECT id, balance, model, is_banned FROM users").fetchall()
    conn.close()
    content = '''
    <div class="glass-card">
        <h3>👥 旅人名册</h3>
        <table class="table table-dark table-hover mt-3">
            <thead><tr><th>用户 ID</th><th>Token 余额</th><th>当前模型</th><th>状态</th><th>操作</th></tr></thead>
            <tbody>
                {% for u in users %}
                <tr>
                    <td>{{u[0]}}</td><td>{{u[1]}}</td><td>{{u[2]}}</td>
                    <td>{{ "🚫 封禁" if u[3] else "✅ 正常" }}</td>
                    <td><button class="btn btn-sm btn-outline-warning" onclick="editUser('{{u[0]}}', {{u[1]}})">充值</button></td>
                </tr>
                {% endfor %}
            </tbody>
        </table>
    </div>
    <script>
    function editUser(id, bal) {
        let n = prompt("请输入充值后的总余额:", bal);
        if(n !== null) {
            fetch('/api/update_user', {
                method:'POST', 
                headers:{'Content-Type':'application/json'},
                body: JSON.stringify({id:id, balance:n})
            }).then(() => location.reload());
        }
    }
    </script>
    '''
    return render_witch_page(content, users=users)

# --- 3. 魔法库 ---
@app.route('/models')
def model_settings():
    content = '''
    <div class="glass-card">
        <h3>📜 魔法库与费率</h3>
        <form action="/save_models" method="POST" class="row g-3 mt-3">
            <div class="col-md-12"><label class="mb-2">默认吟唱模型</label><input name="DEFAULT_MODEL" class="form-control" value="{{c.DEFAULT_MODEL}}"></div>
            <div class="col-12 mt-3">
                <label class="mb-2">模型费率 (每行一个，格式：模型名:倍率)</label>
                <textarea name="MODEL_RATES_TEXT" class="form-control" rows="8">{% for k,v in rates.items() %}{{k}}:{{v}}\n{% endfor %}</textarea>
                <p class="small text-muted mt-2">例如：gpt-4o:2.0 (表示2倍计费)</p>
            </div>
            <button type="submit" class="btn btn-magic mt-4">更新魔法库</button>
        </form>
    </div>
    '''
    return render_witch_page(content, rates=MODEL_RATES)

# --- 4. 预言水晶 (日志) ---
@app.route('/logs')
def view_logs():
    logs = list(log_queue.queue) if 'log_queue' in globals() else ["日志系统未初始化"]
    content = '''
    <div class="glass-card">
        <h3>🔮 预言水晶 (实时日志)</h3>
        <div class="log-area" id="logBox">
            {% for l in logs %}{{l}}<br>{% endfor %}
        </div>
        <button class="btn btn-sm btn-outline-secondary mt-3" onclick="location.reload()">手动刷新</button>
    </div>
    '''
    return render_witch_page(content, logs=logs)

# --- 后端 API ---
@app.route('/save_config', methods=['POST'])
def save_conf():
    conf = load_config()
    conf['KOOK_TOKEN'] = request.form.get('KOOK_TOKEN')
    conf['AI_API_KEY'] = request.form.get('AI_API_KEY')
    conf['AI_BASE_URL'] = request.form.get('AI_BASE_URL')
    conf['ADMIN_IDS'] = [i.strip() for i in request.form.get('ADMIN_IDS').split(',') if i.strip()]
    save_config(conf)
    return redirect('/')

@app.route('/save_models', methods=['POST'])
def save_models_logic():
    global MODEL_RATES
    conf = load_config()
    conf['DEFAULT_MODEL'] = request.form.get('DEFAULT_MODEL')
    
    # 解析费率文本
    new_rates = {}
    lines = request.form.get('MODEL_RATES_TEXT').split('\n')
    for line in lines:
        if ':' in line:
            name, val = line.split(':')
            try: new_rates[name.strip()] = float(val.strip())
            except: continue
    
    MODEL_RATES = new_rates
    # 这里建议将 MODEL_RATES 也持久化到 config.json
    conf['MODEL_RATES'] = new_rates
    save_config(conf)
    return redirect('/models')

@app.route('/api/update_user', methods=['POST'])
def update_user_api():
    data = request.json
    conn = sqlite3.connect('bot_data.db')
    conn.execute("UPDATE users SET balance = ? WHERE id = ?", (data['balance'], data['id']))
    conn.commit(); conn.close()
    return {"status": "ok"}

@app.route('/api/restart', methods=['POST'])
def restart():
    # 延迟1秒关闭，给前端反馈时间
    threading.Timer(1, lambda: os.kill(os.getpid(), signal.SIGTERM)).start()
    return {"status": "ok"}

def run_flask():
    app.run(host='0.0.0.0', port=2013, threaded=True)

# ==========================================
#               💬 AI 对话逻辑
# ==========================================
async def ai_response_logic(msg: Message, content: str):
    import time
    user_id = msg.author.id
    
    # 获取用户数据
    balance, _, model, _, use_context, is_banned, use_search, _ = get_user(user_id)
    
    if is_banned: 
        return await msg.reply("🚫 你的账号已被封禁，无法使用。")
    if balance <= 0: 
        return await msg.reply("⚠️ 余额不足，请签到或兑换。")
    if not content.strip(): 
        return

    try:
        # 1. 构建消息上下文
        # 这里的 SYSTEM_PROMPT 和 user_history 现在可以正常访问了
        sys_content = SYSTEM_PROMPT["content"]
        if use_search: 
            sys_content += " 请联网搜索最新信息回答。"
        
        messages = [{"role": "system", "content": sys_content}]
        
        if use_context == 1:
            # 从全局变量获取历史记录
            history = user_history.get(user_id, [])
            messages.extend(history)
            
        messages.append({"role": "user", "content": content})

        # 2. 调用 API
        response = await aclient.chat.completions.create(
            model=model, 
            messages=messages, 
            timeout=60.0
        )
        
        answer = response.choices[0].message.content
        tokens = response.usage.total_tokens
        
        # 3. 计费逻辑
        rate = MODEL_RATES.get(model, 1.0)
        if use_search: rate *= 1.1
        cost = int(tokens * rate)
        
        # 4. 更新数据库与历史记录
        update_user(user_id, balance=max(0, balance - cost))
        
        if use_context == 1:
            if user_id not in user_history: 
                user_history[user_id] = []
            user_history[user_id].append({"role": "user", "content": content})
            user_history[user_id].append({"role": "assistant", "content": answer})
            # 保留最近的轮数
            user_history[user_id] = user_history[user_id][-MAX_HISTORY_TURNS:]

        await msg.reply(f"{answer}\n\n🔸 消耗：{cost} | 剩余：{max(0, balance - cost)}")

    except Exception as e:
        await msg.reply(f"❌ 调用出错：{str(e)}")

# ==========================================
#               🚀 启动
# ==========================================
if __name__ == '__main__':
    init_db()
    
    # 1. 启动网页线程（非守护线程，确保它能独立存活）
    print("🌐 配置面板已启动: http://127.0.0.1:2013")
    web_thread = threading.Thread(target=run_flask)
    web_thread.setDaemon(True) 
    web_thread.start()

    # 2. 检查 Token 是否有效
    conf = load_config()
    token = conf.get('KOOK_TOKEN', "")
    
    # 如果还是默认提示词或者是空的，就先不启动机器人，只跑网页
    if "填写" in token or not token:
        print("⚠️ 检测到尚未配置有效的 KOOK Token。")
        print("📢 请访问 http://127.0.0.1:2013 完成配置，然后手动重启程序。")
        # 让主线程进入无限循环，防止程序退出，从而保护网页线程
        try:
            while True:
                time.sleep(10)
        except KeyboardInterrupt:
            print("程序已关闭。")
    else:
        # 3. 只有在有 Token 的情况下才尝试运行机器人，并增加异常捕获
        print("🚀 正在尝试连接至 KOOK...")
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            bot.run()
        except Exception as e:
            print(f"❌ 机器人启动失败: {e}")
            print("📢 进程将继续运行，你可以通过网页面板修改配置后重启。")
            # 即使机器人报错崩溃，主线程也保持存活，让网页面板可以继续访问
            while True:
                time.sleep(10)


