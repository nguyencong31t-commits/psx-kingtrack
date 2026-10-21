import os
import aiosqlite
from typing import List, Dict, Optional, Any

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
_env_db = os.getenv("DATABASE_PATH", "trackings.db")
if not os.path.isabs(_env_db):
    DB_PATH = os.path.join(BASE_DIR, _env_db)
else:
    DB_PATH = _env_db


async def init_db():
    """
    Khởi tạo cấu trúc bảng SQLite nếu chưa tồn tại và thực hiện
    tự động migration cho các cột mới (carrier, estimated_delivery).
    """
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
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
        """)

        # Migration: Kiểm tra và thêm cột nếu database cũ chưa có
        try:
            async with db.execute("PRAGMA table_info(trackings)") as cursor:
                columns = [row[1] for row in await cursor.fetchall()]

            if "carrier" not in columns:
                await db.execute("ALTER TABLE trackings ADD COLUMN carrier TEXT DEFAULT 'SPX'")

            if "estimated_delivery" not in columns:
                await db.execute("ALTER TABLE trackings ADD COLUMN estimated_delivery TEXT DEFAULT ''")
        except Exception:
            # Fallback an toàn cho từng cột
            try:
                await db.execute("ALTER TABLE trackings ADD COLUMN carrier TEXT DEFAULT 'SPX'")
            except Exception:
                pass
            try:
                await db.execute("ALTER TABLE trackings ADD COLUMN estimated_delivery TEXT DEFAULT ''")
            except Exception:
                pass

        # Đảm bảo tất cả các bản ghi cũ đều có carrier mặc định là 'SPX' nếu đang NULL hoặc rỗng
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
    """Lưu hoặc cập nhật mã vận đơn cần theo dõi theo chat_id kèm theo hãng vận chuyển (carrier)."""
    carrier_clean = (carrier or "SPX").strip().upper()
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
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
        """, (chat_id, tracking_number, carrier_clean, label, status, timestamp, estimated_delivery))
        await db.commit()


async def get_active_trackings_by_chat(chat_id: int) -> List[Dict[str, Any]]:
    """Lấy danh sách các đơn đang hoạt động của người dùng (trả về list dict có trường carrier)."""
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("""
            SELECT id, chat_id, tracking_number, COALESCE(carrier, 'SPX') AS carrier,
                   label, last_status, last_timestamp, is_completed, estimated_delivery,
                   created_at, updated_at
            FROM trackings
            WHERE chat_id = ? AND is_completed = 0
            ORDER BY updated_at DESC
        """, (chat_id,)) as cursor:
            rows = await cursor.fetchall()
            return [dict(row) for row in rows]


async def remove_tracking(chat_id: int, tracking_number: str) -> bool:
    """Xóa bỏ một đơn hàng khỏi danh sách theo dõi."""
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute("""
            DELETE FROM trackings 
            WHERE chat_id = ? AND tracking_number = ?
        """, (chat_id, tracking_number))
        await db.commit()
        return cursor.rowcount > 0


async def get_all_active_trackings() -> List[Dict[str, Any]]:
    """Lấy toàn bộ các đơn đang chờ giao trên toàn bộ hệ thống để bot polling (có trường carrier)."""
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("""
            SELECT id, chat_id, tracking_number, COALESCE(carrier, 'SPX') AS carrier,
                   label, last_status, last_timestamp, is_completed, estimated_delivery,
                   created_at, updated_at
            FROM trackings
            WHERE is_completed = 0
            ORDER BY updated_at DESC
        """) as cursor:
            rows = await cursor.fetchall()
            return [dict(row) for row in rows]


async def get_tracking(chat_id: int, tracking_number: str) -> Optional[Dict[str, Any]]:
    """Lấy thông tin chi tiết một đơn hàng theo chat_id và mã vận đơn."""
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("""
            SELECT id, chat_id, tracking_number, COALESCE(carrier, 'SPX') AS carrier,
                   label, last_status, last_timestamp, is_completed, estimated_delivery,
                   created_at, updated_at
            FROM trackings
            WHERE chat_id = ? AND tracking_number = ?
        """, (chat_id, tracking_number)) as cursor:
            row = await cursor.fetchone()
            return dict(row) if row else None


async def get_untracked_by_chat(chat_id: int, limit: int = 20) -> List[Dict[str, Any]]:
    """Lấy danh sách các đơn đã hoàn tất hoặc ngừng theo dõi gần đây của người dùng."""
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("""
            SELECT id, chat_id, tracking_number, COALESCE(carrier, 'SPX') AS carrier,
                   label, last_status, last_timestamp, is_completed, estimated_delivery,
                   created_at, updated_at
            FROM trackings
            WHERE chat_id = ? AND is_completed = 1
            ORDER BY updated_at DESC
            LIMIT ?
        """, (chat_id, limit)) as cursor:
            rows = await cursor.fetchall()
            return [dict(row) for row in rows]


async def get_all_trackings_by_chat(chat_id: int) -> List[Dict[str, Any]]:
    """Lấy toàn bộ các đơn hàng (cả đang theo dõi lẫn đã hoàn tất) của người dùng."""
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("""
            SELECT id, chat_id, tracking_number, COALESCE(carrier, 'SPX') AS carrier,
                   label, last_status, last_timestamp, is_completed, estimated_delivery,
                   created_at, updated_at
            FROM trackings
            WHERE chat_id = ?
            ORDER BY updated_at DESC
        """, (chat_id,)) as cursor:
            rows = await cursor.fetchall()
            return [dict(row) for row in rows]


async def get_active_trackings_by_carrier(carrier: str) -> List[Dict[str, Any]]:
    """Lấy danh sách các đơn đang hoạt động theo hãng vận chuyển cụ thể (SPX, GHN, GHTK...)."""
    carrier_clean = carrier.strip().upper()
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("""
            SELECT id, chat_id, tracking_number, COALESCE(carrier, 'SPX') AS carrier,
                   label, last_status, last_timestamp, is_completed, estimated_delivery,
                   created_at, updated_at
            FROM trackings
            WHERE UPPER(carrier) = ? AND is_completed = 0
            ORDER BY updated_at DESC
        """, (carrier_clean,)) as cursor:
            rows = await cursor.fetchall()
            return [dict(row) for row in rows]


async def update_tracking_status(
    tracking_id: int,
    status: str,
    timestamp: int,
    is_completed: int = 0,
    estimated_delivery: str = "",
    carrier: Optional[str] = None
) -> None:
    """Cập nhật mốc thời gian, trạng thái, cờ hoàn tất, ngày dự kiến và hãng vận chuyển (nếu có)."""
    async with aiosqlite.connect(DB_PATH) as db:
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
        await db.execute(sql, tuple(params))
        await db.commit()


async def mark_tracking_completed(chat_id: int, tracking_number: str) -> bool:
    """Đánh dấu một đơn hàng là đã hoàn tất (is_completed = 1) thay vì xóa hẳn khỏi CSDL."""
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute("""
            UPDATE trackings
            SET is_completed = 1,
                updated_at = CURRENT_TIMESTAMP
            WHERE chat_id = ? AND tracking_number = ?
        """, (chat_id, tracking_number))
        await db.commit()
        return cursor.rowcount > 0
