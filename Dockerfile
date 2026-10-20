FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    HOST=0.0.0.0 \
    PORT=8010 \
    MRC_DATA_DIR=/data

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt \
    && groupadd --gid 10001 mrc \
    && useradd --uid 10001 --gid mrc --no-create-home mrc \
    && mkdir /data \
    && chown mrc:mrc /data \
    && chmod 700 /data

COPY app.py media.py ./
COPY static/ static/
COPY templates/ templates/

USER mrc
EXPOSE 8010
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import os, urllib.request; urllib.request.urlopen('http://127.0.0.1:' + os.environ.get('PORT', '8010') + '/health', timeout=3)"
CMD ["python", "app.py"]
