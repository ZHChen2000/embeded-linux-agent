# Bookworm slim base; no apt-get in image build.
# pip: progress bar off, no build isolation (single-vCPU Docker thread limit).
FROM python:3.11-slim-bookworm

WORKDIR /app

ENV PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_PREFER_BINARY=1 \
    PIP_PROGRESS_BAR=off \
    PYTHONUNBUFFERED=1

ARG PIP_INDEX_URL=https://pypi.tuna.tsinghua.edu.cn/simple

RUN pip install --progress-bar off -i "${PIP_INDEX_URL}" \
    --upgrade pip "setuptools>=68" wheel

COPY elda/pyproject.toml /tmp/elda/pyproject.toml
COPY elda/elda /tmp/elda/elda
RUN pip install --progress-bar off --prefer-binary --no-build-isolation \
    -i "${PIP_INDEX_URL}" /tmp/elda

COPY elda-api/pyproject.toml ./
COPY elda-api/app ./app
COPY elda-api/alembic.ini ./
COPY elda-api/alembic ./alembic
COPY schemas /app/schemas

COPY elda-api/docker-entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

RUN pip install --progress-bar off --prefer-binary --no-build-isolation \
    -i "${PIP_INDEX_URL}" .

EXPOSE 8000
ENTRYPOINT ["/entrypoint.sh"]
