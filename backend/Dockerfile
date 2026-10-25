FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PYTHONPATH=/app/src
WORKDIR /app

# 构建期可用 --build-arg PIP_INDEX_URL=... 指定镜像源（如国内环境）
ARG PIP_INDEX_URL=https://pypi.org/simple
COPY requirements.txt alembic.ini ./
RUN pip install --no-cache-dir -i ${PIP_INDEX_URL} -r requirements.txt
COPY backend ./backend
COPY src/deepresearch_agent ./src/deepresearch_agent
COPY skills ./skills

EXPOSE 8000
CMD ["sh", "-c", "alembic upgrade head && uvicorn backend.app.main:app --host 0.0.0.0 --port 8000 --workers 1"]
