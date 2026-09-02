# One image, two roles. The API and the worker share every client, every model
# of a job and every validator, so they cannot drift apart; ROLE picks the
# entrypoint. It also halves build time, registry storage and cold-start pulls.
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /srv

RUN adduser --disabled-password --gecos "" --uid 10001 app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app
COPY web ./web
COPY entrypoint.sh .
RUN chmod +x entrypoint.sh && chown -R app:app /srv

USER app
EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s \
  CMD python -c "import os,urllib.request,sys; sys.exit(0) if os.environ.get('ROLE')=='worker' else urllib.request.urlopen('http://127.0.0.1:8000/healthz')" || exit 1

ENTRYPOINT ["./entrypoint.sh"]
