FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt && useradd --uid 10001 --no-create-home pangolin
RUN mkdir /data && chown 10001:10001 /data && chmod 700 /data
ENV PANGOLIN_DATABASE=/data/server.sqlite3
COPY server/ ./server/
COPY web/ ./web/
USER 10001:10001
EXPOSE 8000
CMD ["python", "-m", "uvicorn", "server.main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1", "--ws-max-size", "131072"]
