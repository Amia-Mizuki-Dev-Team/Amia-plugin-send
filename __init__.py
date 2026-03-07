import os
import time
from datetime import datetime
import aiosqlite

from nonebot import on_command, on_message, get_driver
from nonebot.adapters.onebot.v11 import Bot, GroupMessageEvent, PrivateMessageEvent, Message
from nonebot.permission import SUPERUSER
from nonebot.plugin import PluginMetadata
from nonebot.matcher import Matcher

# ==========================================
#      ✨ 插件元数据 & 帮助信息 ✨
# ==========================================
__plugin_meta__ = PluginMetadata(
    name="看看群U发言(Pro Max)",
    description="全能群聊数据统计，支持日/月/年维度的流量分析",
    usage="""
📊【看看群U发言 Pro Max - 使用帮助】

👥 群友指令：
1. 今日发言 / 本月发言 / 今年发言
   ➤ 查看本群内的龙王排行榜 (Top 10)

👑 管理员指令 (Superuser)：
1. 今日DAU / 本月DAU / 今年DAU
   ➤ 查看实时数据、流量、全局活跃榜单
""".strip(),
    type="application",
    supported_adapters={"~onebot.v11"},
)

# === 数据库配置 ===
DB_PATH = os.path.join(os.path.dirname(__file__), "data.db")

# =======================
#      第一部分：数据库核心
# =======================

driver = get_driver()

@driver.on_startup
async def init_db():
    """在 NoneBot 启动时异步初始化数据库"""
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute('''CREATE TABLE IF NOT EXISTS msg_stats
                     (date TEXT, group_id TEXT, user_id TEXT, count INTEGER, 
                     PRIMARY KEY (date, group_id, user_id))''')
        
        await db.execute('''CREATE TABLE IF NOT EXISTS private_stats
                     (date TEXT, user_id TEXT, count INTEGER,
                     PRIMARY KEY (date, user_id))''')

        await db.execute('''CREATE TABLE IF NOT EXISTS hourly_stats
                     (date TEXT, hour INTEGER, count INTEGER,
                     PRIMARY KEY (date, hour))''')
                     
        # 流量统计表 (改为 total_bytes)
        await db.execute('''CREATE TABLE IF NOT EXISTS traffic_stats
                     (date TEXT PRIMARY KEY, total_bytes INTEGER)''')
                     
        await db.commit()

def calculate_message_bytes(message: Message) -> int:
    """精准估算消息流量 (字节)"""
    total_bytes = 0
    for seg in message:
        if seg.type == "text":
            # 文本按 UTF-8 字节计算 (一个汉字约 3 字节)
            total_bytes += len(seg.data.get("text", "").encode('utf-8'))
        elif seg.type == "image":
            # 图片估算为 500KB
            total_bytes += 500 * 1024 
        elif seg.type == "record":
            # 语音估算为 50KB
            total_bytes += 50 * 1024
        elif seg.type == "video":
            # 视频估算为 2MB
            total_bytes += 2 * 1024 * 1024
        else:
            # 其他特殊 CQ 码按字符串字节算
            total_bytes += len(str(seg).encode('utf-8'))
    return total_bytes

async def record_group_msg(group_id: str, user_id: str, msg_bytes: int):
    """异步记录群消息，合并为单次事务"""
    now = datetime.now()
    today = now.strftime("%Y-%m-%d")
    hour = now.hour
    
    async with aiosqlite.connect(DB_PATH) as db:
        # 记录消息数
        await db.execute("INSERT OR IGNORE INTO msg_stats VALUES (?, ?, ?, 0)", (today, group_id, user_id))
        await db.execute("UPDATE msg_stats SET count = count + 1 WHERE date=? AND group_id=? AND user_id=?", 
                         (today, group_id, user_id))
        # 记录时段
        await db.execute("INSERT OR IGNORE INTO hourly_stats VALUES (?, ?, 0)", (today, hour))
        await db.execute("UPDATE hourly_stats SET count = count + 1 WHERE date=? AND hour=?", (today, hour))
        # 记录流量
        await db.execute("INSERT OR IGNORE INTO traffic_stats VALUES (?, 0)", (today,))
        await db.execute("UPDATE traffic_stats SET total_bytes = total_bytes + ? WHERE date=?", (msg_bytes, today))
        
        await db.commit()

async def record_private_msg(user_id: str, msg_bytes: int):
    """异步记录私聊消息，合并为单次事务"""
    now = datetime.now()
    today = now.strftime("%Y-%m-%d")
    hour = now.hour

    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("INSERT OR IGNORE INTO private_stats VALUES (?, ?, 0)", (today, user_id))
        await db.execute("UPDATE private_stats SET count = count + 1 WHERE date=? AND user_id=?", 
                         (today, user_id))
        await db.execute("INSERT OR IGNORE INTO hourly_stats VALUES (?, ?, 0)", (today, hour))
        await db.execute("UPDATE hourly_stats SET count = count + 1 WHERE date=? AND hour=?", (today, hour))
        await db.execute("INSERT OR IGNORE INTO traffic_stats VALUES (?, 0)", (today,))
        await db.execute("UPDATE traffic_stats SET total_bytes = total_bytes + ? WHERE date=?", (msg_bytes, today))
        
        await db.commit()

# --- 数据查询接口 ---

async def get_group_rank(group_id: str, mode: str):
    """异步获取单群排行榜 (Top 10)"""
    today = datetime.now().strftime("%Y-%m-%d")
    month = datetime.now().strftime("%Y-%m")
    year = datetime.now().strftime("%Y")

    sql, params = "", ()
    if mode == "day":
        sql = "SELECT user_id, count FROM msg_stats WHERE group_id=? AND date=? ORDER BY count DESC LIMIT 10"
        params = (group_id, today)
    elif mode == "month":
        sql = "SELECT user_id, SUM(count) as total FROM msg_stats WHERE group_id=? AND date LIKE ? GROUP BY user_id ORDER BY total DESC LIMIT 10"
        params = (group_id, f"{month}%")
    elif mode == "year":
        sql = "SELECT user_id, SUM(count) as total FROM msg_stats WHERE group_id=? AND date LIKE ? GROUP BY user_id ORDER BY total DESC LIMIT 10"
        params = (group_id, f"{year}%")

    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(sql, params) as cursor:
            return await cursor.fetchall()

async def get_admin_dashboard_data(mode: str = "day"):
    """异步获取管理员面板数据"""
    today = datetime.now().strftime("%Y-%m-%d")
    month = datetime.now().strftime("%Y-%m")
    year = datetime.now().strftime("%Y")
    
    date_condition, params = "", ()
    if mode == "day":
        date_condition, params = "date=?", (today,)
    elif mode == "month":
        date_condition, params = "date LIKE ?", (f"{month}%",)
    elif mode == "year":
        date_condition, params = "date LIKE ?", (f"{year}%",)

    data = {}
    async with aiosqlite.connect(DB_PATH) as db:
        # 活跃用户
        async with db.execute(f"SELECT COUNT(DISTINCT user_id) FROM msg_stats WHERE {date_condition}", params) as c:
            group_u = (await c.fetchone())[0] or 0
        async with db.execute(f"SELECT COUNT(DISTINCT user_id) FROM private_stats WHERE {date_condition}", params) as c:
            priv_u = (await c.fetchone())[0] or 0
        data['active_users'] = group_u + priv_u 
        
        # 活跃群聊
        async with db.execute(f"SELECT COUNT(DISTINCT group_id) FROM msg_stats WHERE {date_condition}", params) as c:
            data['active_groups'] = (await c.fetchone())[0] or 0
        
        # 消息数
        async with db.execute(f"SELECT SUM(count) FROM msg_stats WHERE {date_condition}", params) as c:
            data['total_group_msg'] = (await c.fetchone())[0] or 0
        async with db.execute(f"SELECT SUM(count) FROM private_stats WHERE {date_condition}", params) as c:
            data['total_private_msg'] = (await c.fetchone())[0] or 0
        data['total_all_msg'] = data['total_group_msg'] + data['total_private_msg']

        # 流量 (字节)
        async with db.execute(f"SELECT SUM(total_bytes) FROM traffic_stats WHERE {date_condition}", params) as c:
            data['total_bytes'] = (await c.fetchone())[0] or 0

        # 最活跃时段
        if mode == "day":
            async with db.execute("SELECT hour, count FROM hourly_stats WHERE date=? ORDER BY count DESC LIMIT 1", (today,)) as c:
                peak = await c.fetchone()
                data['peak_str'] = f"{peak[0]}点 ({peak[1]}条)" if peak else "无数据"
        else:
            days_passed = int(datetime.now().day) if mode == "month" else int(datetime.now().strftime("%j"))
            avg_msg = int(data['total_all_msg'] / max(1, days_passed))
            data['peak_str'] = f"日均 {avg_msg} 条"

        # 最活跃群组 Top 10
        async with db.execute(f"SELECT group_id, SUM(count) as total FROM msg_stats WHERE {date_condition} GROUP BY group_id ORDER BY total DESC LIMIT 10", params) as c:
            data['top_groups'] = await c.fetchall()

        # 最活跃用户 Top 10
        async with db.execute(f"SELECT user_id, SUM(count) as total FROM msg_stats WHERE {date_condition} GROUP BY user_id ORDER BY total DESC LIMIT 10", params) as c:
            data['top_users'] = await c.fetchall()
            
    return data

def format_number(num: int):
    if num >= 10000:
        return f"{num/10000:.1f}w"
    return str(num)

def format_traffic(bytes_size: int) -> str:
    """格式化流量显示"""
    if bytes_size < 1024:
        return f"{bytes_size} B"
    elif bytes_size < 1024 * 1024:
        return f"{bytes_size / 1024:.2f} KB"
    elif bytes_size < 1024 * 1024 * 1024:
        return f"{bytes_size / (1024 * 1024):.2f} MB"
    else:
        return f"{bytes_size / (1024 * 1024 * 1024):.2f} GB"

# =======================
#      第二部分：监听逻辑
# =======================

group_recorder = on_message(priority=0, block=False)
@group_recorder.handle()
async def _(event: GroupMessageEvent):
    msg_bytes = calculate_message_bytes(event.message)
    await record_group_msg(str(event.group_id), str(event.user_id), msg_bytes)

private_recorder = on_message(priority=0, block=False)
@private_recorder.handle()
async def _(event: PrivateMessageEvent):
    msg_bytes = calculate_message_bytes(event.message)
    await record_private_msg(str(event.user_id), msg_bytes)


# =======================
#      第三部分：指令逻辑
# =======================

# --- 普通群友指令 ---
cmd_day = on_command("今日发言", aliases={"今日排行榜"}, priority=5, block=True)
cmd_month = on_command("本月发言", aliases={"本月排行榜"}, priority=5, block=True)
cmd_year = on_command("今年发言", aliases={"今年排行榜"}, priority=5, block=True)

# 【核心修复】：传入 matcher，避免上下文报错
async def send_group_rank(bot: Bot, event: GroupMessageEvent, matcher: Matcher, mode: str, title: str):
    group_id = str(event.group_id)
    data = await get_group_rank(group_id, mode) # 异步调用
    
    if not data:
        await matcher.finish(f"📊 {title}\n" + "-"*15 + "\n暂无数据，快来水群！")

    msg = [f"📊 {title} (Top 10)", "-" * 20]
    for i, (uid, count) in enumerate(data):
        rank = i + 1
        icon = {1: "🥇", 2: "🥈", 3: "🥉"}.get(rank, f"{rank}.")
        try:
            info = await bot.get_group_member_info(group_id=int(group_id), user_id=int(uid))
            name = info.get("card") or info.get("nickname") or str(uid)
        except:
            name = str(uid)
        msg.append(f"{icon} {name} ({count})")
    
    msg.append("-" * 20)
    msg.append(f"⏱ 统计时间: {datetime.now().strftime('%H:%M')}")
    await matcher.finish("\n".join(msg)) # 使用当前触发的 matcher 发送

@cmd_day.handle()
async def _(bot: Bot, event: GroupMessageEvent, matcher: Matcher): 
    await send_group_rank(bot, event, matcher, "day", "今日龙王榜")
    
@cmd_month.handle()
async def _(bot: Bot, event: GroupMessageEvent, matcher: Matcher): 
    await send_group_rank(bot, event, matcher, "month", "本月龙王榜")
    
@cmd_year.handle()
async def _(bot: Bot, event: GroupMessageEvent, matcher: Matcher): 
    await send_group_rank(bot, event, matcher, "year", "年度龙王榜")


# --- 超级管理员指令 (高端面板) ---
admin_day = on_command("今日DAU", aliases={"全群统计", "bot数据"}, permission=SUPERUSER, priority=1, block=True)
admin_month = on_command("本月DAU", aliases={"本月统计"}, permission=SUPERUSER, priority=1, block=True)
admin_year = on_command("今年DAU", aliases={"年度统计"}, permission=SUPERUSER, priority=1, block=True)

async def send_admin_dashboard(bot: Bot, matcher: Matcher, mode: str, title_prefix: str):
    start_time = time.time()
    
    # 1. 异步获取数据
    data = await get_admin_dashboard_data(mode)
    
    # 2. 格式化流量
    traffic_str = format_traffic(data['total_bytes'])

    msg = []
    msg.append(f"📊 {title_prefix} 活跃概览")
    msg.append(f"👥 活跃群聊: {data['active_groups']}")
    msg.append(f"👤 活跃用户: {format_number(data['active_users'])}")
    msg.append(f"💬 消息总数: {format_number(data['total_all_msg'])}")
    msg.append(f"📡 流量记录: {traffic_str} (估算)")
    
    if mode == "day":
        msg.append(f"⏰ 爆发时段: {data['peak_str']}")
    else:
        msg.append(f"📅 平均热度: {data['peak_str']}")
        
    msg.append("") 

    msg.append(f"🔝 最活跃群组 (Top 10):")
    for i, (gid, count) in enumerate(data['top_groups']):
        try:
            g_info = await bot.get_group_info(group_id=int(gid))
            g_name = g_info.get("group_name", str(gid))
        except:
            g_name = "未知群聊"
        msg.append(f"{i+1}. {g_name} ({gid}) - {format_number(count)}")
        
    msg.append("") 

    msg.append(f"👑 全局卷王 (Top 10):")
    for i, (uid, count) in enumerate(data['top_users']):
        try:
            u_info = await bot.get_stranger_info(user_id=int(uid))
            u_name = u_info.get("nickname", str(uid))
        except:
            u_name = "未知用户"
        msg.append(f"{i+1}. {u_name} ({uid}) - {format_number(count)}")

    end_time = time.time()
    cost_ms = int((end_time - start_time) * 1000)
    
    msg.append("")
    msg.append(f"⏱ 查询: {cost_ms}ms | 源: aiosqlite")
    
    await matcher.finish("\n".join(msg))

@admin_day.handle()
async def _(bot: Bot, matcher: Matcher):
    today_str = datetime.now().strftime("%m-%d")
    await send_admin_dashboard(bot, matcher, "day", f"{today_str} 今日")

@admin_month.handle()
async def _(bot: Bot, matcher: Matcher):
    month_str = datetime.now().strftime("%Y-%m")
    await send_admin_dashboard(bot, matcher, "month", f"{month_str} 本月")

@admin_year.handle()
async def _(bot: Bot, matcher: Matcher):
    year_str = datetime.now().strftime("%Y年")
    await send_admin_dashboard(bot, matcher, "year", f"{year_str} 年度")
