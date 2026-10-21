import asyncio
import os
import sys
import re
import html
import logging
import threading
from http.server import HTTPServer, BaseHTTPRequestHandler

if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

from dotenv import load_dotenv
from telegram import (
    Update, 
    BotCommand, 
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
from carrier_service import (
    detect_carrier,
    fetch_tracking_unified,
    format_carrier_card,
    format_carrier_journey,
    format_carrier_list_item,
    CARRIER_NAMES,
    CARRIER_ICONS,
    format_timestamp,
    translate_spx_status,
    get_tracking_url
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

# Regex phát hiện mã vận đơn đa hãng (SPX, VNPost/EMS, GHTK, ViettelPost)
MULTI_CARRIER_REGEX = re.compile(
    r'(SPXVN[A-Za-z0-9]+|SLS[A-Za-z0-9]+|[A-Za-z]{2}\d{9}[A-Za-z]{2}|(?:EMS|VNPOST)[A-Za-z0-9]+|S\d{6,16}|S\.[A-Za-z0-9\.]+|GHTK[A-Za-z0-9]+|VT[A-Za-z0-9]+|VTP[A-Za-z0-9]+|\b\d{10,12}\b)',
    re.IGNORECASE
)

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
        html_content = """<!DOCTYPE html>
<html>
<head>
    <meta charset="utf-8">
    <title>Multi-Carrier Bot Status</title>
    <style>
        body { font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif; display: flex; justify-content: center; align-items: center; height: 100vh; margin: 0; background: #0f172a; color: #f8fafc; }
        .card { text-align: center; padding: 40px; background: #1e293b; border-radius: 16px; box-shadow: 0 10px 25px rgba(0,0,0,0.3); }
        .badge { display: inline-block; padding: 6px 16px; border-radius: 20px; background: #10b981; color: #fff; font-weight: bold; margin-bottom: 16px; }
    </style>
</head>
<body>
    <div class="card">
        <div class="badge">SYSTEM ONLINE</div>
        <h2>Multi-Carrier Telegram Tracking Bot</h2>
        <p style="color: #94a3b8;">Bot is actively running and polling orders (SPX, VNPost, GHTK, ViettelPost).</p>
    </div>
</body>
</html>"""
        self.wfile.write(html_content.encode("utf-8"))

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
        "🚚 <b>XIN CHÀO! ĐÂY LÀ BOT THEO DÕI ĐƠN HÀNG ĐA HÃNG</b>\n\n"
        "✨ <b>Hỗ trợ tra cứu & tự động thông báo 4 hãng phổ biến:</b>\n"
        "🔹 <b>SPX Express</b> (Ví dụ: <code>SPXVN06694936904A</code>)\n"
        "🔹 <b>VNPost / EMS</b> (Ví dụ: <code>EB123456789VN</code>)\n"
        "🔹 <b>Giao Hàng Tiết Kiệm - GHTK</b> (Ví dụ: <code>S22784532</code>)\n"
        "🔹 <b>ViettelPost</b> (Ví dụ: <code>VT123456789</code> hoặc 10-12 số)\n\n"
        "✨ <b>Cách dùng cực kỳ đơn giản:</b>\n"
        "👉 Bạn chỉ cần <b>gửi thẳng mã vận đơn</b> hoặc kèm tên / ghi chú (ví dụ: <code>SPXVN06694936904A Quần Jean</code>).\n"
        "👉 Có thể gửi <b>nhiều dòng trong cùng một tin nhắn</b> (mỗi dòng 1 mã kèm ghi chú), bot sẽ tự động thêm toàn bộ vào danh sách theo dõi!\n\n"
        "📌 <b>Danh sách lệnh:</b>\n"
        "🔹 /list - Xem danh sách đơn đang theo dõi & ngày dự kiến giao thực tế\n"
        "🔹 /untrack - Chọn đơn cần hủy theo dõi bằng nút bấm tiện lợi\n"
        "🔹 /help - Xem lại hướng dẫn\n\n"
        "🔔 <b>Cơ chế hoạt động:</b> Bot tự động quét live thời gian thực. Ngay khi bưu kiện cập nhật trạng thái mới hoặc dời ngày giao dự kiến, bạn sẽ nhận được thông báo ngay lập tức!"
    )
    await update.message.reply_text(welcome_text, parse_mode=ParseMode.HTML)

def extract_tracking_items(text: str) -> list:
    """
    Trích xuất danh sách các (mã vận đơn, ghi chú) từ tin nhắn.
    Hỗ trợ đa hãng (SPX, VNPost/EMS, GHTK, ViettelPost) và gửi nhiều dòng trong cùng một tin nhắn.
    Ví dụ:
        SPXVN06586066640A 8kg-bs
        EB123456789VN Bưu phẩm EMS
        S22784532 Đơn GHTK áo len
        11506020148 Kiện hàng VTP
    """
    items = []
    lines = [l.strip() for l in (text or "").splitlines() if l.strip()]
    for line in lines:
        matches = [m for m in MULTI_CARRIER_REGEX.finditer(line) if detect_carrier(m.group(1)) != "UNKNOWN"]
        if not matches:
            continue
        if len(matches) == 1:
            m = matches[0]
            code = m.group(1).upper()
            before = line[:m.start()].strip()
            after = line[m.end():].strip()
            # Xóa các ký tự bullet point ở đầu dòng nếu có (VD: 1. , -, *, •)
            if before and re.match(r'^([\-\*\•\+]|\d+[\.\)\:\-])\s*$', before):
                before = ''
            label = (before + " " + after).strip()
            items.append((code, label))
        else:
            for m in matches:
                code = m.group(1).upper()
                items.append((code, ""))

    seen = set()
    unique = []
    for c, lbl in items:
        if c not in seen:
            seen.add(c)
            unique.append((c, lbl))
    return unique

async def process_tracking_request(update: Update, tracking_number: str, label: str):
    """Xử lý tra cứu và lưu đơn đa hãng vào database."""
    chat_id = update.effective_chat.id
    carrier = detect_carrier(tracking_number)
    carrier_name = CARRIER_NAMES.get(carrier, "Vận chuyển")
    status_msg = await update.message.reply_text(f"⏳ Đang kết nối máy chủ {carrier_name} tra cứu hành trình...")

    loop = asyncio.get_running_loop()
    info = await loop.run_in_executor(None, fetch_tracking_unified, tracking_number, label, carrier)

    if not info:
        await status_msg.edit_text(
            f"❌ Không tìm thấy thông tin cho mã: <code>{html.escape(tracking_number)}</code> trên hệ thống {carrier_name}.\n\n"
            "⚠️ <b>Gợi ý kiểm tra:</b>\n"
            f"1. Đảm bảo mã vận đơn chính xác.\n"
            f"2. Nếu đơn vừa mới tạo, bưu cục có thể cần ít phút để đồng bộ dữ liệu.",
            parse_mode=ParseMode.HTML
        )
        return

    carrier = info.get("carrier", carrier)
    carrier_name = info.get("carrier_name", carrier_name)
    edd = info.get("estimated_delivery", "")
    card = format_carrier_card(info)

    # Nếu đơn hàng đã giao thành công hoặc đã kết thúc: Không lưu vào theo dõi (tự động untrack)
    if info.get("is_completed"):
        await remove_tracking(chat_id, info["tracking_number"])
        if info.get("is_delivered"):
            card += "\n\n🎉 <b>ĐƠN HÀNG ĐÃ GIAO THÀNH CÔNG!</b>\n✅ Đơn hàng đã hoàn tất nên bot không cần theo dõi nữa."
        else:
            card += "\n\n⚠️ <b>ĐƠN HÀNG ĐÃ KẾT THÚC / HỦY HOÀN!</b>\n✅ Đơn hàng đã kết thúc nên bot không cần theo dõi nữa."
        
        btn_name = f"🔍 Xem full hành trình" + (f" ({label})" if label else "")
        keyboard = [[InlineKeyboardButton(btn_name, callback_data=f"journey:{info['tracking_number']}")]]
        await status_msg.edit_text(card, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode=ParseMode.HTML)
        return

    # Lưu vào CSDL cho các đơn đang trên đường vận chuyển
    await add_or_update_tracking(
        chat_id=chat_id,
        tracking_number=info["tracking_number"],
        label=label,
        status=info["latest_status"],
        timestamp=info["timestamp"],
        estimated_delivery=edd,
        carrier=carrier
    )

    card += "\n\n🔔 <i>Bot đã bật tự động theo dõi cho đơn hàng này!</i>"
    card += f"\n👉 Xem hành trình: /check_{info['tracking_number']}"
    btn_name = f"🔍 Xem full hành trình" + (f" ({label})" if label else "")
    keyboard = [[InlineKeyboardButton(btn_name, callback_data=f"journey:{info['tracking_number']}")]]
    await status_msg.edit_text(card, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode=ParseMode.HTML)

async def process_multiple_tracking_requests(update: Update, items: list):
    """
    Xử lý tra cứu và thêm nhiều đơn hàng cùng lúc (đa hãng) khi người dùng gửi nhiều dòng.
    Lưu từng đơn vào CSDL và hiển thị danh sách tổng kết kèm link xem hành trình.
    """
    chat_id = update.effective_chat.id
    total = len(items)
    status_msg = await update.message.reply_text(
        f"⏳ Đang kết nối tra cứu và thêm {total} đơn hàng vào danh sách...",
        parse_mode=ParseMode.HTML
    )

    loop = asyncio.get_running_loop()
    sem = asyncio.Semaphore(4)

    async def fetch_one(code: str, lbl: str):
        async with sem:
            carrier = detect_carrier(code)
            try:
                info = await loop.run_in_executor(None, fetch_tracking_unified, code, lbl, carrier)
                return (code, lbl, info, carrier)
            except Exception as e:
                logger.error(f"Lỗi tra cứu {code}: {e}")
                return (code, lbl, None, carrier)

    results = await asyncio.gather(*[fetch_one(c, l) for c, l in items])

    lines = [
        f"📋 <b>KẾT QUẢ THÊM {total} ĐƠN HÀNG VÀO THEO DÕI:</b>",
        "━━━━━━━━━━━━━━━━━━━━━━",
    ]
    added_count = 0

    for idx, (code, lbl, info, carrier_detected) in enumerate(results, 1):
        code_escaped = html.escape(code)
        lbl_escaped = html.escape(lbl)
        label_part = f" ({lbl_escaped})" if lbl_escaped and lbl_escaped.lower() not in ("đơn hàng", "đơn", "none", "", "spx", "ghtk", "vtp", "ems") else ""
        carrier_name = CARRIER_NAMES.get(carrier_detected, "Vận chuyển")
        carrier_badge = f"[{carrier_name}] "

        if not info:
            lines.append(
                f"<b>{idx}.</b> {carrier_badge}<code>{code_escaped}</code>{label_part}\n"
                f"   ❌ <i>Chưa có thông tin trên hệ thống {carrier_name} (vui lòng kiểm tra lại)</i>\n"
            )
            continue

        carrier = info.get("carrier", carrier_detected)
        carrier_name = info.get("carrier_name", carrier_name)
        carrier_badge = f"[{carrier_name}] "

        edd = info.get("estimated_delivery", "")
        edd_badge = f" <b>[Dự kiến: {html.escape(edd)}]</b>" if edd else ""
        status_text = html.escape(info.get("latest_status") or "Đang vận chuyển")
        ts = info.get("timestamp") or 0
        t_str = format_timestamp(ts) if ts else "Đang cập nhật"

        if info.get("is_completed"):
            await remove_tracking(chat_id, info["tracking_number"])
            done_msg = "🎉 Đơn đã giao thành công (không cần theo dõi)" if info.get("is_delivered") else "⚠️ Đơn đã kết thúc / hoàn hàng (không cần theo dõi)"
            lines.append(
                f"<b>{idx}.</b> {carrier_badge}<code>{code_escaped}</code>{label_part}\n"
                f"   📌 Trạng thái: {status_text}\n"
                f"   {done_msg}\n"
            )
        else:
            # Lưu đơn vào CSDL kèm carrier
            await add_or_update_tracking(
                chat_id=chat_id,
                tracking_number=info["tracking_number"],
                label=lbl,
                status=info.get("latest_status", ""),
                timestamp=ts,
                estimated_delivery=edd,
                carrier=carrier
            )
            added_count += 1
            lines.append(
                f"<b>{idx}.</b> {carrier_badge}<code>{code_escaped}</code>{edd_badge}{label_part}\n"
                f"   📌 Trạng thái: {status_text}\n"
                f"   🕒 Cập nhật: {t_str}\n"
                f"   ✅ Đã bật tự động theo dõi\n"
                f"   👉 Xem hành trình: /check_{code}\n"
            )

    lines.append("━━━━━━━━━━━━━━━━━━━━━━")
    lines.append(f"<i>✅ Đã thêm <b>{added_count}/{total}</b> đơn vào danh sách theo dõi. Bấm link /check_... trên từng đơn để xem chi tiết hành trình.</i>")

    full_text = "\n".join(lines)
    try:
        await status_msg.edit_text(full_text, parse_mode=ParseMode.HTML)
    except Exception as e:
        logger.warning(f"Lỗi gửi HTML trong process_multiple_tracking_requests: {e}")
        clean_text = re.sub(r'<[^>]+>', '', full_text).replace('&lt;', '<').replace('&gt;', '>').replace('&amp;', '&')
        await status_msg.edit_text(clean_text)

async def send_full_journey(chat_id: int, code: str, context: ContextTypes.DEFAULT_TYPE, label: str = ""):
    """Tra cứu và gửi toàn bộ hành trình chi tiết của đơn hàng (đa hãng)."""
    carrier = detect_carrier(code)
    carrier_name = CARRIER_NAMES.get(carrier, "Vận chuyển")
    status_msg = await context.bot.send_message(
        chat_id=chat_id, 
        text=f"⏳ Đang tải toàn bộ hành trình cho mã <code>{html.escape(code)}</code> ({carrier_name})...", 
        parse_mode=ParseMode.HTML
    )
    loop = asyncio.get_running_loop()
    info = await loop.run_in_executor(None, fetch_tracking_unified, code, label, carrier)

    if not info:
        await status_msg.edit_text(
            f"❌ Không tìm thấy thông tin cho mã: <code>{html.escape(code)}</code> trên hệ thống {carrier_name}.",
            parse_mode=ParseMode.HTML
        )
        return

    full_journey_text = format_carrier_journey(info)
    try:
        await status_msg.edit_text(full_journey_text, parse_mode=ParseMode.HTML)
    except Exception as e:
        logger.warning(f"Lỗi gửi HTML full journey: {e}, fallback text thường")
        clean = re.sub(r'<[^>]+>', '', full_journey_text)
        await status_msg.edit_text(clean)

async def track_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Hỗ trợ nếu người dùng quen tay gõ /track."""
    if context.args:
        tracking_number = context.args[0].strip().upper()
        label = " ".join(context.args[1:]).strip() if len(context.args) > 1 else ""
        await process_tracking_request(update, tracking_number, label)
    else:
        await update.message.reply_text(
            "💡 <b>MẸO TIỆN LỢI:</b> Bạn không cần gõ lệnh <code>/track</code> nữa!\n\n"
            "👉 Hãy gửi thẳng mã vận đơn vào khung chat (VD: <code>SPXVN06694936904A</code> hoặc <code>EB123456789VN</code>), bot sẽ tự động nhận diện hãng và theo dõi ngay lập tức.",
            parse_mode=ParseMode.HTML
        )

async def check_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Hỗ trợ nếu người dùng quen tay gõ /check."""
    if context.args:
        code = context.args[0].strip().upper()
        await send_full_journey(update.effective_chat.id, code, context)
    else:
        await update.message.reply_text(
            "💡 <b>MẸO TIỆN LỢI:</b> Bạn không cần gõ lệnh <code>/check</code> nữa!\n\n"
            "👉 Hãy gõ lệnh /list rồi bấm vào link /check_... của từng đơn để xem toàn bộ hành trình nhé.",
            parse_mode=ParseMode.HTML
        )

async def list_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Lệnh /list quét live toàn bộ các đơn đang chờ giao trên các máy chủ vận chuyển để cập nhật trạng thái & ngày dự kiến mới nhất."""
    chat_id = update.effective_chat.id
    try:
        rows = await get_active_trackings_by_chat(chat_id)

        if not rows:
            await update.message.reply_text(
                "📭 Hiện tại bạn không có đơn hàng nào đang theo dõi.\n"
                "👉 Hãy gửi mã vận đơn (SPX, VNPost/EMS, GHTK, ViettelPost) để thêm đơn nhé!",
                parse_mode=ParseMode.HTML
            )
            return

        status_msg = await update.message.reply_text(
            f"⏳ Đang quét mới {len(rows)} đơn hàng thời gian thực...",
            parse_mode=ParseMode.HTML
        )

        loop = asyncio.get_running_loop()
        sem = asyncio.Semaphore(4)

        async def fetch_one(r):
            c = r["tracking_number"]
            l = r["label"] or ""
            carrier = r.get("carrier") or detect_carrier(c)
            async with sem:
                try:
                    info = await loop.run_in_executor(None, fetch_tracking_unified, c, l, carrier)
                    return (r, info)
                except Exception as err:
                    logger.warning(f"Lỗi scan {c}: {err}")
                    return (r, None)

        results = await asyncio.gather(*[fetch_one(r) for r in rows])

        lines = [f"📋 <b>DANH SÁCH ĐƠN ĐANG THEO DÕI ({len(rows)} đơn):</b>\n"]

        for idx, (r, info) in enumerate(results, 1):
            code = r["tracking_number"]
            code_escaped = html.escape(code)
            lbl = (r["label"] or "").strip()
            lbl_escaped = html.escape(lbl)
            label_part = f" ({lbl_escaped})" if lbl_escaped and lbl_escaped.lower() not in ("đơn hàng", "đơn", "none", "", "spx", "ghtk", "vtp", "ems") else ""

            carrier = r.get("carrier") or detect_carrier(code)
            if info:
                carrier = info.get("carrier", carrier)
                carrier_name = info.get("carrier_name") or CARRIER_NAMES.get(carrier, "Vận chuyển")
                st_raw = info.get("latest_status") or ""
                ts = info.get("timestamp") or 0
                edd = info.get("estimated_delivery") or ""
                is_completed = bool(info.get("is_completed"))
                is_delivered = bool(info.get("is_delivered"))

                # Tự động cập nhật dữ liệu mới nhất vào database
                if is_completed:
                    await remove_tracking(chat_id, code)
                else:
                    await add_or_update_tracking(
                        chat_id=chat_id,
                        tracking_number=code,
                        label=lbl,
                        status=st_raw,
                        timestamp=ts,
                        estimated_delivery=edd,
                        carrier=carrier
                    )
            else:
                carrier_name = CARRIER_NAMES.get(carrier, "Vận chuyển")
                st_raw = r["last_status"] or ""
                ts = r["last_timestamp"] or 0
                edd = r["estimated_delivery"] or ""
                is_completed = False
                is_delivered = False

            carrier_badge = f"[{carrier_name}] "
            status_text = html.escape(st_raw)
            t_str = format_timestamp(ts) if ts else "Đang cập nhật"
            edd_badge = f" <b>[Dự kiến: {html.escape(edd)}]</b>" if edd else ""

            if is_completed:
                done_desc = "🎉 Đơn đã giao thành công (tự động hoàn tất & untrack)" if is_delivered else "⚠️ Đơn đã kết thúc / untrack"
                lines.append(
                    f"<b>{idx}.</b> {carrier_badge}<code>{code_escaped}</code>{label_part}\n"
                    f"   📌 Trạng thái: {status_text}\n"
                    f"   {done_desc}\n"
                )
            else:
                lines.append(
                    f"<b>{idx}.</b> {carrier_badge}<code>{code_escaped}</code>{edd_badge}{label_part}\n"
                    f"   📌 Trạng thái: {status_text}\n"
                    f"   🕒 Cập nhật: {t_str}\n"
                    f"   👉 Xem hành trình: /check_{code}\n"
                )

        full_text = "\n".join(lines)

        try:
            await status_msg.edit_text(full_text, parse_mode=ParseMode.HTML)
        except Exception as send_err:
            logger.warning(f"Lỗi gửi HTML trong /list ({send_err}), fallback sang text thường")
            clean_text = re.sub(r'<[^>]+>', '', full_text).replace('&lt;', '<').replace('&gt;', '>').replace('&amp;', '&')
            await status_msg.edit_text(clean_text)
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
        label_part = f" ({lbl_escaped})" if lbl_escaped and lbl_escaped.lower() not in ("đơn hàng", "đơn", "none", "", "spx", "ghtk", "vtp", "ems") else ""
        carrier = r.get("carrier") or detect_carrier(code)
        carrier_name = CARRIER_NAMES.get(carrier, "Vận chuyển")
        status_text = html.escape(r["last_status"] or "")
        
        lines.append(
            f"<b>{idx}.</b> [{carrier_name}] <code>{html.escape(code)}</code>{label_part}\n"
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
                    label_part = f" ({lbl_escaped})" if lbl_escaped and lbl_escaped.lower() not in ("đơn hàng", "đơn", "none", "", "spx", "ghtk", "vtp", "ems") else ""
                    carrier = r.get("carrier") or detect_carrier(c)
                    carrier_name = CARRIER_NAMES.get(carrier, "Vận chuyển")
                    status_text = html.escape(r["last_status"] or "")
                    lines.append(
                        f"<b>{idx}.</b> [{carrier_name}] <code>{html.escape(c)}</code>{label_part}\n"
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
    """Xử lý lệnh nhanh dạng /check_<MÃ> hoặc /untrack_<MÃ> khi người dùng bấm vào tin nhắn."""
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
    Tự động nhận diện khi người dùng gửi tin nhắn chứa 1 hoặc nhiều mã vận đơn đa hãng.
    Hỗ trợ gửi nhiều dòng trong cùng một tin nhắn (mỗi dòng 1 mã kèm ghi chú).
    """
    text = (update.message.text or "").strip()

    items = extract_tracking_items(text)
    if not items:
        await update.message.reply_text(
            "💡 <b>HƯỚNG DẪN NHANH:</b>\n\n"
            "• Gửi thẳng mã vận đơn (VD: <code>SPXVN06694936904A Quần Jean</code> hoặc <code>EB123456789VN</code>) để theo dõi tự động.\n"
            "• Hỗ trợ đa hãng: <b>SPX Express, VNPost / EMS, GHTK, ViettelPost</b>.\n"
            "• Có thể gửi <b>nhiều dòng cùng lúc</b> (mỗi dòng 1 mã kèm ghi chú) để thêm toàn bộ vào danh sách!\n"
            "• Gõ lệnh /list để xem danh sách đơn & ngày dự kiến giao.\n"
            "• Gõ lệnh /untrack để chọn hủy theo dõi đơn hàng bằng nút bấm.\n"
            "• Gõ lệnh /help để xem chi tiết cú pháp.",
            parse_mode=ParseMode.HTML
        )
        return

    if len(items) == 1:
        tracking_number, label = items[0]
        await process_tracking_request(update, tracking_number, label)
    else:
        await process_multiple_tracking_requests(update, items)

# ================= BACKGROUND POLLING JOB ================= #
async def poll_spx_updates_job(context: ContextTypes.DEFAULT_TYPE):
    """
    Tiến trình quét ngầm định kỳ đa hãng:
    - Lấy danh sách các đơn hàng chưa hoàn tất.
    - Gọi API tra cứu của từng hãng để kiểm tra sự kiện mới.
    - Bắn thông báo Telegram khi có biến động.
    """
    active_orders = await get_all_active_trackings()
    if not active_orders:
        return

    logger.info(f"Đang tiến hành quét tự động {len(active_orders)} đơn hàng đa hãng...")
    loop = asyncio.get_running_loop()

    for row in active_orders:
        order_id = row["id"]
        chat_id = row["chat_id"]
        code = row["tracking_number"]
        label = row["label"]
        last_time = row["last_timestamp"]
        carrier = row.get("carrier") or detect_carrier(code)

        info = await loop.run_in_executor(None, fetch_tracking_unified, code, label, carrier)
        if not info:
            continue

        is_done = bool(info.get("is_completed"))
        has_new_event = info.get("timestamp", 0) > last_time
        edd = info.get("estimated_delivery", "")

        # Phát hiện sự kiện mới (timestamp mới hơn mốc đã lưu)
        if has_new_event:
            card = format_carrier_card(info)

            if is_done:
                if info.get("is_delivered"):
                    card += "\n\n🎉 <b>ĐƠN HÀNG ĐÃ GIAO THÀNH CÔNG!</b>\n✅ Bot đã tự động hoàn tất và ngừng theo dõi (untrack) đơn này."
                elif info.get("is_cancelled"):
                    card += "\n\n⚠️ <b>ĐƠN HÀNG ĐÃ KẾT THÚC / HỦY HOÀN!</b>\n✅ Bot đã tự động ngừng theo dõi (untrack) đơn này."

            try:
                await context.bot.send_message(chat_id=chat_id, text=card, parse_mode=ParseMode.HTML)
            except Exception as e:
                logger.error(f"Lỗi gửi tin nhắn đến chat_id {chat_id}: {e}")

            if is_done:
                await remove_tracking(chat_id, code)
                logger.info(f"Đã tự động untrack đơn hoàn tất: {code} (chat_id: {chat_id})")
            else:
                await update_tracking_status(
                    tracking_id=order_id,
                    status=info["latest_status"],
                    timestamp=info["timestamp"],
                    is_completed=0,
                    estimated_delivery=edd,
                    carrier=info.get("carrier", carrier)
                )
        elif is_done:
            await remove_tracking(chat_id, code)
            logger.info(f"Đã tự động dọn dẹp (untrack) đơn hoàn tất: {code} (chat_id: {chat_id})")

        # Nghỉ 1.5 giây giữa các request để bảo đảm IP không bị rate-limit
        await asyncio.sleep(1.5)

async def post_init(application: Application):
    """Tự động đăng ký danh sách menu lệnh rút gọn với Telegram."""
    commands = [
        BotCommand("start", "Bắt đầu & hướng dẫn sử dụng"),
        BotCommand("list", "Xem danh sách đơn & ngày dự kiến giao"),
        BotCommand("untrack", "Chọn đơn cần hủy theo dõi"),
        BotCommand("help", "Xem trợ giúp & cú pháp"),
    ]
    await application.bot.set_my_commands(commands)
    logger.info("Đã tự động đăng ký danh sách menu lệnh rút gọn với Telegram thành công!")

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

    # Xử lý khi người dùng bấm vào lệnh nhanh /check_<MÃ> hoặc /untrack_<MÃ>
    app.add_handler(MessageHandler(filters.Regex(r'^/(check|untrack)_'), handle_quick_command))

    # Tự động bắt tin nhắn văn bản khi người dùng gửi mã vận đơn đa hãng
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text_message))

    # Đăng ký Polling Job chạy định kỳ mỗi POLL_INTERVAL_MINUTES phút
    job_queue = app.job_queue
    interval_seconds = POLL_INTERVAL_MINUTES * 60
    job_queue.run_repeating(
        poll_spx_updates_job,
        interval=interval_seconds,
        first=15 # Quét lần đầu sau 15 giây kể từ khi bật bot
    )

    print(f"\n🚀 Bot Đa Hãng Telegram (SPX, VNPost, GHTK, ViettelPost) đang chạy!")
    print(f"⏱  Chu kỳ quét tự động: {POLL_INTERVAL_MINUTES} phút/lần.")
    print(f"👉 Mở Telegram và nhắn mã vận đơn cho bot để bắt đầu!\n")

    # Bật server phục vụ health check cho Koyeb/Render
    start_health_check_server()

    app.run_polling()

if __name__ == "__main__":
    main()
