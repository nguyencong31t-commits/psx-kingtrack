FROM python:3.11-slim

WORKDIR /app

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PORT=8000

# Cài đặt thư viện hệ thống cần thiết cho Tesseract OCR và OpenCV (EasyOCR)
RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates \
    tesseract-ocr \
    tesseract-ocr-eng \
    libgl1 \
    libglib2.0-0 \
    && rm -rf /var/lib/apt/lists/*

# Cài đặt PyTorch bản CPU-only siêu nhẹ (~150MB thay vì 850MB CUDA mặc định)
RUN pip install --no-cache-dir torch torchvision --index-url https://download.pytorch.org/whl/cpu

# Cài đặt Python dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Tải sẵn model EasyOCR ngay khi build image để lúc chạy không phải tải từ mạng (tránh lag/timeout)
RUN python -c "import easyocr; easyocr.Reader(['en'], gpu=False)"

# Copy toàn bộ mã nguồn
COPY . .

EXPOSE 8000

CMD ["python", "bot.py"]
