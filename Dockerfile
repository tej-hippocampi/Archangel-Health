# CareGuide backend + frontend (patient dashboard). Landing is deployed separately.
FROM python:3.12-slim

WORKDIR /app

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
CMD ["sh", "-c", "python3 -m uvicorn main:app --host 0.0.0.0 --port ${PORT}"]
