FROM python:3.12-slim

WORKDIR /app

# postgresql-client-16 ships the pg_dump/psql that backup_task.py shells
# out to. Pinned to the server's major version and taken from PGDG because
# trixie's default is 17, whose custom-format archives (header 1.16) are
# rejected by the pg_restore 16 that restore.sh and validate_backup.sh run
# inside the postgres:16 container — a 17 client yields backups that look
# healthy and cannot be restored. Bump only with the postgres image.
# The PGDG list is removed again in the same layer so the later
# `playwright install --with-deps` apt run resolves against Debian alone.
RUN apt-get update && apt-get install -y --no-install-recommends \
      build-essential libpq-dev curl ca-certificates gnupg && \
    install -d /usr/share/postgresql-common/pgdg && \
    curl -sSf -o /usr/share/postgresql-common/pgdg/apt.postgresql.org.asc \
      https://www.postgresql.org/media/keys/ACCC4CF8.asc && \
    . /etc/os-release && \
    echo "deb [signed-by=/usr/share/postgresql-common/pgdg/apt.postgresql.org.asc] https://apt.postgresql.org/pub/repos/apt ${VERSION_CODENAME}-pgdg main" \
      > /etc/apt/sources.list.d/pgdg.list && \
    apt-get update && apt-get install -y --no-install-recommends \
      postgresql-client-16 && \
    rm -f /etc/apt/sources.list.d/pgdg.list && \
    rm -rf /var/lib/apt/lists/*

# Install Python deps via pip (extract deps from pyproject.toml)
COPY pyproject.toml .
RUN pip install --no-cache-dir \
    "fastapi[standard]>=0.115" \
    "uvicorn[standard]>=0.32" \
    "sqlalchemy[asyncio]>=2.0" \
    "asyncpg>=0.30" \
    "alembic>=1.14" \
    "pydantic-settings>=2.6" \
    "authlib>=1.4" \
    "httpx>=0.28" \
    "python-jose[cryptography]>=3.3" \
    "celery[redis]>=5.4" \
    "redis>=5.2" \
    "pyyaml>=6.0" \
    "openpyxl>=3.1" \
    "python-multipart>=0.0.12" \
    "itsdangerous>=2.1" \
    "psycopg2-binary>=2.9" \
    "pdfplumber>=0.11" \
    "python-docx>=1.1" \
    "anthropic>=0.40" \
    "beautifulsoup4>=4.12" \
    "dnspython>=2.6" \
    "bcrypt>=4.2" \
    "playwright>=1.45" \
    "playwright-stealth>=2.0"

# F254: install Chromium + the system libs it needs for headless rendering.
# Used by ``app.services.playwright_browser`` for two surfaces:
#   - scraping ATS sites that 403 plain httpx (Wellfound's DataDome wall)
#   - filling + submitting application forms in the v6 Apply Routine
# We pin to Chromium only (not Firefox or WebKit) to keep the image small
# — adds ~280 MB to the layer; full multi-browser would be ~900 MB.
# ``--with-deps`` pulls the apt packages Chromium needs (libnss3, libxkbcommon,
# fonts, etc.); separating it from the Python install keeps Docker's layer
# cache hits useful when only pyproject.toml changes.
RUN playwright install --with-deps chromium

# Copy app
COPY alembic.ini .
COPY alembic/ alembic/
COPY app/ app/

EXPOSE 8000

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
