# ── Stage 1: LibreDWG (dwg2dxf / dxf2dwg) ────────────────────────────────────
# Gives the headless ezdxf engine real DWG read and write (R2000/R2004 output)
# without the licensed ODA File Converter. Built from the release tarball.
FROM python:3.11-slim AS libredwg

# 0.14 or newer: 0.13.3 writes DWGs that lose 20 of 21 dimensions (R2000 DXF
# in) or come out with one-letter names and an empty model space (R2010 in).
ARG LIBREDWG_VERSION=0.14

RUN apt-get update \
    && apt-get install -y --no-install-recommends build-essential curl ca-certificates xz-utils \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /build
RUN curl -fsSL "https://github.com/LibreDWG/libredwg/releases/download/${LIBREDWG_VERSION}/libredwg-${LIBREDWG_VERSION}.tar.xz" \
        | tar -xJ --strip-components=1 \
    && ./configure --prefix=/opt/libredwg --disable-bindings --disable-static \
    && make -j"$(nproc)" \
    && make install-strip

# ── Stage 2: the server ──────────────────────────────────────────────────────
FROM python:3.11-slim

WORKDIR /app

RUN useradd --create-home --shell /bin/bash mcp

# Real TrueType fonts for PDF/PNG text. The slim image has none, and ezdxf then
# draws every character as an empty box. DejaVu covers Vietnamese and French;
# Liberation Sans stands in for the Arial that new drawings' Standard style names.
RUN apt-get update \
    && apt-get install -y --no-install-recommends fonts-dejavu-core fonts-liberation \
    && rm -rf /var/lib/apt/lists/*

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

# Build ezdxf's font cache now, as the user that renders, so the first export
# does not pay for the scan and never caches an empty font list.
RUN python -c "from ezdxf.fonts import fonts; fonts.build_font_manager_cache(fonts._get_font_manager_path())"

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=10s --start-period=5s --retries=3 \
    CMD python -c "import server, engineering.gear; print('ok')" || exit 1

CMD ["python", "server.py"]
