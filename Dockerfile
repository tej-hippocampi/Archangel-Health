# CareGuide backend + frontend (patient dashboard). Landing is deployed separately.
FROM python:3.12-slim

WORKDIR /app

# System binaries pip cannot install (Audit PRD §C1). tesseract-ocr backs
# pytesseract, the ONLY layer that inspects DICOM pixels for burned-in PHI — without
# it every study routes to manual review instead of silently auto-clearing.
# poppler-utils backs pdf2image (PDF page rendering). Installed BEFORE the pip layer
# so this change does not bust the requirements.txt-keyed pip cache.
RUN apt-get update && apt-get install -y --no-install-recommends \
        tesseract-ocr poppler-utils \
    && rm -rf /var/lib/apt/lists/*

# Install Python deps first so the (slow) pip layer is cached on
# requirements.txt content alone — application source changes won't bust it.
COPY backend/requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir -r /tmp/requirements.txt

# Application source. The backend serves /static from ../frontend, so both
# trees are required at runtime even though only backend/ is the entrypoint.
COPY backend/ backend/
COPY frontend/ frontend/

WORKDIR /app/backend
EXPOSE 8000

# PORT comes from the host (Railway, Render, etc.); default to 8000 locally.
ENV PORT=8000
# --proxy-headers/--forwarded-allow-ips let the app trust the platform's TLS
# terminator (Railway/Render) for X-Forwarded-Proto/For so HTTPS redirect and
# IP-based rate limiting behave correctly behind the proxy (PRD-2).
CMD ["sh", "-c", "python3 -m uvicorn main:app --host 0.0.0.0 --port ${PORT} --proxy-headers --forwarded-allow-ips='*'"]
