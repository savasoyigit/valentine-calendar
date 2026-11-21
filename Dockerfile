ARG PYTHON_IMAGE=python:3.12-slim
FROM ${PYTHON_IMAGE}

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    DATA_DIR=/data

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app.py core.py config_store.py setup_wizard.py settings_api.py places_api.py \
     space_api.py capsules_api.py security.py validation.py \
     create_login_credentials.py index.html setup.html ./
COPY icons ./icons

RUN useradd --system --uid 10001 --home /app askimiz \
    && mkdir -p /data && chown askimiz /data
USER askimiz
VOLUME ["/data"]
EXPOSE 8000

HEALTHCHECK --interval=60s --timeout=5s --start-period=10s \
  CMD python -c "import urllib.request,sys; urllib.request.urlopen('http://127.0.0.1:8000/manifest.json', timeout=4)" || exit 1

CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8000", "--proxy-headers"]
