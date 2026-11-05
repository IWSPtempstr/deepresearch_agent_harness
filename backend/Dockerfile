FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PYTHONPATH=/app/src
WORKDIR /app

# 构建期可用 --build-arg PIP_INDEX_URL=... 指定镜像源（如国内环境）
ARG PIP_INDEX_URL=https://pypi.org/simple
COPY requirements.txt alembic.ini ./
RUN pip install --no-cache-dir -i ${PIP_INDEX_URL} -r requirements.txt
COPY backend ./backend
COPY src/deepresearch_agent ./src/deepresearch_agent
COPY scripts ./scripts
COPY skills ./skills

EXPOSE 8000
# T-005：迁移作为 CMD 内前置步骤（migrate.py 带锁竞争重试），uvicorn 启动不执行 DDL
CMD ["sh", "-c", "python scripts/migrate.py && uvicorn backend.app.main:app --host 0.0.0.0 --port 8000 --workers 1"]
