import time
import base64
import hashlib
import logging
import sys
import re
import html
from datetime import datetime
from typing import Optional, Dict, Any, List

# Tự động cấu hình UTF-8 cho Windows CMD
if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

logger = logging.getLogger(__name__)

SPX_API_URL = "https://spx.vn/api/v2/fleet_order/tracking/search"
SPX_HOME_URL = "https://spx.vn/"
SPX_SECRET_SALT = b"0ebfffe63d2a481cf57fe7d5ebdc9fd6"

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Referer": "https://spx.vn/",
    "Origin": "https://spx.vn",
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "vi-VN,vi;q=0.9,en-US;q=0.8",
}

DELIVERY_COMPLETE_KEYWORDS = [
    "delivered", "delivery_done", "giao hàng thành công", "đã giao", "giao thành công", 
    "ký nhận", "đã nhận hàng", "received by consignee", "buyer received"
]
CANCELLED_KEYWORDS = [
    "cancelled", "canceled", "đã hủy", "hủy đơn", "trả hàng thành công", 
    "chuyển hoàn thành công", "đã hoàn hàng", "return_done", "returned"
]

from datetime import datetime, timezone, timedelta

# Múi giờ Việt Nam UTC+7
VN_TZ = timezone(timedelta(hours=7))

def format_timestamp(ts: int) -> str:
    """Chuyển Unix timestamp (giây) sang định dạng ngày giờ Việt Nam (UTC+7)."""
    if not ts:
        return "N/A"
    return datetime.fromtimestamp(ts, tz=VN_TZ).strftime("%H:%M:%S %d/%m/%Y")

def generate_signed_tracking_number(tracking_number: str) -> str:
    """Tạo chữ ký SHA256 cho mã vận đơn theo thuật toán của SPX."""
    code = tracking_number.strip().upper()
    ts = int(time.time())
    salt_b64 = base64.b64encode(SPX_SECRET_SALT).decode()
    signature_raw = f"{code}{ts}{salt_b64}"
    sig = hashlib.sha256(signature_raw.encode("utf-8")).hexdigest()
    return f"{code}|{ts}{sig}"

def translate_spx_status(raw_status: str, msg: str = "", loc: str = "") -> str:
    """Chuyển đổi trạng thái mã code của SPX sang tiếng Việt chuẩn, phân biệt rõ Đang giao vs Đã giao thành công."""
    if not raw_status and not msg:
        return "Đang vận chuyển"
    raw_status = str(raw_status or "")
    msg = str(msg or "")
    status_lower = raw_status.lower()
    msg_lower = msg.lower()
    full_lower = f"{status_lower} {msg_lower}"

    # 1. ĐÃ GIAO HÀNG THÀNH CÔNG (Chỉ khi có xác nhận giao hoàn tất rõ ràng)
    if (
        "delivery_done" in status_lower
        or status_lower == "delivered"
        or "pod_done" in status_lower
        or "giao hàng thành công" in full_lower
        or "giao thành công" in full_lower
        or "đã giao hàng thành công" in full_lower
        or "đã nhận hàng" in full_lower
        or "ký nhận thành công" in full_lower
    ):
        return "Đơn hàng đã giao thành công"

    # 2. ĐANG GIAO HÀNG (Shipper đang trên đường đi giao đến người nhận)
    if (
        "delivering" in status_lower
        or "out for delivery" in full_lower
        or "out_for_delivery" in status_lower
        or "đang được giao" in full_lower
        or "đang giao hàng" in full_lower
        or "đang đi giao" in full_lower
        or "đang giao" in full_lower
    ):
        return "Đơn hàng đang được giao đến bạn"

    # 3. ĐÃ HỦY / CHUYỂN HOÀN
    if (
        "cancelled" in status_lower
        or "canceled" in status_lower
        or "return_done" in status_lower
        or "returned" in status_lower
        or "hủy đơn" in full_lower
        or "đã hủy" in full_lower
        or "chuyển hoàn" in full_lower
        or "trả hàng" in full_lower
    ):
        return "Đơn hàng đã bị hủy / chuyển hoàn"

    # 4. LẤY HÀNG THÀNH CÔNG
    if "pickup_done" in status_lower or "picked up" in status_lower or "picked_up" in status_lower:
        return "Đơn vị vận chuyển lấy hàng thành công"

    # 5. ĐẾN KHO
    if "received" in status_lower:
        return f"Đơn hàng đã đến kho {loc}" if loc else "Đơn hàng đã đến kho"

    # 6. PHÂN LOẠI
    if "sorting" in status_lower or "unloading" in status_lower or "unloaded" in status_lower:
        return "Đơn hàng đã được phân loại và sẵn sàng trung chuyển đến điểm kế tiếp"

    # 7. TRUNG CHUYỂN
    if "transporting" in status_lower or "lhtransporting" in status_lower:
        return "Đơn hàng đã lên xe trung chuyển"

    # 8. KHỞI TẠO
    if "created" in status_lower:
        return "Đơn hàng đã được khởi tạo"

    return msg or raw_status

def format_spx_card(tracking_number: str, label: str, event: dict) -> str:
    """
    Định dạng thông báo chuẩn theo mẫu EXPRESS VN TRACK BOT:
    🔔 SPX Update: ...
    ━━━━━━━━━━━━━━
    📦 Mã (Tên)
    📌 Status: ...
    🕒 Date: ...
    📍 Location: ...
    🚚 Next Location: ...
    """
    raw_status = event.get("status", "")
    msg = event.get("message", "")
    ts = event.get("timestamp", 0)
    date_str = format_timestamp(ts)
    
    # 1. Trích xuất Location
    loc = event.get("location") or ""
    if not loc:
        m_at = re.search(r'at \[(.*?)\]', msg)
        if m_at:
            loc = m_at.group(1)
        else:
            m_bracket = re.search(r'^\[(.*?)\]', msg)
            if m_bracket:
                cand = m_bracket.group(1)
                if not cand.startswith("TO"):
                    loc = cand
                    
    # 2. Trích xuất Next Location
    next_loc = ""
    m_next = re.search(r'transporting to \[(.*?)\]', msg)
    if m_next:
        next_loc = m_next.group(1)

    # 3. Chuyển ngữ sang tiếng Việt chuẩn theo mẫu
    status_vn = translate_spx_status(raw_status, msg, loc)
    if raw_status in ("FMHub_Pickup_Done", "Picked Up"):
        if loc and not next_loc:
            next_loc = loc
        loc = ""  # Không in dòng Location ở mốc lấy hàng theo mẫu
        
    # Tên đơn hàng: chỉ in kèm ngoặc đơn nếu người dùng có đặt tên riêng
    clean_label = label.strip() if label else ""
    if clean_label and clean_label.lower() not in ("đơn hàng spx", "đơn spx", "tra cứu nhanh", "none"):
        pkg_line = f"📦 {tracking_number} ({clean_label})"
    else:
        pkg_line = f"📦 {tracking_number}"

    lines = [
        f"🔔 SPX Update: {status_vn}",
        "━━━━━━━━━━━━━━",
        pkg_line,
        f"📌 Status: {status_vn}",
        f"🕒 Date: {date_str}"
    ]
    if loc:
        lines.append(f"📍 Location: {loc}")
    if next_loc:
        lines.append(f"🚚 Next Location: {next_loc}")
        
    return "\n".join(lines)

def format_full_journey(info: dict, label: str = "") -> str:
    """Định dạng toàn bộ lịch sử hành trình chi tiết của đơn hàng (Full Timeline)."""
    code = info.get("tracking_number", "")
    clean_label = label.strip() if label else ""
    if clean_label and clean_label.lower() not in ("đơn hàng spx", "đơn spx", "none", ""):
        pkg_title = f"{code} ({clean_label})"
    else:
        pkg_title = code

    current_status = info.get("latest_status", "Đang cập nhật")
    current_time = format_timestamp(info.get("timestamp", 0))
    history = info.get("history", [])

    lines = [
        "📜 <b>CHI TIẾT TOÀN BỘ HÀNH TRÌNH ĐƠN HÀNG</b>",
        "━━━━━━━━━━━━━━━━━━━━━━",
        f"📦 <b>Mã đơn:</b> <code>{code}</code>",
        f"📌 <b>Trạng thái:</b> <b>{html.escape(current_status)}</b>",
        f"🕒 <b>Cập nhật:</b> {current_time}",
        "━━━━━━━━━━━━━━━━━━━━━━",
        f"📋 <b>Lịch sử chi tiết ({len(history)} mốc):</b>\n"
    ]

    if not history:
        lines.append("<i>Chưa có dữ liệu lịch sử các mốc vận chuyển.</i>")
    else:
        for idx, event in enumerate(history, 1):
            ts = event.get("timestamp") or event.get("checkpoint_time", 0)
            t_str = format_timestamp(ts)
            raw_st = event.get("status", "")
            raw_msg = event.get("message", "") or event.get("description", "")
            loc = event.get("location") or event.get("hub_name", "")
            if not loc:
                m_at = re.search(r'at \[(.*?)\]', raw_msg)
                if m_at:
                    loc = m_at.group(1)
                else:
                    m_b = re.search(r'^\[(.*?)\]', raw_msg)
                    if m_b and not m_b.group(1).startswith("TO"):
                        loc = m_b.group(1)

            st_vn = translate_spx_status(raw_st, raw_msg, loc)

            event_text = f"<b>{idx}.</b> 🕒 <code>{t_str}</code>\n   🔹 <b>{html.escape(st_vn)}</b>"
            if loc and loc not in st_vn:
                event_text += f"\n   📍 Kho: {html.escape(loc)}"
            
            m_next = re.search(r'transporting to \[(.*?)\]', raw_msg)
            if m_next:
                event_text += f"\n   🚚 Đến kho: {html.escape(m_next.group(1))}"

            lines.append(event_text)

    return "\n".join(lines)

def fetch_spx_tracking(tracking_number: str) -> Optional[Dict[str, Any]]:
    """Tra cứu hành trình kiện hàng từ SPX Express Việt Nam với chữ ký số."""
    clean_code = tracking_number.strip().upper()
    signed_code = generate_signed_tracking_number(clean_code)
    
    try:
        from curl_cffi import requests
        session = requests.Session(impersonate="chrome124")
        
        params = {"sls_tracking_number": signed_code}
        res = session.get(SPX_API_URL, params=params, headers=HEADERS, timeout=12)
        
        if res.status_code != 200:
            logger.warning(f"SPX API returned HTTP {res.status_code}")
            return None
            
        data_json = res.json()
    except Exception as e:
        logger.error(f"Lỗi kết nối SPX: {e}")
        return None
        
    retcode = data_json.get("retcode", data_json.get("code", -1))
    if retcode != 0:
        return None
        
    data = data_json.get("data")
    if not data or not isinstance(data, dict) or len(data) == 0:
        return None

    history_list: List[Dict[str, Any]] = (
        data.get("tracking_list") or 
        data.get("tracking_history") or 
        []
    )
    
    # Sắp xếp theo mốc thời gian tăng dần
    sorted_history = sorted(
        history_list, 
        key=lambda x: x.get("timestamp", x.get("checkpoint_time", 0))
    )
    
    latest_event = sorted_history[-1] if sorted_history else None
    
    status_title = data.get("current_status") or "Đang vận chuyển"
    checkpoint_time = 0
    location = ""
    detail_msg = ""
    
    if latest_event:
        status_title = latest_event.get("status") or status_title
        checkpoint_time = latest_event.get("timestamp") or latest_event.get("checkpoint_time", 0)
        location = latest_event.get("location") or latest_event.get("hub_name", "")
        detail_msg = latest_event.get("message") or latest_event.get("description", "")
        
    status_lower = (status_title or "").lower()
    full_text = f"{status_title} {detail_msg}".lower()

    # Phân biệt rõ ràng ĐANG GIAO HÀNG vs ĐÃ GIAO HÀNG THÀNH CÔNG
    is_delivering = (
        "delivering" in status_lower
        or "out for delivery" in full_text
        or "out_for_delivery" in status_lower
        or "đang được giao" in full_text
        or "đang giao" in full_text
        or "đang đi giao" in full_text
    )

    is_delivered = (
        (
            "delivery_done" in status_lower
            or status_lower == "delivered"
            or "pod_done" in status_lower
            or "giao hàng thành công" in full_text
            or "giao thành công" in full_text
            or "đã giao hàng thành công" in full_text
            or "đã nhận hàng" in full_text
            or "ký nhận thành công" in full_text
        )
        and not is_delivering
    )

    is_cancelled = (
        "cancelled" in status_lower
        or "canceled" in status_lower
        or "return_done" in status_lower
        or "returned" in status_lower
        or "đã hủy" in full_text
        or "hủy đơn" in full_text
        or "chuyển hoàn thành công" in full_text
        or "trả hàng thành công" in full_text
    )
    
    return {
        "tracking_number": data.get("sls_tracking_number", clean_code),
        "latest_status": translate_spx_status(status_title, detail_msg, location),
        "raw_status": status_title,
        "detail_message": detail_msg,
        "location": location,
        "timestamp": checkpoint_time,
        "is_completed": 1 if (is_delivered or is_cancelled) else 0,
        "is_delivered": is_delivered,
        "is_cancelled": is_cancelled,
        "history_count": len(sorted_history),
        "history": sorted_history,
        "latest_event": latest_event
    }
