FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 AGENTRY_DATA=/data
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY agentry ./agentry
RUN useradd --system --uid 10001 agentry && install -d -o agentry -g agentry /data
USER agentry
VOLUME /data
EXPOSE 8090
HEALTHCHECK --interval=30s --timeout=5s CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8090/healthz')"
CMD ["python", "-m", "agentry"]
