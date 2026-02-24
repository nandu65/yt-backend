from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from pydantic import BaseModel
import yt_dlp
import os
import re
import uuid

app = FastAPI()

# -------------------------
# CONFIG
# -------------------------
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DOWNLOAD_DIR = os.path.join(BASE_DIR, "downloads")
COOKIE_PATH = os.path.join(BASE_DIR, "cookies.txt")

os.makedirs(DOWNLOAD_DIR, exist_ok=True)

# -------------------------
# CORS
# -------------------------
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# -------------------------
# MODELS
# -------------------------
class FetchRequest(BaseModel):
    url: str

class DownloadRequest(BaseModel):
    url: str
    format_id: str
    quality_label: str
    format_type: str

# -------------------------
# ROOT (Render health check)
# -------------------------
@app.head("/")
@app.get("/")
def root():
    return {"status": "running"}

# -------------------------
# UTILS
# -------------------------
def sanitize(name):
    return re.sub(r"[^\w\-.]", "_", name)[:80]

def get_ydl_opts(extra=None):
    opts = {
        "quiet": True,
        "no_warnings": True,
        "http_headers": {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        },
    }
    if os.path.exists(COOKIE_PATH):
        opts["cookiefile"] = COOKIE_PATH
    if extra:
        opts.update(extra)
    return opts

# =========================
# FETCH VIDEO INFO
# =========================
@app.post("/api/fetch")
def fetch_video(req: FetchRequest):
    ydl_opts = get_ydl_opts({"skip_download": True})

    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(req.url, download=False)
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))

    qualities = []
    seen = set()

    for f in info.get("formats", []):
        if f.get("vcodec") != "none" and f.get("height"):
            label = f"{f['height']}p"
            if label not in seen:
                qualities.append({
                    "label": label,
                    "format_id": f["format_id"],
                    "type": "video",
                    "height": f["height"],
                })
                seen.add(label)

    audio = [
        f for f in info.get("formats", [])
        if f.get("acodec") != "none" and f.get("vcodec") == "none"
    ]
    if audio:
        qualities.append({
            "label": "Audio Only",
            "format_id": audio[-1]["format_id"],
            "type": "audio",
            "height": 0,
        })

    qualities.sort(key=lambda x: x.get("height", 0), reverse=True)

    return {
        "title": info.get("title", "Unknown"),
        "thumbnail": info.get("thumbnail", ""),
        "duration": info.get("duration"),
        "uploader": info.get("uploader"),
        "view_count": info.get("view_count"),
        "qualities": qualities,
    }

# =========================
# DOWNLOAD VIDEO
# =========================
@app.post("/api/download")
def download_video(req: DownloadRequest):
    ext = "mp3" if req.format_type == "audio" else "mp4"
    uid = str(uuid.uuid4())[:8]
    filename = f"{uid}.{ext}"
    filepath = os.path.join(DOWNLOAD_DIR, filename)

    fmt = f"{req.format_id}+bestaudio/best" if req.format_type == "video" else req.format_id

    ydl_opts = get_ydl_opts({
        "format": fmt,
        "outtmpl": filepath,
        "merge_output_format": ext,
    })

    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(req.url, download=True)
            title = sanitize(info.get("title", "video"))
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

    # yt-dlp may rename extension
    if not os.path.exists(filepath):
        for f in os.listdir(DOWNLOAD_DIR):
            if f.startswith(uid):
                filename = f
                filepath = os.path.join(DOWNLOAD_DIR, f)
                break

    if not os.path.exists(filepath):
        raise HTTPException(status_code=500, detail="Download failed — file not found after processing.")

    safe_name = f"{title}_{req.quality_label}.{ext}"

    return {
        "download_url": f"/downloads/{filename}",
        "filename": safe_name,
    }

# =========================
# SERVE FILE
# =========================
@app.get("/downloads/{filename}")
def serve_file(filename: str):
    filepath = os.path.join(DOWNLOAD_DIR, filename)
    if not os.path.exists(filepath):
        raise HTTPException(status_code=404, detail="File not found")
    return FileResponse(filepath, filename=filename)

# =========================
# START SERVER
# =========================
if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("PORT", 10000))
    uvicorn.run(app, host="0.0.0.0", port=port)
