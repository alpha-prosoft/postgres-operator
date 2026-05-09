FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

COPY requirements.txt .
RUN pip install -r requirements.txt

COPY pg_operator/ ./pg_operator/

RUN groupadd -g 10001 pgop && useradd -u 10001 -g 10001 -M -d /app pgop
USER 10001

ENTRYPOINT ["kopf", "run", "--standalone", "--all-namespaces", "-m", "pg_operator.main"]
