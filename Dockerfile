FROM python:3.11-slim

WORKDIR /app

# Cài đặt thư viện hệ thống cần thiết cho curl_cffi và mạng
RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Cổng mặc định cho Health Check
ENV PORT=8000
EXPOSE 8000

CMD ["python", "bot.py"]
