# syntax=docker/dockerfile:1
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PORT=8080

WORKDIR /srv

COPY app ./app
COPY tests ./tests
COPY verify ./verify

RUN useradd --create-home --uid 10001 appuser && chown -R appuser:appuser /srv
USER appuser

EXPOSE 8080

HEALTHCHECK --interval=5s --timeout=3s --start-period=5s --retries=12 \
  CMD python -c "import os,urllib.request,sys;sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:%s/health'%os.environ.get('PORT','8080'),timeout=2).status==200 else 1)"

CMD ["python", "-m", "app.main"]
