# Image for the deployed dispatcher.
#
# Kept small on purpose: the ECR free allowance is 500 MB of private storage per
# month, and a careless Python image eats that in two pushes. Two things do most
# of the work — a slim base, and psycopg's binary wheel so there is no compiler
# or libpq-dev in the final layer.

FROM python:3.12-slim AS base

# Don't write .pyc files into the image, and don't buffer stdout — an unflushed
# buffer is why a container that is clearly working appears to log nothing.
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

# Dependencies first, so editing source doesn't invalidate the layer that took
# the longest to build.
COPY pyproject.toml README.md ./
COPY src/ ./src/

RUN pip install --no-cache-dir ".[postgres]"

COPY deploy/dispatcher_main.py ./deploy/

# Run as a non-root user. Nothing here needs root, and a container that does not
# need it should not have it.
RUN useradd --create-home --shell /usr/sbin/nologin hapax \
    && chown -R hapax:hapax /app
USER hapax

# No EXPOSE and no port: the dispatcher makes outbound connections to Postgres
# and listens for nothing. The MCP server is stdio and is spawned per session by
# an agent host, not run as a service — see deploy/dispatcher_main.py.

# A dead database connection should surface as an unhealthy container rather
# than a silently idle one.
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import os, psycopg; psycopg.connect(os.environ['HAPAX_DATABASE_URL'], connect_timeout=4).close()" || exit 1

ENTRYPOINT ["python", "deploy/dispatcher_main.py"]
