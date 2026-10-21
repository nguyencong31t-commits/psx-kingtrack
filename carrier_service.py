"""
Module Carrier Service: Hỗ trợ tra cứu vận đơn đa hãng (SPX, VNPost/EMS, GHTK, ViettelPost)
Chuẩn hóa dữ liệu hành trình, tự động nhận diện hãng và format giao diện thông báo đồng bộ.
"""

import os
import re
import html
import time
import json
import io
import base64
import threading
import logging
from datetime import datetime, timezone, timedelta
from typing import Optional, Dict, Any, List

# Xử lý hình ảnh và OCR cho hệ thống giải Captcha đa hãng tự động
try:
    from PIL import Image, ImageEnhance
    HAS_PIL = True
except ImportError:
    HAS_PIL = False

# Sử dụng curl_cffi nếu có, fallback về urllib
try:
    from curl_cffi import requests as cffi_requests
    HAS_CURL_CFFI = True
except ImportError:
    HAS_CURL_CFFI = False
    import urllib.request
    import urllib.error

# Import trực tiếp logic từ spx_service
from spx_service import (
    fetch_spx_tracking as _fetch_spx_internal,
    format_timestamp,
    translate_spx_status,
    estimate_delivery_date,
    VN_TZ
)

logger = logging.getLogger(__name__)

# Cấu hình OCR siêu nhẹ:
# - VNPost: Sử dụng Tesseract OCR siêu nhẹ (giải trong 0.04s, RAM cực thấp)
try:
    import pytesseract
    HAS_PYTESSERACT = True
    if os.name == 'nt':
        win_tess = r"C:\Program Files\Tesseract-OCR\tesseract.exe"
        if os.path.exists(win_tess):
            pytesseract.pytesseract.tesseract_cmd = win_tess
except ImportError:
    HAS_PYTESSERACT = False

def _solve_captcha_image(img_bytes: bytes, carrier: str = "GENERIC") -> str:
    """Hàm giải Captcha tự động bằng Tesseract OCR siêu tốc (chuyên dụng cho VNPost)."""
    if not img_bytes:
        return ""

    if HAS_PYTESSERACT and HAS_PIL:
        try:
            img = Image.open(io.BytesIO(img_bytes))
            w, h = img.size
            resized = img.resize((w * 3, h * 3), Image.LANCZOS)
            gray = resized.convert('L')
            enh = ImageEnhance.Contrast(gray).enhance(2.5)
            bw = enh.point(lambda p: 0 if p < 160 else 255)
            config = '--psm 7 -c tessedit_char_whitelist=0123456789abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ'
            raw_text = pytesseract.image_to_string(bw, config=config).strip()
            clean = re.sub(r'[^a-zA-Z0-9]', '', raw_text)
            if len(clean) == 4:
                return clean
        except Exception as e:
            logger.debug(f"Tesseract OCR error: {e}")

    return ""

# Tên và biểu tượng đại diện cho các hãng vận chuyển
CARRIER_NAMES = {
    "SPX": "SPX Express",
    "VNPOST": "VNPost / EMS",
    "GHTK": "GHTK",
    "VTP": "ViettelPost",
    "UNKNOWN": "Vận chuyển"
}

CARRIER_ICONS = {
    "SPX": "🔔",
    "VNPOST": "📮",
    "GHTK": "⚡",
    "VTP": "🔴",
    "UNKNOWN": "📦"
}

CARRIER_URL_TEMPLATES = {
    "SPX": "https://spx.vn/track?{code}",
    "VNPOST": "https://ems.com.vn/tra-cuu-buu-gui?code={code}",
    "GHTK": "https://i.ghtk.vn/{code}",
    "VTP": "https://viettelpost.com.vn/tra-cuu-hanh-trinh-don/?order={code}"
}

DEFAULT_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
    "Accept-Language": "vi-VN,vi;q=0.9,en-US;q=0.8"
}

def parse_datetime_to_timestamp(dt_str: Any) -> int:
    """Chuyển đổi các định dạng chuỗi ngày giờ tiếng Việt hoặc ISO sang Unix timestamp (UTC+7)."""
    if not dt_str:
        return 0
    if isinstance(dt_str, (int, float)):
        return int(dt_str) if dt_str < 1e11 else int(dt_str / 1000)
    
    clean_str = str(dt_str).strip()
    
    # 1. Thử phân tích cú pháp chuẩn ISO (hỗ trợ múi giờ như 2026-10-20T16:06:03+07:00)
    try:
        dt = datetime.fromisoformat(clean_str)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=VN_TZ)
        return int(dt.timestamp())
    except Exception:
        pass

    # 2. Thử các định dạng ngày tháng tiếng Việt thông dụng
    formats = [
        "%d/%m/%Y %H:%M:%S",
        "%d/%m/%Y %H:%M",
        "%Y-%m-%d %H:%M:%S",
        "%Y-%m-%dT%H:%M:%S",
        "%Y-%m-%dT%H:%M:%S.%f",
        "%Y-%m-%dT%H:%M:%SZ",
        "%d-%m-%Y %H:%M:%S",
        "%d-%m-%Y %H:%M",
        "%d/%m/%Y",
        "%Y-%m-%d"
    ]
    for fmt in formats:
        try:
            dt = datetime.strptime(clean_str, fmt)
            dt = dt.replace(tzinfo=VN_TZ)
            return int(dt.timestamp())
        except Exception:
            continue
            
    return int(time.time())

def detect_carrier(code: str) -> str:
    """
    Tự động nhận diện hãng vận chuyển dựa trên định dạng mã vận đơn:
    - SPX: SPXVN... hoặc SLS...
    - VNPost/EMS: Chuẩn UPU 13 ký tự (2 chữ cái đầu, 9 số, 2 chữ cái đuôi e.g. EB123456789VN) hoặc đầu EMS, VNPOST
    - GHTK: Bắt đầu bằng S... (e.g. S22784532) hoặc GHTK...
    - ViettelPost: Bắt đầu bằng VT... hoặc dãy 10-12 số
    """
    clean = (code or "").strip().upper()
    if not clean:
        return "UNKNOWN"
        
    # 1. SPX Express
    if clean.startswith("SPXVN") or clean.startswith("SLS") or (clean.startswith("SPX") and len(clean) >= 8):
        return "SPX"
        
    # 2. VNPost / EMS: Định dạng UPU S10 (2 chữ cái, 9 số, 2 chữ cái) hoặc tiền tố bưu điện
    if re.match(r'^[A-Z]{2}\d{9}[A-Z]{2}$', clean):
        return "VNPOST"
    if (clean.startswith("EMS") or clean.startswith("VNPOST")) and len(clean) >= 8:
        return "VNPOST"
        
    # 3. GHTK: Bắt đầu bằng S theo sau là số (tối thiểu 6 chữ số), S.xxx hoặc GHTK
    if clean.startswith("GHTK") and len(clean) >= 8:
        return "GHTK"
    if re.match(r'^S\d{6,16}$', clean) or (clean.startswith("S.") and len(clean) >= 6):
        return "GHTK"
        
    # 4. ViettelPost: Bắt đầu bằng VT / VTP kèm số hoặc dãy số 10-12 số
    if (clean.startswith("VT") or clean.startswith("VTP")) and len(clean) >= 7 and re.search(r'\d', clean):
        return "VTP"
    if re.match(r'^\d{10,12}$', clean):
        return "VTP"
        
    return "UNKNOWN"

def get_tracking_url(code: str, carrier: str = None) -> str:
    """Tạo đường link tra cứu chính hãng trực tiếp từ mã vận đơn."""
    clean_code = (code or "").strip()
    c = carrier or detect_carrier(clean_code)
    template = CARRIER_URL_TEMPLATES.get(c, CARRIER_URL_TEMPLATES["SPX"])
    return template.format(code=clean_code)

def _http_get(url: str, headers: dict = None, timeout: int = 12) -> Optional[str]:
    """Helper gửi HTTP GET request với fallback giữa curl_cffi và urllib."""
    req_headers = {**DEFAULT_HEADERS, **(headers or {})}
    if HAS_CURL_CFFI:
        try:
            session = cffi_requests.Session(impersonate="chrome124")
            resp = session.get(url, headers=req_headers, timeout=timeout)
            if resp.status_code == 200:
                return resp.text
            logger.debug(f"HTTP GET {url} returned {resp.status_code}")
            return None
        except Exception as e:
            logger.debug(f"curl_cffi error on {url}: {e}, trying urllib")
            
    try:
        import urllib.request
        req = urllib.request.Request(url, headers=req_headers)
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            if resp.status == 200:
                return resp.read().decode("utf-8", errors="ignore")
    except Exception as e:
        logger.error(f"HTTP GET error for {url}: {e}")
        
    return None

def _extract_val(d: dict, *keys, default: str = "") -> str:
    """Trích xuất linh hoạt giá trị từ dict hỗ trợ nhiều kiểu đặt tên key hoa/thường."""
    if not isinstance(d, dict):
        return default
    for k in keys:
        if k in d and d[k] is not None:
            return str(d[k]).strip()
    d_lower = {str(k).lower(): v for k, v in d.items() if v is not None}
    for k in keys:
        kl = str(k).lower()
        if kl in d_lower:
            return str(d_lower[kl]).strip()
    return default

# ==============================================================================
# 1. TRA CỨU VNPOST / EMS
# ==============================================================================
def _fetch_vnpost_portal_live(clean_code: str) -> Optional[Dict[str, Any]]:
    """
    Tra cứu VNPost trực tiếp qua cổng vnpost.vn với Tesseract OCR siêu nhẹ
    (chuyên dụng cho các mã bưu kiện UPU/bưu phẩm như CQ..., CC..., CP..., RN...).
    """
    if not HAS_CURL_CFFI or not HAS_PIL:
        return None

    try:
        session = cffi_requests.Session(impersonate="chrome124")
    except Exception as e:
        logger.error(f"Cannot initialize curl_cffi session for VNPost: {e}")
        return None

    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
        "Referer": "https://vnpost.vn/vi",
        "X-Requested-With": "XMLHttpRequest",
        "Accept": "application/json, text/plain, */*"
    }

    try:
        session.get("https://vnpost.vn/vi", headers=headers, timeout=8)
    except Exception:
        pass

    for attempt in range(8):
        try:
            r_cap = session.get("https://vnpost.vn/handle-captcha/refresh-captcha", headers=headers, timeout=8)
            if r_cap.status_code != 200:
                continue
            cap_url = r_cap.json().get("captcha")
            if not cap_url:
                continue
            img_resp = session.get(cap_url, headers=headers, timeout=8)
            if img_resp.status_code != 200:
                continue

            captcha_text = _solve_captcha_image(img_resp.content, carrier="VNPOST")
            if len(captcha_text) != 4:
                continue

            track_url = f"https://vnpost.vn/postcode/thong-tin?post_code={clean_code}&captcha={captcha_text}"
            r_track = session.get(track_url, headers=headers, timeout=10)
            if r_track.status_code == 200:
                try:
                    data = r_track.json()
                    if data.get("info") or data.get("locate"):
                        return data
                except Exception:
                    continue
        except Exception as e:
            logger.debug(f"VNPost live attempt {attempt+1} error: {e}")
            continue

    return None

def _parse_myems_data(clean_code: str, data: dict, label: str = "") -> Optional[Dict[str, Any]]:
    """Parse dữ liệu từ API myems.vn sang schema tracking thống nhất."""
    list_dinh_vi = data.get("List_TBL_DINH_VI") or []
    list_delivery = data.get("List_TBL_DELIVERY") or []
    tbl_info = data.get("TBL_INFO") or {}
    
    if not list_dinh_vi and not list_delivery and not tbl_info:
        return None
        
    history: List[Dict[str, Any]] = []
    
    for item in list_dinh_vi:
        if not isinstance(item, dict):
            continue
        dt_str = _extract_val(item, "DATE_TIME", "NgayGio", "DateTime", "Date", "TIME", "ThoiGian")
        pos = _extract_val(item, "POS_NAME", "POST_OFFICE", "Location", "TEN_BUU_CUC", "BuuCuc", "DiaDiem")
        status = _extract_val(item, "STATUS_NAME", "STATUS", "TRANG_THAI", "MoTa", "Description", "TrangThai", "NOTE", "GhiChu")
        ts = parse_datetime_to_timestamp(dt_str)
        history.append({
            "status": status or "Đang vận chuyển",
            "time": format_timestamp(ts) if ts else dt_str,
            "timestamp": ts,
            "location": pos,
            "message": status
        })
        
    delivery_success = False
    for item in list_delivery:
        if not isinstance(item, dict):
            continue
        dt_str = _extract_val(item, "DATE_TIME", "NgayGio", "DateTime", "Date", "TIME")
        pos = _extract_val(item, "POS_NAME", "POST_OFFICE", "Location", "BUU_TA", "DELIVERY_MAN")
        deliv_res = _extract_val(item, "DELIVERY_RESULT", "STATUS_NAME", "TRANG_THAI_PHAT", "STATUS", "KetQuaPhat")
        receiver = _extract_val(item, "RECEIVER_NAME", "NGUOI_NHAN", "NguoiNhan")
        
        status_line = deliv_res or "Phát hàng"
        if receiver:
            status_line += f" (Người nhận: {receiver})"
            
        ts = parse_datetime_to_timestamp(dt_str)
        history.append({
            "status": status_line,
            "time": format_timestamp(ts) if ts else dt_str,
            "timestamp": ts,
            "location": pos,
            "message": status_line
        })
        
        deliv_lower = deliv_res.lower()
        if any(w in deliv_lower for w in ["thành công", "đã phát", "ký nhận", "delivered"]):
            delivery_success = True
            
    history.sort(key=lambda x: x.get("timestamp") or 0)
    latest_event = history[-1] if history else {}
    
    latest_status = latest_event.get("status") or "Đang vận chuyển"
    latest_time = latest_event.get("timestamp") or 0
    latest_loc = latest_event.get("location") or ""
    
    status_lower = latest_status.lower()
    is_delivered = delivery_success or any(w in status_lower for w in [
        "phát thành công", "đã phát", "giao thành công", "đã giao", "người nhận đã ký", "ký nhận", "buyer received"
    ])
    is_cancelled = any(w in status_lower for w in [
        "chuyển hoàn", "hủy", "trả lại", "không phát được", "chuyển trả", "returned", "cancelled"
    ])
    is_completed = is_delivered or is_cancelled
    
    edd = ""
    if not is_completed and history:
        first_ts = history[0].get("timestamp") or 0
        if first_ts:
            edd_dt = datetime.fromtimestamp(first_ts + 3 * 86400, tz=VN_TZ)
            edd = f"~{edd_dt.strftime('%d/%m')}"
            
    return {
        "carrier": "VNPOST",
        "carrier_name": CARRIER_NAMES["VNPOST"],
        "tracking_number": clean_code,
        "label": label or "",
        "latest_status": latest_status,
        "timestamp": latest_time,
        "location": latest_loc,
        "estimated_delivery": edd or None,
        "is_delivered": is_delivered,
        "is_completed": is_completed,
        "is_cancelled": is_cancelled,
        "history": history,
        "tracking_url": get_tracking_url(clean_code, "VNPOST")
    }

def _parse_vnpost_portal_data(clean_code: str, data: dict, label: str = "") -> Optional[Dict[str, Any]]:
    """Chuyển đổi dữ liệu từ cổng tra cứu vnpost.vn sang schema tracking thống nhất."""
    info = data.get("info") or {}
    locates = data.get("locate") or []
    if not locates and not info:
        return None

    history: List[Dict[str, Any]] = []
    for item in locates:
        if not isinstance(item, dict):
            continue
        dt_str = item.get("TraceDate") or f"{item.get('Date', '')} {item.get('TimeDetail', '')}".strip()
        ts = parse_datetime_to_timestamp(dt_str)
        st_text = item.get("StatusText") or "Đang vận chuyển"
        loc = item.get("VI_TRI") or ""
        history.append({
            "status": st_text,
            "time": format_timestamp(ts) if ts else dt_str,
            "timestamp": ts,
            "location": loc,
            "message": st_text
        })

    history.sort(key=lambda x: x.get("timestamp") or 0)
    latest_event = history[-1] if history else {}
    latest_status = latest_event.get("status") or "Đang vận chuyển"
    latest_time = latest_event.get("timestamp") or int(time.time())
    latest_loc = latest_event.get("location") or info.get("BC_GUI") or ""

    st_lower = latest_status.lower()
    is_delivered = any(w in st_lower for w in [
        "phát thành công", "đã phát", "giao thành công", "đã giao", "người nhận đã ký", "ký nhận", "buyer received"
    ])
    is_cancelled = any(w in st_lower for w in [
        "chuyển hoàn", "hủy", "trả lại", "không phát được", "chuyển trả", "returned", "cancelled"
    ])
    is_completed = is_delivered or is_cancelled

    edd = ""
    if not is_completed and history:
        first_ts = history[0].get("timestamp") or 0
        if first_ts:
            edd_dt = datetime.fromtimestamp(first_ts + 3 * 86400, tz=VN_TZ)
            edd = f"~{edd_dt.strftime('%d/%m')}"

    return {
        "carrier": "VNPOST",
        "carrier_name": CARRIER_NAMES["VNPOST"],
        "tracking_number": clean_code,
        "label": label or "",
        "latest_status": latest_status,
        "timestamp": latest_time,
        "location": latest_loc,
        "estimated_delivery": edd or None,
        "is_delivered": is_delivered,
        "is_completed": is_completed,
        "is_cancelled": is_cancelled,
        "history": history,
        "tracking_url": get_tracking_url(clean_code, "VNPOST")
    }

def fetch_vnpost_tracking(code: str, label: str = "") -> Optional[Dict[str, Any]]:
    """
    Tra cứu vận đơn VNPost / EMS:
    1. Nếu là mã chuyển phát nhanh EMS (mã bắt đầu bằng E như EB..., EM... hoặc EMS): ưu tiên cổng API MyEMS (siêu tốc, không cần captcha)
    2. Nếu là mã bưu phẩm/bưu kiện thông thường (CQ..., CC..., CP..., RN...): tra cứu trực tiếp qua cổng vnpost.vn (giải captcha siêu nhẹ 0.04s)
    3. Tự động chuyển đổi qua lại giữa 2 cổng nếu một bên không có dữ liệu.
    """
    clean_code = code.strip().upper()
    is_ems_prefix = clean_code.startswith("E") or clean_code.startswith("EMS")

    def _try_ems() -> Optional[Dict[str, Any]]:
        itemcode_param = clean_code if clean_code.endswith("EMS") else f"{clean_code}EMS"
        api_url = f"https://api.myems.vn/TrackAndTraceItemCode?itemcode={itemcode_param}&language=0"
        headers = {
            "Accept": "application/json, text/plain, */*",
            "Referer": "https://ems.com.vn/",
            "Origin": "https://ems.com.vn"
        }
        raw_text = _http_get(api_url, headers=headers, timeout=10)
        if raw_text:
            try:
                data = json.loads(raw_text)
                if isinstance(data, dict) and data.get("Code") == "00":
                    return _parse_myems_data(clean_code, data, label)
            except Exception:
                pass
        return None

    def _try_vnpost_portal() -> Optional[Dict[str, Any]]:
        portal_data = _fetch_vnpost_portal_live(clean_code)
        if portal_data:
            return _parse_vnpost_portal_data(clean_code, portal_data, label)
        return None

    if is_ems_prefix:
        res = _try_ems()
        if res:
            return res
        return _try_vnpost_portal()
    else:
        res = _try_vnpost_portal()
        if res:
            return res
        return _try_ems()

    return None

# ==============================================================================
# 2. TRA CỨU GHTK
# ==============================================================================
def fetch_ghtk_tracking(code: str, label: str = "") -> Optional[Dict[str, Any]]:
    """
    Tra cứu vận đơn GHTK qua:
    1. SSR https://i.ghtk.vn/{code} (trích xuất __NEXT_DATA__)
    2. Fallback gọi API iggw-service nếu SSR không có logs
    """
    clean_code = code.strip().upper()
    ssr_url = f"https://i.ghtk.vn/{clean_code}"
    html_text = _http_get(ssr_url, timeout=12)
    
    page_props: Dict[str, Any] = {}
    if html_text:
        m = re.search(r'<script id="__NEXT_DATA__" type="application/json">(.*?)</script>', html_text)
        if m:
            try:
                next_data = json.loads(m.group(1))
                page_props = next_data.get("props", {}).get("pageProps", {})
            except Exception as e:
                logger.error(f"Lỗi parse __NEXT_DATA__ GHTK {clean_code}: {e}")
                
    has_error = page_props.get("hasError", False)
    package_order = page_props.get("packageOrder") or page_props.get("tracking_code")
    
    # Thử gọi thêm iggw-service nếu SSR chưa có timeline hoặc báo lỗi
    logs = (
        page_props.get("logs") or 
        page_props.get("timeline") or 
        page_props.get("packageLog") or 
        page_props.get("aggregated_timelines") or 
        []
    )
    
    if not logs and not has_error:
        # Gọi API iggw
        api_url = f"https://iggw-service.giaohangtietkiem.vn/api/v2/package-tracking/get-package?q={clean_code}"
        headers = {
            "Accept": "application/json, text/plain, */*",
            "Referer": "https://i.ghtk.vn/",
            "Origin": "https://i.ghtk.vn"
        }
        api_text = _http_get(api_url, headers=headers, timeout=10)
        if api_text:
            try:
                api_json = json.loads(api_text)
                api_data = api_json.get("data") or {}
                logs = (
                    api_data.get("aggregated_timelines") or 
                    api_data.get("logs") or 
                    api_data.get("timeline") or 
                    []
                )
                if not package_order:
                    package_order = api_data.get("package_order") or api_data.get("tracking_code")
            except Exception:
                pass
                
    # Nếu mã thực sự không tồn tại hoặc báo lỗi
    if has_error and not logs:
        return None
        
    history: List[Dict[str, Any]] = []
    for item in logs:
        if not isinstance(item, dict):
            continue
        content = _extract_val(item, "content", "status_text", "message", "action_text", "description")
        dt_val = item.get("timestamp") or item.get("action_time") or item.get("time") or _extract_val(item, "time", "date")
        loc = _extract_val(item, "hub_name", "location", "station", "kho")
        ts = parse_datetime_to_timestamp(dt_val)
        
        history.append({
            "status": content or "Đang xử lý",
            "time": format_timestamp(ts) if ts else str(dt_val),
            "timestamp": ts,
            "location": loc,
            "message": content
        })
        
    history.sort(key=lambda x: x.get("timestamp") or 0)
    latest_event = history[-1] if history else {}
    
    shop_name = page_props.get("shopName", "")
    latest_status = latest_event.get("status") or (f"Shop: {shop_name}" if shop_name else "Đang vận chuyển")
    latest_time = latest_event.get("timestamp") or int(time.time())
    latest_loc = latest_event.get("location") or ""
    
    status_lower = latest_status.lower()
    is_delivered = any(w in status_lower for w in [
        "giao hàng thành công", "đã giao hàng", "giao thành công", "đã nhận hàng", "ký nhận", "đối soát"
    ])
    is_cancelled = any(w in status_lower for w in [
        "hủy đơn", "đã hủy", "trả hàng", "chuyển hoàn", "không nhận hàng"
    ])
    is_completed = is_delivered or is_cancelled
    
    edd = ""
    if not is_completed:
        if "đang giao" in status_lower or "đi giao" in status_lower:
            edd = "Hôm nay"
        elif history:
            first_ts = history[0].get("timestamp") or 0
            if first_ts:
                edd_dt = datetime.fromtimestamp(first_ts + 3 * 86400, tz=VN_TZ)
                edd = f"~{edd_dt.strftime('%d/%m')}"
                
    return {
        "carrier": "GHTK",
        "carrier_name": CARRIER_NAMES["GHTK"],
        "tracking_number": package_order or clean_code,
        "label": label or "",
        "latest_status": latest_status,
        "timestamp": latest_time,
        "location": latest_loc,
        "estimated_delivery": edd or None,
        "is_delivered": is_delivered,
        "is_completed": is_completed,
        "is_cancelled": is_cancelled,
        "history": history,
        "tracking_url": get_tracking_url(clean_code, "GHTK")
    }

# ==============================================================================
# 3. VIETTELPOST (HIỆN KHÔNG HỖ TRỢ THEO YÊU CẦU)
# ==============================================================================
def fetch_viettelpost_tracking(code: str, label: str = "") -> Dict[str, Any]:
    """ViettelPost hiện không được hỗ trợ theo cấu hình hệ thống."""
    clean_code = (code or "").strip().upper()
    return {
        "carrier": "VTP",
        "carrier_name": CARRIER_NAMES["VTP"],
        "tracking_number": clean_code,
        "label": label or "",
        "latest_status": "Đơn hàng của vtpost không hỗ trợ",
        "timestamp": int(time.time()),
        "location": "",
        "estimated_delivery": None,
        "is_delivered": False,
        "is_completed": True,
        "is_cancelled": False,
        "history": [],
        "tracking_url": "",
        "is_fallback": True,
        "unsupported": True
    }

# ==============================================================================
# 4. TRA CỨU SPX EXPRESS (WRAPPER)
# ==============================================================================
def fetch_spx_unified(code: str, label: str = "") -> Optional[Dict[str, Any]]:
    """Tra cứu bưu gửi SPX và chuẩn hóa sang cấu trúc chung."""
    clean_code = code.strip().upper()
    info = _fetch_spx_internal(clean_code, label)
    if not info:
        return None
        
    tracking_num = info.get("tracking_number", clean_code)
    history_raw = info.get("history", [])
    history_unified = []
    
    for ev in history_raw:
        ts = ev.get("timestamp") or ev.get("checkpoint_time", 0)
        st = ev.get("status") or ev.get("message") or ""
        msg = ev.get("message") or ev.get("description") or ""
        loc = ev.get("location") or ev.get("hub_name", "")

        # Tự động trích xuất vị trí kho/hub từ thông điệp của SPX
        if not loc and msg:
            m_at = re.search(r'at \[(.*?)\]', msg, re.IGNORECASE)
            m_to = re.search(r'transporting to \[(.*?)\]', msg, re.IGNORECASE)
            m_b = re.search(r'^\[(.*?)\]', msg)
            if m_at:
                loc = m_at.group(1)
            elif m_to:
                loc = f"Đang chuyển đến {m_to.group(1)}"
            elif m_b and not m_b.group(1).startswith("TO"):
                loc = m_b.group(1)

        st_vn = translate_spx_status(st, msg, loc)
        history_unified.append({
            "status": st_vn,
            "raw_status": st,
            "message": msg,
            "time": format_timestamp(ts),
            "timestamp": ts,
            "location": loc
        })
        
    latest_loc = info.get("location", "")
    if not latest_loc and history_unified:
        latest_loc = history_unified[-1].get("location", "")

    return {
        "carrier": "SPX",
        "carrier_name": CARRIER_NAMES["SPX"],
        "tracking_number": tracking_num,
        "label": label or "",
        "latest_status": info.get("latest_status", "Đang vận chuyển"),
        "timestamp": info.get("timestamp", 0),
        "location": latest_loc,
        "estimated_delivery": info.get("estimated_delivery") or None,
        "is_delivered": bool(info.get("is_delivered")),
        "is_completed": bool(info.get("is_completed")),
        "is_cancelled": bool(info.get("is_cancelled")),
        "history": history_unified,
        "tracking_url": get_tracking_url(tracking_num, "SPX")
    }

# ==============================================================================
# 5. HÀM CHUẨN HÓA TRUY VẤN TỔNG HỢP (UNIFIED ROUTER)
# ==============================================================================
def fetch_tracking_unified(code: str, label: str = "", carrier: str = None) -> Optional[Dict[str, Any]]:
    """
    Tra cứu vận đơn thống nhất cho tất cả các hãng.
    Nếu carrier không được truyền vào, hàm sẽ tự động nhận diện bằng detect_carrier().
    """
    clean_code = (code or "").strip().upper()
    if not clean_code:
        return None
        
    actual_carrier = (carrier or detect_carrier(clean_code)).upper()
    
    if actual_carrier == "SPX":
        return fetch_spx_unified(clean_code, label)
    elif actual_carrier == "VNPOST":
        return fetch_vnpost_tracking(clean_code, label)
    elif actual_carrier == "GHTK":
        return fetch_ghtk_tracking(clean_code, label)
    elif actual_carrier == "VTP":
        return fetch_viettelpost_tracking(clean_code, label)
    else:
        # Nếu chưa rõ hãng, thử lần lượt SPX -> VNPOST -> GHTK -> VTP fallback
        res = fetch_spx_unified(clean_code, label)
        if res:
            return res
        res = fetch_vnpost_tracking(clean_code, label)
        if res:
            return res
        res = fetch_ghtk_tracking(clean_code, label)
        if res:
            return res
        return fetch_viettelpost_tracking(clean_code, label)

# ==============================================================================
# 6. CÁC HÀM FORMAT TIN NHẮN ĐỒNG BỘ
# ==============================================================================
def format_carrier_card(data: dict) -> str:
    """
    Định dạng thẻ thông tin (Card) cho thông báo cập nhật mới hoặc tra cứu đơn hàng:
    🔔 SPX Express: ...
    ━━━━━━━━━━━━━━━━━━━━━━
    📦 Mã đơn: SPXVN... (Ghi chú)
    🏢 Đơn vị: SPX Express
    📌 Trạng thái: ...
    🕒 Cập nhật: ...
    📅 Dự kiến: ... (nếu có)
    📍 Vị trí: ... (nếu có)
    🌐 Link tra cứu: ...
    """
    carrier = data.get("carrier", "SPX")
    code = data.get("tracking_number", "")
    
    if carrier == "VTP" or data.get("unsupported"):
        return f"❌ <b>Đơn hàng của vtpost không hỗ trợ.</b>\n📦 Mã đơn: <code>{html.escape(code)}</code>"

    carrier_name = data.get("carrier_name") or CARRIER_NAMES.get(carrier, "Vận chuyển")
    icon = CARRIER_ICONS.get(carrier, "📦")
    code = data.get("tracking_number", "")
    label = (data.get("label") or "").strip()
    latest_status = data.get("latest_status", "Đang cập nhật")
    ts = data.get("timestamp", 0)
    time_str = format_timestamp(ts) if ts else "Đang cập nhật"
    loc = data.get("location", "")
    edd = data.get("estimated_delivery") or ""
    url = data.get("tracking_url") or get_tracking_url(code, carrier)
    is_fallback = data.get("is_fallback", False)
    is_delivered = data.get("is_delivered", False)
    
    # Tiêu đề mã đơn kèm ghi chú nếu có
    if label and label.lower() not in ("đơn hàng spx", "đơn spx", "tra cứu nhanh", "none", ""):
        pkg_title = f"<code>{html.escape(code)}</code> (<i>{html.escape(label)}</i>)"
    else:
        pkg_title = f"<code>{html.escape(code)}</code>"
        
    lines = [
        f"{icon} <b>{carrier_name.upper()} UPDATE</b>",
        "━━━━━━━━━━━━━━━━━━━━━━",
        f"📦 <b>Mã đơn:</b> {pkg_title}",
        f"🏢 <b>Hãng:</b> {html.escape(carrier_name)}",
        f"📌 <b>Trạng thái:</b> <b>{html.escape(latest_status)}</b>",
        f"🕒 <b>Cập nhật:</b> {time_str}"
    ]
    
    if edd and not is_delivered and not is_fallback:
        lines.append(f"📅 <b>Dự kiến giao:</b> <code>{html.escape(edd)}</code>")
    if loc and not is_fallback:
        lines.append(f"📍 <b>Vị trí:</b> {html.escape(loc)}")
        
    return "\n".join(lines)

def format_carrier_journey(data: dict) -> str:
    """Định dạng toàn bộ lịch sử hành trình chi tiết (Full Journey Timeline)."""
    carrier = data.get("carrier", "SPX")
    code = data.get("tracking_number", "")
    
    if carrier == "VTP" or data.get("unsupported"):
        return f"❌ <b>Đơn hàng của vtpost không hỗ trợ.</b>\n📦 Mã đơn: <code>{html.escape(code)}</code>"

    carrier_name = data.get("carrier_name") or CARRIER_NAMES.get(carrier, "Vận chuyển")
    icon = CARRIER_ICONS.get(carrier, "📦")
    code = data.get("tracking_number", "")
    label = (data.get("label") or "").strip()
    latest_status = data.get("latest_status", "Đang cập nhật")
    ts = data.get("timestamp", 0)
    time_str = format_timestamp(ts) if ts else "Đang cập nhật"
    history = data.get("history", [])
    is_fallback = data.get("is_fallback", False)
    
    if label and label.lower() not in ("đơn hàng spx", "đơn spx", "tra cứu nhanh", "none", ""):
        pkg_title = f"<code>{html.escape(code)}</code> (<i>{html.escape(label)}</i>)"
    else:
        pkg_title = f"<code>{html.escape(code)}</code>"
        
    lines = [
        f"📜 <b>CHI TIẾT TOÀN BỘ HÀNH TRÌNH ({carrier_name.upper()})</b>",
        "━━━━━━━━━━━━━━━━━━━━━━",
        f"📦 <b>Mã đơn:</b> {pkg_title}",
        f"🏢 <b>Đơn vị:</b> {carrier_name}",
        f"📌 <b>Trạng thái:</b> <b>{html.escape(latest_status)}</b>",
        f"🕒 <b>Cập nhật:</b> {time_str}",
        "━━━━━━━━━━━━━━━━━━━━━━"
    ]
    
    if is_fallback or not history:
        lines.append("<i>Chưa có dữ liệu lịch sử các mốc vận chuyển chi tiết từ hãng.</i>")
    else:
        lines.append(f"📋 <b>Lịch sử chi tiết ({len(history)} mốc):</b>\n")
        for idx, ev in enumerate(history, 1):
            t_str = ev.get("time") or format_timestamp(ev.get("timestamp", 0))
            st = ev.get("status", "")
            loc = ev.get("location", "")
            msg = ev.get("message", "")
            
            ev_text = f"<b>{idx}.</b> 🕒 <code>{t_str}</code>\n   🔹 <b>{html.escape(st)}</b>"
            if loc and loc.lower() not in st.lower():
                ev_text += f"\n   📍 Địa điểm: {html.escape(loc)}"
            lines.append(ev_text)
            
    return "\n".join(lines)

def format_carrier_list_item(item: Any, idx: int = 1) -> str:
    """
    Định dạng hiển thị từng dòng đơn hàng cho lệnh /list.
    item có thể là sqlite3 Row / dict từ CSDL hoặc dict unified.
    """
    if hasattr(item, "keys"):
        d = dict(item)
    elif isinstance(item, dict):
        d = item
    else:
        d = {}
        
    code = d.get("tracking_number", "")
    carrier = d.get("carrier") or detect_carrier(code)
    carrier_name = d.get("carrier_name") or CARRIER_NAMES.get(carrier, "Vận chuyển")
    icon = CARRIER_ICONS.get(carrier, "📦")
    label = (d.get("label") or "").strip()
    status = d.get("last_status") or d.get("latest_status") or "Đang vận chuyển"
    ts = d.get("last_timestamp") or d.get("timestamp") or 0
    t_str = format_timestamp(ts) if ts else "N/A"
    edd = d.get("estimated_delivery") or ""
    
    code_esc = html.escape(code)
    label_part = f" (<i>{html.escape(label)}</i>)" if label and label.lower() not in ("none", "") else ""
    edd_badge = f" <b>[Dự kiến: {html.escape(edd)}]</b>" if edd else ""
    
    return (
        f"<b>{idx}.</b> <code>{code_esc}</code>{edd_badge}{label_part}\n"
        f"   {icon} {carrier_name} | 📌 <b>{html.escape(status)}</b>\n"
        f"   🕒 {t_str}\n"
        f"   👉 Xem hành trình: /check_{code_esc}"
    )
