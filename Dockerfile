# Microsoft 공식 Playwright Python 이미지 - Chromium + 시스템 의존성 미리 포함
FROM mcr.microsoft.com/playwright/python:v1.49.0-jammy

WORKDIR /app

# Python 의존성 먼저 (이 레이어 캐싱)
COPY requirements.txt .
RUN pip install --no-cache-dir --upgrade pip && \
    pip install --no-cache-dir -r requirements.txt

# 소스 코드
COPY src ./src

# Playwright 브라우저 (이미지에 이미 있지만 버전 매칭 보장)
RUN playwright install chromium --with-deps

# 봇 워커 실행
CMD ["python", "-m", "src.bot_worker"]
