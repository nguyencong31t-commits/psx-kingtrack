import asyncio
import os
import sys
import re
import html
import logging

if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

from dotenv import load_dotenv
from telegram import (
    Update, 
    BotCommand, 
    ForceReply, 
    InlineKeyboardMarkup, 
    InlineKeyboardButton
)
from telegram.constants import ParseMode
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ContextTypes,
    filters,
)

from database import (
    init_db,
    add_or_update_tracking,
    get_active_trackings_by_chat,
    remove_tracking,
    get_all_active_trackings,
    update_tracking_status,
)
from spx_service import (
    fetch_spx_tracking, 
    format_timestamp, 
    format_spx_card, 
    format_full_journey,
    translate_spx_status
)

# Tải biến môi trường
load_dotenv()
BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
POLL_INTERVAL_MINUTES = int(os.getenv("POLL_INTERVAL_MINUTES", "20"))

# Cấu hình logging
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO
)
logger = logging.getLogger(__name__)

import threading
from http.server import HTTPServer, BaseHTTPRequestHandler

# Regex phát hiện mã vận đơn SPX (bắt đầu bằng SPXVN hoặc VN theo sau là số/chữ)
SPX_REGEX = re.compile(r'(SPXVN[A-Za-z0-9]+)', re.IGNORECASE)

class HealthCheckHandler(BaseHTTPRequestHandler):
    """Máy chủ HTTP mini trả về HTML và hỗ trợ HEAD/GET cho UptimeRobot, Render, Koyeb."""
    def do_HEAD(self):
        self.send_response(200)
        self.send_header("Content-type", "text/html; charset=utf-8")
        self.end_headers()

    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-type", "text/html; charset=utf-8")
        self.end_headers()
        html = """<!DOCTYPE html>
<html>
<head>
    <meta charset="utf-8">
    <title>SPX Bot Status</title>
    <style>
        body { font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif; display: flex; justify-content: center; align-items: center; height: 100vh; margin: 0; background: #0f172a; color: #f8fafc; }
        .card { text-align: center; padding: 40px; background: #1e293b; border-radius: 16px; box-shadow: 0 10px 25px rgba(0,0,0,0.3); }
        .badge { display: inline-block; padding: 6px 16px; border-radius: 20px; background: #10b981; color: #fff; font-weight: bold; margin-bottom: 16px; }
    </style>
</head>
<body>
    <div class="card">
        <div class="badge">SYSTEM ONLINE</div>
        <h2>SPX Telegram Tracking Bot</h2>
        <p style="color: #94a3b8;">Bot is actively running and polling orders.</p>
    </div>
</body>
</html>"""
        self.wfile.write(html.encode("utf-8"))

    def log_message(self, format, *args):
        pass

def start_health_check_server():
    port = int(os.getenv("PORT", 8000))
    try:
        server = HTTPServer(("0.0.0.0", port), HealthCheckHandler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        logger.info(f"Health check server running on port {port}")
    except Exception as e:
        logger.warning(f"Could not start health check server on port {port}: {e}")

async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Lệnh /start."""
    welcome_text = (
        "🚚 *XIN CHÀO! ĐÂY LÀ BOT THEO DÕI ĐƠN HÀNG SPX EXPRESS*\n\n"
        "✨ *Cách dùng cực kỳ tiện lợi:*\n"
        "👉 Bạn chỉ cần **gửi trực tiếp mã vận đơn** (ví dụ: `SPXVN045388374941`) hoặc kèm tên gợi nhớ (ví dụ: `SPXVN045388374941 Giày Nike`), bot sẽ tự động nhận diện và bật chế độ theo dõi ngay lập tức!\n\n"
        "📌 *Danh sách lệnh:*\n"
        "🔹 `/list` - Xem các đơn hàng đang theo dõi\n"
        "🔹 `/untrack <mã>` - Hủy theo dõi một đơn hàng\n"
        "🔹 `/check <mã>` - Tra cứu tức thì 1 lần (không lưu)\n"
        "🔹 `/help` - Xem lại hướng dẫn\n\n"
        "🔔 *Cơ chế hoạt động:* Bot sẽ định kỳ quét hành trình từ máy chủ SPX. Ngay khi bưu kiện di chuyển qua kho mới, bạn sẽ nhận được tin nhắn báo ngay lập tức!"
    )
    await update.message.reply_text(welcome_text, parse_mode=ParseMode.MARKDOWN)

async def process_tracking_request(update: Update, tracking_number: str, label: str):
    """Xử lý tra cứu và lưu đơn vào database."""
    chat_id = update.effective_chat.id
    status_msg = await update.message.reply_text("⏳ Đang kết nối máy chủ SPX tra cứu hành trình...")

    # Gọi API SPX trong worker thread để tránh block asyncio loop
    loop = asyncio.get_running_loop()
    info = await loop.run_in_executor(None, fetch_spx_tracking, tracking_number)

    if not info:
        await status_msg.edit_text(
            f"❌ Không tìm thấy thông tin cho mã: `{tracking_number}`.\n\n"
            "⚠️ *Gợi ý kiểm tra:*\n"
            "1. Đảm bảo mã vận đơn chính xác (thường bắt đầu bằng `SPXVN...`).\n"
            "2. Nếu đơn vừa mới đặt trên Shopee, bưu cục có thể chưa kịp nhập dữ liệu lên hệ thống SPX.",
            parse_mode=ParseMode.MARKDOWN
        )
        return

    latest_event = info.get("latest_event") or {}
    card = format_spx_card(info["tracking_number"], label, latest_event)

    # Nếu đơn hàng đã giao thành công hoặc đã hủy: Không lưu vào theo dõi (tự động untrack)
    if info.get("is_completed"):
        await remove_tracking(chat_id, info["tracking_number"])
        if info.get("is_delivered"):
            card += "\n\n🎉 ĐƠN HÀNG ĐÃ GIAO THÀNH CÔNG!\n✅ Đơn hàng đã hoàn tất nên bot không cần theo dõi nữa."
        else:
            card += "\n\n⚠️ ĐƠN HÀNG ĐÃ BỊ HỦY HOẶC CHUYỂN HOÀN!\n✅ Đơn hàng đã kết thúc nên bot không cần theo dõi nữa."
        await status_msg.edit_text(card)
        return

    # Lưu vào CSDL cho các đơn đang trên đường vận chuyển
    await add_or_update_tracking(
        chat_id=chat_id,
        tracking_number=info["tracking_number"],
        label=label,
        status=info["latest_status"],
        timestamp=info["timestamp"]
    )

    card += "\n\n🔔 Bot đã bật tự động theo dõi cho đơn hàng này!"
    await status_msg.edit_text(card)

async def send_full_journey(chat_id: int, code: str, context: ContextTypes.DEFAULT_TYPE, label: str = ""):
    """Tra cứu và gửi toàn bộ hành trình chi tiết của đơn hàng."""
    status_msg = await context.bot.send_message(
        chat_id=chat_id, 
        text=f"⏳ Đang tải toàn bộ hành trình cho mã <code>{html.escape(code)}</code>...", 
        parse_mode=ParseMode.HTML
    )
    loop = asyncio.get_running_loop()
    info = await loop.run_in_executor(None, fetch_spx_tracking, code)

    if not info:
        await status_msg.edit_text(f"❌ Không tìm thấy thông tin cho mã: <code>{html.escape(code)}</code>.", parse_mode=ParseMode.HTML)
        return

    full_journey_text = format_full_journey(info, label)
    try:
        await status_msg.edit_text(full_journey_text, parse_mode=ParseMode.HTML)
    except Exception as e:
        logger.warning(f"Lỗi gửi HTML full journey: {e}, fallback text thường")
        clean = re.sub(r'<[^>]+>', '', full_journey_text)
        await status_msg.edit_text(clean)

async def track_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Lệnh /track [mã] [tên_gợi_nhớ]: Đăng ký theo dõi đơn hàng tự động."""
    if not context.args:
        await update.message.reply_text(
            "➕ <b>THEO DÕI ĐƠN HÀNG TỰ ĐỘNG</b>\n\n"
            "👇 <i>Dán mã SPXVN... (kèm tên gợi nhớ nếu có) vào đây:</i>",
            reply_markup=ForceReply(selective=True, input_field_placeholder="Ví dụ: SPXVN06479385806A Áo thun"),
            parse_mode=ParseMode.HTML
        )
        return

    tracking_number = context.args[0].strip().upper()
    label = " ".join(context.args[1:]).strip() if len(context.args) > 1 else ""
    await process_tracking_request(update, tracking_number, label)

async def check_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Lệnh /check [mã]: Tra cứu toàn bộ hành trình chi tiết."""
    if not context.args:
        await update.message.reply_text(
            "🔍 <b>TRA CỨU TOÀN BỘ HÀNH TRÌNH</b>\n\n"
            "👇 <i>Dán mã SPXVN... cần tra cứu vào đây:</i>",
            reply_markup=ForceReply(selective=True, input_field_placeholder="Dán mã SPXVN... vào đây"),
            parse_mode=ParseMode.HTML
        )
        return

    code = context.args[0].strip().upper()
    await send_full_journey(update.effective_chat.id, code, context)

async def list_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Lệnh /list liệt kê các đơn đang chờ giao kèm nút bấm xem hành trình."""
    try:
        chat_id = update.effective_chat.id
        rows = await get_active_trackings_by_chat(chat_id)

        if not rows:
            await update.message.reply_text(
                "📭 Hiện tại bạn không có đơn hàng nào đang theo dõi.\n"
                "👉 Hãy gửi mã vận đơn <code>SPXVN...</code> để thêm đơn nhé!",
                parse_mode=ParseMode.HTML
            )
            return

        lines = [f"📋 <b>DANH SÁCH ĐƠN ĐANG THEO DÕI ({len(rows)} đơn):</b>\n"]
        keyboard = []

        for idx, r in enumerate(rows, 1):
            t_str = format_timestamp(r["last_timestamp"])
            code = r["tracking_number"]
            lbl = (r["label"] or "").strip()
            lbl_escaped = html.escape(lbl)
            label_part = f" ({lbl_escaped})" if lbl_escaped and lbl_escaped.lower() not in ("đơn hàng spx", "đơn spx", "none", "") else ""
            status_text = html.escape(translate_spx_status(r["last_status"] or ""))
            code_escaped = html.escape(code)
            
            lines.append(
                f"<b>{idx}.</b> <code>{code_escaped}</code>{label_part}\n"
                f"   📌 Trạng thái: {status_text}\n"
                f"   🕒 Cập nhật: {t_str}\n"
                f"   👉 Xem hành trình: /check_{code}\n"
            )

            # Thêm nút bấm tương tác cho từng đơn: Bấm là ra full hành trình ngay!
            btn_name = f"🔍 {code}" + (f" ({lbl[:8]}..)" if len(lbl) > 8 else f" ({lbl})" if lbl else "")
            keyboard.append([
                InlineKeyboardButton(btn_name, callback_data=f"journey:{code}"),
                InlineKeyboardButton("🗑 Hủy", callback_data=f"untrack:{code}")
            ])

        lines.append("<i>💡 Bấm thẳng vào nút trên hoặc link lệnh /check_... để xem full hành trình!</i>")
        full_text = "\n".join(lines)
        reply_markup = InlineKeyboardMarkup(keyboard)

        try:
            await update.message.reply_text(full_text, reply_markup=reply_markup, parse_mode=ParseMode.HTML)
        except Exception as send_err:
            logger.warning(f"Lỗi gửi HTML trong /list ({send_err}), fallback sang text thường")
            clean_text = re.sub(r'<[^>]+>', '', full_text).replace('&lt;', '<').replace('&gt;', '>').replace('&amp;', '&')
            await update.message.reply_text(clean_text, reply_markup=reply_markup)
    except Exception as e:
        logger.error(f"Lỗi list_command: {e}", exc_info=True)
        await update.message.reply_text("⚠️ Có lỗi xảy ra khi lấy danh sách đơn. Vui lòng thử lại sau!")

async def untrack_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Lệnh /untrack [mã]: Hiển thị danh sách các đơn đang theo dõi kèm nút Hủy trực tiếp bên cạnh."""
    chat_id = update.effective_chat.id

    # 1. Nếu người dùng nhập mã trực tiếp: /untrack SPXVN...
    if context.args:
        code = context.args[0].strip().upper()
        success = await remove_tracking(chat_id, code)
        code_escaped = html.escape(code)
        if success:
            await update.message.reply_text(f"🗑 Đã dừng theo dõi đơn <code>{code_escaped}</code> thành công.", parse_mode=ParseMode.HTML)
        else:
            await update.message.reply_text(f"❓ Không tìm thấy mã <code>{code_escaped}</code> trong danh sách theo dõi của bạn.", parse_mode=ParseMode.HTML)
        return

    # 2. Nếu không nhập mã: Tự động liệt kê danh sách đơn kèm từng nút Remove (Hủy) bên cạnh
    rows = await get_active_trackings_by_chat(chat_id)
    if not rows:
        await update.message.reply_text(
            "📭 Hiện tại bạn không có đơn hàng nào đang theo dõi để hủy.",
            parse_mode=ParseMode.HTML
        )
        return

    lines = [
        "🗑 <b>CHỌN ĐƠN HÀNG CẦN HỦY THEO DÕI:</b>",
        "━━━━━━━━━━━━━━━━━━━━━━",
    ]
    keyboard = []

    for idx, r in enumerate(rows, 1):
        code = r["tracking_number"]
        lbl = (r["label"] or "").strip()
        lbl_escaped = html.escape(lbl)
        label_part = f" ({lbl_escaped})" if lbl_escaped and lbl_escaped.lower() not in ("đơn hàng spx", "đơn spx", "none", "") else ""
        status_text = html.escape(translate_spx_status(r["last_status"] or ""))
        
        lines.append(
            f"<b>{idx}.</b> <code>{html.escape(code)}</code>{label_part}\n"
            f"   📌 Trạng thái: {status_text}\n"
        )

        btn_label = f"{code}" + (f" ({lbl[:8]}..)" if len(lbl) > 8 else f" ({lbl})" if lbl else "")
        keyboard.append([
            InlineKeyboardButton(f"📦 {btn_label}", callback_data=f"journey:{code}"),
            InlineKeyboardButton("❌ Hủy (Untrack)", callback_data=f"untrack_btn:{code}")
        ])

    lines.append("👇 <i>Bấm vào nút <b>[❌ Hủy (Untrack)]</b> tương ứng để dừng theo dõi:</i>")
    full_text = "\n".join(lines)
    reply_markup = InlineKeyboardMarkup(keyboard)

    try:
        await update.message.reply_text(full_text, reply_markup=reply_markup, parse_mode=ParseMode.HTML)
    except Exception as e:
        logger.warning(f"Lỗi gửi HTML trong /untrack: {e}")
        clean_text = re.sub(r'<[^>]+>', '', full_text).replace('&lt;', '<').replace('&gt;', '>').replace('&amp;', '&')
        await update.message.reply_text(clean_text, reply_markup=reply_markup)

async def handle_callback_query(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Xử lý sự kiện khi người dùng bấm vào các nút Inline Keyboard."""
    query = update.callback_query
    data = query.data or ""
    chat_id = update.effective_chat.id

    if data.startswith("journey:"):
        code = data.split("journey:")[1].strip().upper()
        await query.answer("⏳ Đang tải toàn bộ hành trình...")
        await send_full_journey(chat_id, code, context)
    elif data.startswith("untrack_btn:") or data.startswith("untrack:"):
        code = data.replace("untrack_btn:", "").replace("untrack:", "").strip().upper()
        success = await remove_tracking(chat_id, code)
        if success:
            await query.answer(f"✅ Đã hủy theo dõi {code}")
            
            # Tự động cập nhật lại giao diện danh sách ngay tức thì
            remaining_rows = await get_active_trackings_by_chat(chat_id)
            if not remaining_rows:
                await query.edit_message_text(
                    f"🗑 Đã hủy theo dõi đơn <code>{html.escape(code)}</code> thành công!\n\n"
                    "📭 Danh sách theo dõi của bạn hiện đang trống.",
                    parse_mode=ParseMode.HTML
                )
            else:
                lines = [
                    f"✅ <i>Đã hủy theo dõi đơn <code>{html.escape(code)}</code> thành công!</i>\n",
                    "🗑 <b>CÁC ĐƠN HÀNG CÒN LẠI ĐANG THEO DÕI:</b>",
                    "━━━━━━━━━━━━━━━━━━━━━━",
                ]
                new_keyboard = []
                for idx, r in enumerate(remaining_rows, 1):
                    c = r["tracking_number"]
                    lbl = (r["label"] or "").strip()
                    lbl_escaped = html.escape(lbl)
                    label_part = f" ({lbl_escaped})" if lbl_escaped and lbl_escaped.lower() not in ("đơn hàng spx", "đơn spx", "none", "") else ""
                    status_text = html.escape(translate_spx_status(r["last_status"] or ""))
                    lines.append(
                        f"<b>{idx}.</b> <code>{html.escape(c)}</code>{label_part}\n"
                        f"   📌 Trạng thái: {status_text}\n"
                    )
                    btn_label = f"{c}" + (f" ({lbl[:8]}..)" if len(lbl) > 8 else f" ({lbl})" if lbl else "")
                    new_keyboard.append([
                        InlineKeyboardButton(f"📦 {btn_label}", callback_data=f"journey:{c}"),
                        InlineKeyboardButton("❌ Hủy (Untrack)", callback_data=f"untrack_btn:{c}")
                    ])
                lines.append("👇 <i>Bấm vào nút <b>[❌ Hủy (Untrack)]</b> bên cạnh đơn để tiếp tục hủy:</i>")
                full_text = "\n".join(lines)
                try:
                    await query.edit_message_text(
                        full_text, 
                        reply_markup=InlineKeyboardMarkup(new_keyboard), 
                        parse_mode=ParseMode.HTML
                    )
                except Exception as e:
                    logger.warning(f"Lỗi cập nhật tin nhắn sau khi untrack: {e}")
        else:
            await query.answer("Đơn này không còn trong danh sách!")
    else:
        await query.answer()

async def handle_quick_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Xử lý lệnh nhanh dạng /check_SPXVN... hoặc /untrack_SPXVN... khi người dùng bấm vào tin nhắn."""
    text = (update.message.text or "").strip()
    chat_id = update.effective_chat.id

    if text.startswith("/check_"):
        code = text.replace("/check_", "").strip().upper()
        await send_full_journey(chat_id, code, context)
    elif text.startswith("/untrack_"):
        code = text.replace("/untrack_", "").strip().upper()
        success = await remove_tracking(chat_id, code)
        code_escaped = html.escape(code)
        if success:
            await update.message.reply_text(f"🗑 Đã dừng theo dõi đơn <code>{code_escaped}</code> thành công.", parse_mode=ParseMode.HTML)
        else:
            await update.message.reply_text(f"❓ Không tìm thấy mã <code>{code_escaped}</code> trong danh sách theo dõi của bạn.", parse_mode=ParseMode.HTML)

async def handle_text_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Xử lý tin nhắn văn bản:
    1. Trả lời từ ForceReply (khi gõ /check, /track, /untrack mà không kèm mã)
    2. Bấm vào lệnh nhanh dạng /check_SPXVN...
    3. Gửi trực tiếp mã SPXVN... để tra cứu & theo dõi
    """
    text = update.message.text.strip()
    chat_id = update.effective_chat.id

    # 1. Kiểm tra nếu tin nhắn là trả lời cho một ô chờ ForceReply
    reply = update.message.reply_to_message
    if reply and reply.text:
        prompt_text = reply.text.upper()
        if "TRA CỨU TOÀN BỘ HÀNH TRÌNH" in prompt_text or "TRA CỨU" in prompt_text:
            match = SPX_REGEX.search(text)
            code = match.group(1).upper() if match else text.split()[0].upper()
            await send_full_journey(chat_id, code, context)
            return
        elif "HỦY THEO DÕI" in prompt_text:
            match = SPX_REGEX.search(text)
            code = match.group(1).upper() if match else text.split()[0].upper()
            success = await remove_tracking(chat_id, code)
            code_escaped = html.escape(code)
            if success:
                await update.message.reply_text(f"🗑 Đã dừng theo dõi đơn <code>{code_escaped}</code> thành công.", parse_mode=ParseMode.HTML)
            else:
                await update.message.reply_text(f"❓ Không tìm thấy mã <code>{code_escaped}</code> trong danh sách theo dõi của bạn.", parse_mode=ParseMode.HTML)
            return
        elif "THEO DÕI ĐƠN HÀNG" in prompt_text:
            match = SPX_REGEX.search(text)
            if match:
                tracking_number = match.group(1).upper()
                remaining_text = text.replace(match.group(1), "").strip()
                label = remaining_text if remaining_text else ""
                await process_tracking_request(update, tracking_number, label)
                return

    # 2. Tự động nhận diện khi người dùng gửi tin nhắn chứa mã SPXVN...
    match = SPX_REGEX.search(text)
    if match:
        tracking_number = match.group(1).upper()
        remaining_text = text.replace(match.group(1), "").strip()
        label = remaining_text if remaining_text else ""
        await process_tracking_request(update, tracking_number, label)
    else:
        # Nếu gửi tin nhắn thường không khớp mã SPX
        await update.message.reply_text(
            "💡 <b>HƯỚNG DẪN:</b>\n\n"
            "• Gửi thẳng mã vận đơn (VD: <code>SPXVN045388374941 Quần Jean</code>) để theo dõi tự động.\n"
            "• Gõ lệnh /check để tra cứu toàn bộ hành trình.\n"
            "• Gõ lệnh /list để xem danh sách đơn đang theo dõi.\n"
            "• Gõ lệnh /help để xem chi tiết cú pháp.",
            parse_mode=ParseMode.HTML
        )

# ================= BACKGROUND POLLING JOB ================= #
async def poll_spx_updates_job(context: ContextTypes.DEFAULT_TYPE):
    """
    Tiến trình quét ngầm định kỳ:
    - Lấy danh sách các đơn hàng chưa hoàn tất.
    - Gọi API SPX để kiểm tra sự kiện mới.
    - Bắn thông báo Telegram khi có biến động.
    """
    active_orders = await get_all_active_trackings()
    if not active_orders:
        return

    logger.info(f"Đang tiến hành quét tự động {len(active_orders)} đơn hàng SPX...")
    loop = asyncio.get_running_loop()

    for row in active_orders:
        order_id = row["id"]
        chat_id = row["chat_id"]
        code = row["tracking_number"]
        label = row["label"]
        last_time = row["last_timestamp"]

        info = await loop.run_in_executor(None, fetch_spx_tracking, code)
        if not info:
            continue

        is_done = bool(info.get("is_completed"))
        has_new_event = info.get("timestamp", 0) > last_time

        # Phát hiện sự kiện mới (timestamp mới hơn mốc đã lưu)
        if has_new_event:
            latest_event = info.get("latest_event") or {}
            card = format_spx_card(code, label, latest_event)

            if is_done:
                if info.get("is_delivered"):
                    card += "\n\n🎉 ĐƠN HÀNG ĐÃ GIAO THÀNH CÔNG!\n✅ Bot đã tự động hoàn tất và ngừng theo dõi (untrack) đơn này."
                elif info.get("is_cancelled"):
                    card += "\n\n⚠️ ĐƠN HÀNG ĐÃ BỊ HỦY HOẶC CHUYỂN HOÀN!\n✅ Bot đã tự động ngừng theo dõi (untrack) đơn này."

            try:
                await context.bot.send_message(chat_id=chat_id, text=card)
            except Exception as e:
                logger.error(f"Lỗi gửi tin nhắn đến chat_id {chat_id}: {e}")

            if is_done:
                # Tự động untrack: Xóa khỏi danh sách theo dõi khi giao thành công/hủy
                await remove_tracking(chat_id, code)
                logger.info(f"Đã tự động untrack đơn hoàn tất: {code} (chat_id: {chat_id})")
            else:
                # Cập nhật mốc mới vào database nếu đơn vẫn đang trên đường giao
                await update_tracking_status(
                    tracking_id=order_id,
                    status=info["latest_status"],
                    timestamp=info["timestamp"],
                    is_completed=0
                )
        elif is_done:
            # Đơn đã giao thành công từ trước: tự động dọn dẹp untrack khỏi CSDL
            await remove_tracking(chat_id, code)
            logger.info(f"Đã tự động dọn dẹp (untrack) đơn hoàn tất: {code} (chat_id: {chat_id})")

        # Nghỉ 2 giây giữa các request để giữ an toàn cho IP
        await asyncio.sleep(2.0)

async def post_init(application: Application):
    """Tự động đăng ký danh sách menu lệnh với Telegram."""
    commands = [
        BotCommand("start", "Bắt đầu & hướng dẫn sử dụng"),
        BotCommand("list", "Xem danh sách đơn đang theo dõi"),
        BotCommand("check", "Kiểm tra nhanh tình trạng đơn"),
        BotCommand("track", "Theo dõi đơn: /track SPXVN..."),
        BotCommand("untrack", "Hủy theo dõi đơn: /untrack SPXVN..."),
        BotCommand("help", "Xem trợ giúp & cú pháp"),
    ]
    await application.bot.set_my_commands(commands)
    logger.info("Đã tự động đăng ký danh sách menu lệnh với Telegram thành công!")

async def global_error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Bắt và log toàn bộ exception chưa được xử lý để tránh bot bị câm lặng."""
    logger.error("Exception khi xử lý update:", exc_info=context.error)
    if isinstance(update, Update) and update.effective_message:
        try:
            await update.effective_message.reply_text("⚠️ Đã có lỗi xảy ra khi xử lý yêu cầu. Vui lòng thử lại sau!")
        except Exception:
            pass

def main():
    if not BOT_TOKEN or BOT_TOKEN == "YOUR_TELEGRAM_BOT_TOKEN_HERE":
        print("\n" + "=" * 60)
        print("⚠️  LỖI: BẠN CHƯA CẤU HÌNH BOT TOKEN!")
        print("Vui lòng mở file .env và điền TELEGRAM_BOT_TOKEN nhận từ @BotFather.")
        print("=" * 60 + "\n")
        return

    # Khởi tạo CSDL SQLite
    asyncio.run(init_db())

    # Khởi tạo ứng dụng Telegram Bot kèm JobQueue và menu lệnh
    app = Application.builder().token(BOT_TOKEN).post_init(post_init).build()

    # Đăng ký xử lý lỗi toàn cục
    app.add_error_handler(global_error_handler)

    # Đăng ký các bộ xử lý lệnh
    app.add_handler(CommandHandler(["start", "help"], start_command))
    app.add_handler(CommandHandler("track", track_command))
    app.add_handler(CommandHandler("check", check_command))
    app.add_handler(CommandHandler("list", list_command))
    app.add_handler(CommandHandler("untrack", untrack_command))

    # Xử lý các nút bấm tương tác (Inline Buttons)
    app.add_handler(CallbackQueryHandler(handle_callback_query))

    # Xử lý khi người dùng bấm vào lệnh nhanh /check_SPXVN... hoặc /untrack_SPXVN...
    app.add_handler(MessageHandler(filters.Regex(r'^/(check|untrack)_'), handle_quick_command))

    # Tự động bắt tin nhắn văn bản khi người dùng gõ SPXVN... hoặc phản hồi từ ForceReply
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text_message))

    # Đăng ký Polling Job chạy định kỳ mỗi POLL_INTERVAL_MINUTES phút
    job_queue = app.job_queue
    interval_seconds = POLL_INTERVAL_MINUTES * 60
    job_queue.run_repeating(
        poll_spx_updates_job,
        interval=interval_seconds,
        first=15 # Quét lần đầu sau 15 giây kể từ khi bật bot
    )

    print(f"\n🚀 Bot SPX Telegram đang chạy!")
    print(f"⏱  Chu kỳ quét tự động: {POLL_INTERVAL_MINUTES} phút/lần.")
    print(f"👉 Mở Telegram và nhắn mã vận đơn SPXVN... cho bot để bắt đầu!\n")

    # Bật server phục vụ health check cho Koyeb/Render
    start_health_check_server()

    app.run_polling()

if __name__ == "__main__":
    main()
