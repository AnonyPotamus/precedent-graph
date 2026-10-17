FROM python:3.13-slim
WORKDIR /srv
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY app ./app
COPY pipeline ./pipeline
COPY data/precedents_full.json.xz ./data/
ENV PORT=8000 PYTHONUNBUFFERED=1 GRAPH_DUMP=/srv/data/precedents_full.json.xz
EXPOSE 8000
# one worker: the graph bootstrap and the MCP session manager run once per process
CMD ["sh", "-c", "uvicorn app.server:app --host 0.0.0.0 --port ${PORT} --proxy-headers --forwarded-allow-ips='*'"]
