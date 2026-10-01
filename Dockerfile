# syntax=docker/dockerfile:1
# TSF Sizer: upload a PAN-OS Tech Support File, get a replacement sizing report.
#
# Behind a TLS-inspecting proxy, pass its CA so pip can reach PyPI (not stored in the image):
#   docker build --secret id=ca,src=/path/to/corporate-ca.pem -t tsf-sizer .
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    TSF_SIZER_DB=/data/app.db

RUN useradd --uid 10001 --create-home app \
    && mkdir /data && chown app:app /data

WORKDIR /app
COPY backend/pyproject.toml backend/README.md ./
COPY backend/tsf_sizer ./tsf_sizer
RUN --mount=type=secret,id=ca,required=false \
    if [ -s /run/secrets/ca ]; then export PIP_CERT=/run/secrets/ca; fi; \
    pip install . && rm -rf /root/.cache build

USER app
VOLUME /data
EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8080/healthz', timeout=4)"

CMD ["uvicorn", "--factory", "tsf_sizer.web.app:create_app", "--host", "0.0.0.0", "--port", "8080", "--proxy-headers"]
