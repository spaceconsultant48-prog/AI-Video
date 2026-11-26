# ---- 1) build the React frontend ----
FROM node:20-slim AS web
WORKDIR /web
COPY frontend/package.json frontend/package-lock.json ./
RUN npm install --no-audit --no-fund
COPY frontend/ ./
# Empty = the website calls the API on the same address (no CORS, no second host)
ENV VITE_API_URL=""
RUN npm run build

# ---- 2) backend + FFmpeg, serving the built frontend ----
FROM python:3.12-slim
RUN apt-get update \
 && apt-get install -y --no-install-recommends ffmpeg \
 && rm -rf /var/lib/apt/lists/*

# Hugging Face Spaces runs the container as user 1000
RUN useradd -m -u 1000 user
USER user
ENV HOME=/home/user PATH=/home/user/.local/bin:$PATH
WORKDIR /home/user/app

COPY --chown=user backend/requirements.txt .
RUN pip install --no-cache-dir --upgrade -r requirements.txt

COPY --chown=user backend/app ./app
COPY --chown=user --from=web /web/dist ./static
RUN mkdir -p uploads generated
ENV UPLOAD_DIR=uploads OUTPUT_DIR=generated

# Hugging Face uses 7860 (no PORT set); Render sets PORT itself.
EXPOSE 7860
CMD ["sh", "-c", "uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-7860}"]
