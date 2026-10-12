# ── Stage 1: LibreDWG (dwg2dxf / dxf2dwg) ────────────────────────────────────
# Gives the headless ezdxf engine real DWG read and write (R2000/R2004 output)
# without the licensed ODA File Converter. Built from the GNU release tarball.
FROM python:3.11-slim AS libredwg

ARG LIBREDWG_VERSION=0.13.3

RUN apt-get update \
    && apt-get install -y --no-install-recommends build-essential curl ca-certificates xz-utils \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /build
RUN curl -fsSL "https://ftp.gnu.org/gnu/libredwg/libredwg-${LIBREDWG_VERSION}.tar.xz" \
        | tar -xJ --strip-components=1 \
    && ./configure --prefix=/opt/libredwg --disable-bindings --disable-static \
    && make -j"$(nproc)" \
    && make install-strip

# ── Stage 2: the server ──────────────────────────────────────────────────────
FROM python:3.11-slim

WORKDIR /app

RUN useradd --create-home --shell /bin/bash mcp

COPY --from=libredwg /opt/libredwg /opt/libredwg
ENV LIBREDWG_BIN=/opt/libredwg/bin \
    LD_LIBRARY_PATH=/opt/libredwg/lib \
    ALLOWED_PATHS=/data/store

COPY pyproject.toml README.md LICENSE ./
COPY server.py config.py security.py version.py ./
COPY backends/ ./backends/
COPY engineering/ ./engineering/
COPY discovery/ ./discovery/
COPY templates/ ./templates/

# [pdf] brings matplotlib + Pillow: PDF export, PNG screenshots, viewport
# render. [office] brings openpyxl for XLSX takeoffs. Without them the headless
# engine refuses all of those.
RUN pip install --no-cache-dir -e ".[pdf,office]"

RUN mkdir -p /data/store && chown mcp:mcp /data/store
VOLUME /data/store

USER mcp

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=10s --start-period=5s --retries=3 \
    CMD python -c "import server, engineering.gear; print('ok')" || exit 1

CMD ["python", "server.py"]
