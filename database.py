import os
import aiosqlite

DB_PATH = os.getenv("DATABASE_PATH", "trackings.db")

async def init_db():
    """Khởi tạo cấu trúc bảng SQLite nếu chưa tồn tại."""
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
            CREATE TABLE IF NOT EXISTS trackings (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                chat_id INTEGER NOT NULL,
                tracking_number TEXT NOT NULL,
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
        # Tự động thêm cột estimated_delivery nếu database cũ chưa có
        try:
            await db.execute("ALTER TABLE trackings ADD COLUMN estimated_delivery TEXT DEFAULT ''")
        except Exception:
            pass
        await db.commit()

async def add_or_update_tracking(chat_id: int, tracking_number: str, label: str, status: str, timestamp: int, estimated_delivery: str = ""):
    """Lưu hoặc cập nhật mã vận đơn cần theo dõi theo chat_id."""
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
            INSERT INTO trackings (chat_id, tracking_number, label, last_status, last_timestamp, is_completed, estimated_delivery, updated_at)
            VALUES (?, ?, ?, ?, ?, 0, ?, CURRENT_TIMESTAMP)
            ON CONFLICT(chat_id, tracking_number) DO UPDATE SET
                label = excluded.label,
                last_status = excluded.last_status,
                last_timestamp = excluded.last_timestamp,
                is_completed = 0,
                estimated_delivery = excluded.estimated_delivery,
                updated_at = CURRENT_TIMESTAMP
        """, (chat_id, tracking_number, label, status, timestamp, estimated_delivery))
        await db.commit()

async def get_active_trackings_by_chat(chat_id: int):
    """Lấy danh sách các đơn đang hoạt động của người dùng."""
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("""
            SELECT tracking_number, label, last_status, last_timestamp, estimated_delivery, created_at
            FROM trackings
            WHERE chat_id = ? AND is_completed = 0
            ORDER BY updated_at DESC
        """, (chat_id,)) as cursor:
            return await cursor.fetchall()

async def remove_tracking(chat_id: int, tracking_number: str) -> bool:
    """Xóa bỏ một đơn hàng khỏi danh sách theo dõi."""
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute("""
            DELETE FROM trackings 
            WHERE chat_id = ? AND tracking_number = ?
        """, (chat_id, tracking_number))
        await db.commit()
        return cursor.rowcount > 0

async def get_all_active_trackings():
    """Lấy toàn bộ các đơn đang chờ giao trên toàn bộ hệ thống để bot polling."""
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("""
            SELECT id, chat_id, tracking_number, label, last_status, last_timestamp, estimated_delivery
            FROM trackings
            WHERE is_completed = 0
        """) as cursor:
            return await cursor.fetchall()

async def update_tracking_status(tracking_id: int, status: str, timestamp: int, is_completed: int = 0, estimated_delivery: str = ""):
    """Cập nhật mốc thời gian, trạng thái, cờ hoàn tất và ngày dự kiến."""
    async with aiosqlite.connect(DB_PATH) as db:
        if estimated_delivery:
            await db.execute("""
                UPDATE trackings
                SET last_status = ?,
                    last_timestamp = ?,
                    is_completed = ?,
                    estimated_delivery = ?,
                    updated_at = CURRENT_TIMESTAMP
                WHERE id = ?
            """, (status, timestamp, is_completed, estimated_delivery, tracking_id))
        else:
            await db.execute("""
                UPDATE trackings
                SET last_status = ?,
                    last_timestamp = ?,
                    is_completed = ?,
                    updated_at = CURRENT_TIMESTAMP
                WHERE id = ?
            """, (status, timestamp, is_completed, tracking_id))
        await db.commit()
