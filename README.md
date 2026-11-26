---
title: Space Consultant AI Studio
emoji: 🎬
colorFrom: blue
colorTo: indigo
sdk: docker
app_port: 7860
pinned: false
---

# Space Consultant AI Studio (frontend + backend on one Hugging Face Space)

One Space serves both the website and the API, so no Netlify, no Render and no CORS setup.

## Deploy steps
1. https://huggingface.co/new-space -> Space SDK: **Docker** (Blank), Hardware: **CPU basic (free)**, Visibility: **Public**.
2. Files tab -> **Add file -> Upload files**: upload everything from this zip (Dockerfile, README.md, .dockerignore, `backend` and `frontend` folders), keeping the folder structure.
3. Settings -> **Variables and secrets** -> add two secrets:
   - `SECRET_KEY` = any long random text
   - `DATABASE_URL` = your Neon connection string
   - Optional: `REPLICATE_API_TOKEN`, `REPLICATE_IMAGE_VIDEO_VERSION`
4. Wait for the build (first build takes a few minutes) until the status is **Running**.
5. Open `https://YOUR-USERNAME-YOUR-SPACE.hf.space` -> Create account -> make your video.
   Health check: `.../health` shows `{"status":"ok"}`.

## Notes
- Free CPU basic: 2 vCPU, 16 GB RAM. The Space sleeps after ~48 hours of no use and wakes on the next visit.
- Users and history live in Neon. Uploaded images and videos are temporary (lost on restart/rebuild), so download videos right after they finish.
- Do not put real keys in files; use Secrets only. The Space is public, so the code is visible.
- Changing code: edit/upload the file in the Files tab; the Space rebuilds automatically.

## Optional: deploy on Render instead (same zip)
Push these files to a GitHub repo, then Render -> New -> Blueprint (reads `render.yaml`), or New -> Web Service -> Runtime **Docker**.
Set `DATABASE_URL` (Neon). One service serves the website and the API; no Netlify needed.
Render free is slow for video rendering (very little CPU), so Hugging Face is the better free host.
