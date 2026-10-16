import asyncio
import os
import sys
import re
import logging

if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

from dotenv import load_dotenv
from telegram import Update
from telegram.constants import ParseMode
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
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
from spx_service import fetch_spx_tracking, format_timestamp, format_spx_card

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
    """Máy chủ HTTP mini phục vụ Health Check cho Koyeb/Render."""
    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-type", "text/plain; charset=utf-8")
        self.end_headers()
        self.wfile.write(b"SPX Telegram Bot is alive and healthy!")

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

    # Lưu vào CSDL
    await add_or_update_tracking(
        chat_id=chat_id,
        tracking_number=info["tracking_number"],
        label=label,
        status=info["latest_status"],
        timestamp=info["timestamp"]
    )

    latest_event = info.get("latest_event") or {}
    card = format_spx_card(info["tracking_number"], label, latest_event)
    card += "\n\n🔔 Bot đã bật tự động theo dõi cho đơn hàng này!"

    if info["is_completed"]:
        card += "\n\n🎉 Đơn hàng này hiện đã ở trạng thái GIAO THÀNH CÔNG!"

    await status_msg.edit_text(card)

async def track_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Lệnh /track <mã_vận_đơn> [tên_gợi_nhớ]."""
    if not context.args:
        await update.message.reply_text(
            "⚠️ Vui lòng nhập mã vận đơn!\n"
            "Cú pháp: `/track <mã_vận_đơn> [tên_gợi_nhớ]`\n"
            "Ví dụ: `/track SPXVN045388374941 Đơn Quần Áo`",
            parse_mode=ParseMode.MARKDOWN
        )
        return

    tracking_number = context.args[0].strip().upper()
    label = " ".join(context.args[1:]).strip() if len(context.args) > 1 else ""
    await process_tracking_request(update, tracking_number, label)

async def check_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Lệnh /check <mã>: Tra cứu nhanh 1 lần không lưu vào danh sách tự động theo dõi."""
    if not context.args:
        await update.message.reply_text("⚠️ Nhập mã cần tra cứu. Ví dụ: `/check SPXVN045388374941`", parse_mode=ParseMode.MARKDOWN)
        return

    code = context.args[0].strip().upper()
    status_msg = await update.message.reply_text("⏳ Đang tra cứu nhanh...")

    loop = asyncio.get_running_loop()
    info = await loop.run_in_executor(None, fetch_spx_tracking, code)

    if not info:
        await status_msg.edit_text(f"❌ Không tìm thấy thông tin cho mã: `{code}`.", parse_mode=ParseMode.MARKDOWN)
        return

    latest_event = info.get("latest_event") or {}
    card = format_spx_card(info["tracking_number"], "", latest_event)
    await status_msg.edit_text(card)

async def list_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Lệnh /list liệt kê các đơn đang chờ giao."""
    chat_id = update.effective_chat.id
    rows = await get_active_trackings_by_chat(chat_id)

    if not rows:
        await update.message.reply_text(
            "📭 Hiện tại bạn không có đơn hàng nào đang theo dõi.\n"
            "👉 Hãy gửi mã vận đơn `SPXVN...` để thêm đơn nhé!",
            parse_mode=ParseMode.MARKDOWN
        )
        return

    lines = [f"📋 *DANH SÁCH ĐƠN ĐANG THEO DÕI ({len(rows)} đơn):*\n"]
    for idx, r in enumerate(rows, 1):
        t_str = format_timestamp(r["last_timestamp"])
        lbl = r["label"]
        label_part = f" ({lbl})" if lbl and lbl.lower() not in ("đơn hàng spx", "đơn spx", "") else ""
        lines.append(
            f"*{idx}.* `{r['tracking_number']}`{label_part}\n"
            f"   📊 Trạng thái: _{r['last_status']}_\n"
            f"   🕒 Cập nhật: {t_str}\n"
        )
    lines.append("👉 Dùng `/untrack <mã>` nếu muốn hủy theo dõi đơn nào.")
    await update.message.reply_text("\n".join(lines), parse_mode=ParseMode.MARKDOWN)

async def untrack_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Lệnh /untrack <mã>."""
    chat_id = update.effective_chat.id
    if not context.args:
        await update.message.reply_text(
            "⚠️ Vui lòng nhập mã vận đơn muốn dừng theo dõi!\n"
            "Ví dụ: `/untrack SPXVN045388374941`",
            parse_mode=ParseMode.MARKDOWN
        )
        return

    code = context.args[0].strip().upper()
    success = await remove_tracking(chat_id, code)
    if success:
        await update.message.reply_text(f"🗑 Đã dừng theo dõi đơn `{code}` thành công.", parse_mode=ParseMode.MARKDOWN)
    else:
        await update.message.reply_text(f"❓ Không tìm thấy mã `{code}` trong danh sách theo dõi của bạn.", parse_mode=ParseMode.MARKDOWN)

async def handle_text_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Tự động nhận diện khi người dùng gửi tin nhắn có chứa mã SPXVN...
    mà không cần phải gõ lệnh /track.
    """
    text = update.message.text.strip()
    match = SPX_REGEX.search(text)

    if match:
        tracking_number = match.group(1).upper()
        # Trích xuất phần text còn lại làm nhãn gợi nhớ nếu có
        remaining_text = text.replace(match.group(1), "").strip()
        label = remaining_text if remaining_text else ""
        await process_tracking_request(update, tracking_number, label)
    else:
        # Nếu gửi tin nhắn thường không khớp mã SPX
        await update.message.reply_text(
            "💡 Nếu bạn muốn tra cứu đơn hàng, hãy gửi mã vận đơn có dạng `SPXVN...`\n"
            "Ví dụ: `SPXVN045388374941 Quần Jean`\n"
            "Hoặc gõ lệnh `/help` để xem các tính năng.",
            parse_mode=ParseMode.MARKDOWN
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

        # Phát hiện sự kiện mới (timestamp mới hơn mốc đã lưu)
        if info and info["timestamp"] > last_time:
            latest_event = info.get("latest_event") or {}
            card = format_spx_card(code, label, latest_event)

            is_done = info["is_completed"]
            if is_done:
                if info["is_delivered"]:
                    card += "\n\n🎉 ĐƠN HÀNG ĐÃ GIAO THÀNH CÔNG!\nBot tự động hoàn tất và ngừng theo dõi đơn này."
                elif info["is_cancelled"]:
                    card += "\n\n⚠️ ĐƠN HÀNG ĐÃ BỊ HỦY HOẶC CHUYỂN HOÀN!\nBot tự động ngừng theo dõi đơn này."

            try:
                await context.bot.send_message(chat_id=chat_id, text=card)
            except Exception as e:
                logger.error(f"Lỗi gửi tin nhắn đến chat_id {chat_id}: {e}")

            # Cập nhật mốc mới vào database
            await update_tracking_status(
                tracking_id=order_id,
                status=info["latest_status"],
                timestamp=info["timestamp"],
                is_completed=is_done
            )

        # Nghỉ 2 giây giữa các request để giữ an toàn cho IP
        await asyncio.sleep(2.0)

def main():
    if not BOT_TOKEN or BOT_TOKEN == "YOUR_TELEGRAM_BOT_TOKEN_HERE":
        print("\n" + "=" * 60)
        print("⚠️  LỖI: BẠN CHƯA CẤU HÌNH BOT TOKEN!")
        print("Vui lòng mở file .env và điền TELEGRAM_BOT_TOKEN nhận từ @BotFather.")
        print("=" * 60 + "\n")
        return

    # Khởi tạo CSDL SQLite
    asyncio.run(init_db())

    # Khởi tạo ứng dụng Telegram Bot kèm JobQueue
    app = Application.builder().token(BOT_TOKEN).build()

    # Đăng ký các bộ xử lý lệnh
    app.add_handler(CommandHandler(["start", "help"], start_command))
    app.add_handler(CommandHandler("track", track_command))
    app.add_handler(CommandHandler("check", check_command))
    app.add_handler(CommandHandler("list", list_command))
    app.add_handler(CommandHandler("untrack", untrack_command))

    # Tự động bắt tin nhắn văn bản khi người dùng gõ SPXVN...
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
