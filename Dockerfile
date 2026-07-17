# syntax=docker/dockerfile:1
#
# pwflow HTTP service — declarative Playwright automation.
#   docker build -t pwflow .                          # includes CloakBrowser (provider: cloak)
#   docker build -t pwflow --build-arg WITH_CLOAK=false .   # lean, playwright-only image
#   docker run -p 8000:8000 -v "$PWD/flows:/app/flows:ro" pwflow
#
# For the Pro stealth binary, bake a license at build time:
#   docker build -t pwflow --build-arg CLOAKBROWSER_LICENSE_KEY=cb_xxx .

FROM python:3.12-slim-bookworm

# uv — fast, reproducible installs. Pin tracks the uv you develop with; bump freely.
COPY --from=ghcr.io/astral-sh/uv:0.9.30 /uv /uvx /bin/

# Bundle the CloakBrowser stealth binary into the image (provider: cloak). Turn off for a
# smaller, playwright-only image and use a cloakserve sidecar over CDP instead.
ARG WITH_CLOAK=true

ENV UV_LINK_MODE=copy \
    UV_COMPILE_BYTECODE=1 \
    UV_PYTHON_DOWNLOADS=never \
    PLAYWRIGHT_BROWSERS_PATH=/opt/pw-browsers \
    CLOAKBROWSER_CACHE_DIR=/opt/cloakbrowser \
    # Chromium's sandbox needs privileges most container runtimes withhold, so without
    # these it fails to launch. pwflow feeds this env to both providers on launch.
    PWFLOW_BROWSER_ARGS="--no-sandbox --disable-dev-shm-usage"

WORKDIR /app

# 1) Dependencies only — this layer is cached until pyproject.toml / uv.lock change.
COPY pyproject.toml uv.lock README.md ./
RUN if [ "$WITH_CLOAK" = "true" ]; then EXTRA="--extra cloak"; fi; \
    uv sync --frozen --no-dev --no-install-project $EXTRA

# 2) Chromium + every system library it needs (installed via apt by Playwright).
#    Call the venv's playwright binary directly — `uv run` here would try to sync the
#    project (whose src/ is not copied yet) and fail.
RUN /app/.venv/bin/playwright install --with-deps chromium

# 3) Pre-download the CloakBrowser stealth binary (~200MB) so the first run needs no
#    network. `install` only downloads — it does not launch — so no sandbox is needed here.
ARG CLOAKBROWSER_LICENSE_KEY=""
RUN if [ "$WITH_CLOAK" = "true" ]; then \
      CLOAKBROWSER_LICENSE_KEY="$CLOAKBROWSER_LICENSE_KEY" \
      /app/.venv/bin/python -m cloakbrowser install; \
    fi

# 4) The application itself.
COPY src ./src
RUN if [ "$WITH_CLOAK" = "true" ]; then EXTRA="--extra cloak"; fi; \
    uv sync --frozen --no-dev $EXTRA
COPY flows ./flows

# Run as a non-root user that owns the dirs and binary caches it reads/writes.
RUN useradd --create-home --uid 10001 pwflow \
 && mkdir -p /app/.pwflow /app/out /app/artifacts /opt/cloakbrowser \
 && chown -R pwflow:pwflow /app /opt/pw-browsers /opt/cloakbrowser
USER pwflow

EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=25s --retries=3 \
    CMD /app/.venv/bin/python -c \
        "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/healthz')" || exit 1

# The venv binary directly — no `uv run` overhead on every start.
CMD ["/app/.venv/bin/pwflow", "serve", \
     "--host", "0.0.0.0", "--port", "8000", \
     "--flows-dir", "flows", "--state-dir", "/app/.pwflow"]
