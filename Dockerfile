FROM python:3.11-slim

WORKDIR /app

# Thiết lập biến môi trường tối ưu cho Python trong container
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PORT=8000

# Cài đặt thư viện hệ thống tối thiểu: ca-certificates và Tesseract-OCR (chỉ gói tiếng Anh)
# --no-install-recommends giúp giữ kích thước image nhỏ nhất có thể cho Render Free
RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates \
    tesseract-ocr \
    tesseract-ocr-eng \
    && rm -rf /var/lib/apt/lists/*

# Cài đặt Python dependencies trước để tận dụng Docker layer cache
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy toàn bộ mã nguồn
COPY . .

# Port mặc định cho Health Check server trên Render
EXPOSE 8000

CMD ["python", "bot.py"]
