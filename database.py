"""
database.py - Quản lý cơ sở dữ liệu tracking hàng hóa.
Hỗ trợ kiến trúc Hybrid:
  - Nếu có biến môi trường TURSO_DATABASE_URL & TURSO_AUTH_TOKEN: Kết nối Turso Cloud (libSQL).
    Dữ liệu được lưu trữ vĩnh viễn trên Cloud, không bao giờ mất dù Render có khởi động lại bao nhiêu lần.
  - Nếu không cấu hình Turso: Tự động fallback về file SQLite cục bộ (aiosqlite).
"""

import os
import aiosqlite
from typing import List, Dict, Optional, Any

# Hỗ trợ Turso Cloud qua libsql-client
try:
    import libsql_client
    HAS_LIBSQL = True
except ImportError:
    HAS_LIBSQL = False

# Đường dẫn CSDL cục bộ (Fallback)
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
_env_db = os.getenv("DATABASE_PATH", "trackings.db")
DB_PATH = _env_db if os.path.isabs(_env_db) else os.path.join(BASE_DIR, _env_db)

# Cấu hình Turso Cloud
TURSO_DATABASE_URL = os.getenv("TURSO_DATABASE_URL", "").strip()
TURSO_AUTH_TOKEN = os.getenv("TURSO_AUTH_TOKEN", "").strip()


def is_turso_enabled() -> bool:
    """Kiểm tra hệ thống có đang sử dụng Turso Cloud hay không."""
    return bool(HAS_LIBSQL and TURSO_DATABASE_URL)


def get_turso_client():
    """Khởi tạo Turso libSQL Client."""
    return libsql_client.create_client(
        url=TURSO_DATABASE_URL,
        auth_token=TURSO_AUTH_TOKEN if TURSO_AUTH_TOKEN else None
    )


async def _execute_sql(sql: str, params: tuple = ()) -> int:
    """Thực thi lệnh INSERT/UPDATE/DELETE và trả về số dòng bị ảnh hưởng."""
    if is_turso_enabled():
        async with get_turso_client() as client:
            rs = await client.execute(sql, list(params))
            return rs.rows_affected
    else:
        async with aiosqlite.connect(DB_PATH) as db:
            cursor = await db.execute(sql, params)
            await db.commit()
            return cursor.rowcount


async def _fetch_all(sql: str, params: tuple = ()) -> List[Dict[str, Any]]:
    """Thực thi lệnh SELECT và trả về danh sách các bản ghi dạng Dict."""
    if is_turso_enabled():
        async with get_turso_client() as client:
            rs = await client.execute(sql, list(params))
            return [dict(zip(rs.columns, tuple(row))) for row in rs.rows]
    else:
        async with aiosqlite.connect(DB_PATH) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(sql, params) as cursor:
                rows = await cursor.fetchall()
                return [dict(row) for row in rows]


async def _fetch_one(sql: str, params: tuple = ()) -> Optional[Dict[str, Any]]:
    """Thực thi lệnh SELECT và trả về 1 bản ghi dạng Dict hoặc None."""
    if is_turso_enabled():
        async with get_turso_client() as client:
            rs = await client.execute(sql, list(params))
            if rs.rows:
                return dict(zip(rs.columns, tuple(rs.rows[0])))
            return None
    else:
        async with aiosqlite.connect(DB_PATH) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(sql, params) as cursor:
                row = await cursor.fetchone()
                return dict(row) if row else None


async def init_db():
    """Khởi tạo cấu trúc bảng trackings và thực hiện migration tự động."""
    create_table_sql = """
        CREATE TABLE IF NOT EXISTS trackings (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            chat_id INTEGER NOT NULL,
            tracking_number TEXT NOT NULL,
            carrier TEXT DEFAULT 'SPX',
            label TEXT,
            last_status TEXT,
            last_timestamp INTEGER DEFAULT 0,
            is_completed INTEGER DEFAULT 0,
            estimated_delivery TEXT DEFAULT '',
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(chat_id, tracking_number)
        )
    """

    if is_turso_enabled():
        async with get_turso_client() as client:
            await client.execute(create_table_sql)

            # Migration: Kiểm tra và thêm cột nếu schema cũ chưa có
            try:
                rs = await client.execute("PRAGMA table_info(trackings)")
                columns = [r[1] for r in rs.rows]

                if "carrier" not in columns:
                    await client.execute("ALTER TABLE trackings ADD COLUMN carrier TEXT DEFAULT 'SPX'")
                if "estimated_delivery" not in columns:
                    await client.execute("ALTER TABLE trackings ADD COLUMN estimated_delivery TEXT DEFAULT ''")
            except Exception:
                pass

            try:
                await client.execute("UPDATE trackings SET carrier = 'SPX' WHERE carrier IS NULL OR carrier = ''")
            except Exception:
                pass
    else:
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute(create_table_sql)

            # Migration cục bộ
            try:
                async with db.execute("PRAGMA table_info(trackings)") as cursor:
                    columns = [row[1] for row in await cursor.fetchall()]

                if "carrier" not in columns:
                    await db.execute("ALTER TABLE trackings ADD COLUMN carrier TEXT DEFAULT 'SPX'")
                if "estimated_delivery" not in columns:
                    await db.execute("ALTER TABLE trackings ADD COLUMN estimated_delivery TEXT DEFAULT ''")
            except Exception:
                pass

            try:
                await db.execute("UPDATE trackings SET carrier = 'SPX' WHERE carrier IS NULL OR carrier = ''")
            except Exception:
                pass

            await db.commit()


async def add_or_update_tracking(
    chat_id: int,
    tracking_number: str,
    label: str,
    status: str,
    timestamp: int,
    estimated_delivery: str = "",
    carrier: str = "SPX"
) -> None:
    """Lưu hoặc cập nhật mã vận đơn cần theo dõi kèm theo hãng vận chuyển."""
    carrier_clean = (carrier or "SPX").strip().upper()
    sql = """
        INSERT INTO trackings (
            chat_id, tracking_number, carrier, label, last_status,
            last_timestamp, is_completed, estimated_delivery, updated_at
        )
        VALUES (?, ?, ?, ?, ?, ?, 0, ?, CURRENT_TIMESTAMP)
        ON CONFLICT(chat_id, tracking_number) DO UPDATE SET
            carrier = excluded.carrier,
            label = excluded.label,
            last_status = excluded.last_status,
            last_timestamp = excluded.last_timestamp,
            is_completed = 0,
            estimated_delivery = excluded.estimated_delivery,
            updated_at = CURRENT_TIMESTAMP
    """
    await _execute_sql(sql, (chat_id, tracking_number, carrier_clean, label, status, timestamp, estimated_delivery))


async def get_active_trackings_by_chat(chat_id: int) -> List[Dict[str, Any]]:
    """Lấy danh sách các đơn đang hoạt động của người dùng."""
    sql = """
        SELECT id, chat_id, tracking_number, COALESCE(carrier, 'SPX') AS carrier,
               label, last_status, last_timestamp, is_completed, estimated_delivery,
               created_at, updated_at
        FROM trackings
        WHERE chat_id = ? AND is_completed = 0
        ORDER BY updated_at DESC
    """
    return await _fetch_all(sql, (chat_id,))


async def remove_tracking(chat_id: int, tracking_number: str) -> bool:
    """Xóa bỏ một đơn hàng khỏi danh sách theo dõi."""
    sql = "DELETE FROM trackings WHERE chat_id = ? AND tracking_number = ?"
    affected = await _execute_sql(sql, (chat_id, tracking_number))
    return affected > 0


async def get_all_active_trackings() -> List[Dict[str, Any]]:
    """Lấy toàn bộ các đơn đang chờ giao trên hệ thống để bot polling."""
    sql = """
        SELECT id, chat_id, tracking_number, COALESCE(carrier, 'SPX') AS carrier,
               label, last_status, last_timestamp, is_completed, estimated_delivery,
               created_at, updated_at
        FROM trackings
        WHERE is_completed = 0
        ORDER BY updated_at DESC
    """
    return await _fetch_all(sql)


async def get_tracking(chat_id: int, tracking_number: str) -> Optional[Dict[str, Any]]:
    """Lấy thông tin chi tiết một đơn hàng theo chat_id và mã vận đơn."""
    sql = """
        SELECT id, chat_id, tracking_number, COALESCE(carrier, 'SPX') AS carrier,
               label, last_status, last_timestamp, is_completed, estimated_delivery,
               created_at, updated_at
        FROM trackings
        WHERE chat_id = ? AND tracking_number = ?
    """
    return await _fetch_one(sql, (chat_id, tracking_number))


async def get_untracked_by_chat(chat_id: int, limit: int = 20) -> List[Dict[str, Any]]:
    """Lấy danh sách các đơn đã hoàn tất gần đây của người dùng."""
    sql = """
        SELECT id, chat_id, tracking_number, COALESCE(carrier, 'SPX') AS carrier,
               label, last_status, last_timestamp, is_completed, estimated_delivery,
               created_at, updated_at
        FROM trackings
        WHERE chat_id = ? AND is_completed = 1
        ORDER BY updated_at DESC
        LIMIT ?
    """
    return await _fetch_all(sql, (chat_id, limit))


async def get_all_trackings_by_chat(chat_id: int) -> List[Dict[str, Any]]:
    """Lấy toàn bộ các đơn hàng (cả đang theo dõi lẫn đã hoàn tất) của người dùng."""
    sql = """
        SELECT id, chat_id, tracking_number, COALESCE(carrier, 'SPX') AS carrier,
               label, last_status, last_timestamp, is_completed, estimated_delivery,
               created_at, updated_at
        FROM trackings
        WHERE chat_id = ?
        ORDER BY updated_at DESC
    """
    return await _fetch_all(sql, (chat_id,))


async def get_active_trackings_by_carrier(carrier: str) -> List[Dict[str, Any]]:
    """Lấy danh sách các đơn đang hoạt động theo hãng vận chuyển cụ thể."""
    carrier_clean = carrier.strip().upper()
    sql = """
        SELECT id, chat_id, tracking_number, COALESCE(carrier, 'SPX') AS carrier,
               label, last_status, last_timestamp, is_completed, estimated_delivery,
               created_at, updated_at
        FROM trackings
        WHERE UPPER(carrier) = ? AND is_completed = 0
        ORDER BY updated_at DESC
    """
    return await _fetch_all(sql, (carrier_clean,))


async def update_tracking_status(
    tracking_id: int,
    status: str,
    timestamp: int,
    is_completed: int = 0,
    estimated_delivery: str = "",
    carrier: Optional[str] = None
) -> None:
    """Cập nhật trạng thái, mốc thời gian, trạng thái hoàn tất và hãng vận chuyển."""
    query_parts = [
        "last_status = ?",
        "last_timestamp = ?",
        "is_completed = ?",
        "updated_at = CURRENT_TIMESTAMP"
    ]
    params = [status, timestamp, is_completed]

    if estimated_delivery:
        query_parts.append("estimated_delivery = ?")
        params.append(estimated_delivery)

    if carrier:
        query_parts.append("carrier = ?")
        params.append(carrier.strip().upper())

    params.append(tracking_id)
    sql = f"UPDATE trackings SET {', '.join(query_parts)} WHERE id = ?"
    await _execute_sql(sql, tuple(params))


async def mark_tracking_completed(chat_id: int, tracking_number: str) -> bool:
    """Đánh dấu đơn hàng là đã hoàn tất (is_completed = 1)."""
    sql = """
        UPDATE trackings
        SET is_completed = 1,
            updated_at = CURRENT_TIMESTAMP
        WHERE chat_id = ? AND tracking_number = ?
    """
    affected = await _execute_sql(sql, (chat_id, tracking_number))
    return affected > 0
