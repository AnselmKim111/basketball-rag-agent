# Microsoft 공식 Playwright Python 이미지 - Chromium + 시스템 의존성 미리 포함
FROM mcr.microsoft.com/playwright/python:v1.49.0-jammy

WORKDIR /app

# 한국어 폰트 (matplotlib 차트용 - deepdive 기능)
RUN apt-get update && \
    apt-get install -y --no-install-recommends fonts-noto-cjk && \
    rm -rf /var/lib/apt/lists/*

# Python 의존성 먼저 (이 레이어 캐싱)
COPY requirements.txt .
RUN pip install --no-cache-dir --upgrade pip && \
    pip install --no-cache-dir -r requirements.txt

# 소스 코드 + 편집 가능 프롬프트 + 정적 데이터(산업 카탈로그 등)
COPY src ./src
COPY prompts ./prompts
COPY data ./data

# Playwright 브라우저 (이미지에 이미 있지만 버전 매칭 보장)
RUN playwright install chromium --with-deps

# Orchestrator: 4개 봇(company/industry/market/global) + APScheduler 동시 가동
# 단일 봇만 실행하려면 CMD를 ["python", "-m", "src.bot_worker"] 로 변경
CMD ["python", "-m", "src.orchestrator"]
