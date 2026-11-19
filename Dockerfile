FROM python:3.11-slim
WORKDIR /app
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app/src \
    KARRY_WORKSPACE=/tmp/karry-agent
RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg espeak-ng \
    && rm -rf /var/lib/apt/lists/*
COPY requirements.txt pyproject.toml README.md ./
RUN pip install --no-cache-dir --upgrade pip \
    && pip install --no-cache-dir -r requirements.txt
COPY . .
RUN pip install --no-cache-dir . \
    && mkdir -p /tmp/karry-agent
EXPOSE 8000
CMD ["sh", "-c", "python -m karry_agent.location_history --migrate && exec uvicorn app:app --host 0.0.0.0 --port ${PORT:-8000}"]
