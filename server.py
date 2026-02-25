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
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/120.0.0.0 Safari/537.36"
            )
        },
    }
    if os.path.exists(COOKIE_PATH):
        opts["cookiefile"] = COOKIE_PATH
    if extra:
        opts.update(extra)
    return opts


def score_format(f):
    """Higher score = preferred format for a given resolution."""
    score = 0
    ext = (f.get("ext") or "").lower()
    protocol = (f.get("protocol") or "").lower()
    if ext == "mp4":
        score += 10
    if "http" in protocol:
        score += 5
    score += float(f.get("tbr") or f.get("vbr") or 0)
    return score


# =========================
# FETCH VIDEO INFO
# =========================
@app.post("/api/fetch")
def fetch_video(req: FetchRequest):
    ydl_opts = get_ydl_opts({
        "skip_download": True,
        "ignore_no_formats_error": True,
        "format": None,
    })

    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(req.url, download=False)
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))

    formats = info.get("formats") or []
    if not formats:
        raise HTTPException(status_code=400, detail="No downloadable formats found.")

    # ---- Video formats ----
    by_height = {}

    for f in formats:
        # Skip audio-only
        vcodec = f.get("vcodec") or "none"
        if vcodec == "none":
            continue
        if not f.get("format_id"):
            continue

        # Try to get height from multiple fields
        h = f.get("height")
        if not h:
            # Try to parse from format_note like "720p"
            note = f.get("format_note") or ""
            m = re.search(r"(\d{3,4})p", note)
            if m:
                h = int(m.group(1))
        if not h:
            # Try resolution string like "1280x720"
            res = f.get("resolution") or ""
            m = re.search(r"(\d+)x(\d+)", res)
            if m:
                h = int(m.group(2))
        if not h:
            continue

        h = int(h)

        if h not in by_height or score_format(f) > score_format(by_height[h]):
            by_height[h] = f

    qualities = [
        {
            "label": f"{h}p",
            "format_id": fmt["format_id"],
            "type": "video",
            "height": h,
        }
        for h, fmt in sorted(by_height.items(), key=lambda x: x[0], reverse=True)
    ]

    # ---- Fallback: if no per-resolution formats, add generic best ----
    if not qualities:
        qualities.append({
            "label": "Best Available",
            "format_id": "bestvideo",
            "type": "video",
            "height": 9999,
        })

    # ---- Audio only ----
    audio_candidates = [
        f for f in formats
        if (f.get("acodec") or "none") != "none"
        and (f.get("vcodec") or "none") == "none"
        and f.get("format_id")
    ]

    if audio_candidates:
        best_audio = max(audio_candidates, key=lambda x: float(x.get("abr") or x.get("tbr") or 0))
        qualities.append({
            "label": "Audio Only",
            "format_id": best_audio["format_id"],
            "type": "audio",
            "height": 0,
        })
    else:
        # Always offer audio option
        qualities.append({
            "label": "Audio Only",
            "format_id": "bestaudio",
            "type": "audio",
            "height": 0,
        })

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

    if req.format_type == "video":
        format_attempts = [
            f"{req.format_id}+bestaudio[ext=m4a]/best",
            f"{req.format_id}+bestaudio/best",
            f"{req.format_id}/best",
            "bestvideo[ext=mp4]+bestaudio[ext=m4a]/best",
            "bestvideo+bestaudio/best",
            "best",
        ]
    else:
        format_attempts = [
            f"{req.format_id}/bestaudio",
            req.format_id,
            "bestaudio[ext=m4a]/bestaudio/best",
            "best",
        ]

    info = None
    last_error = None

    for fmt in format_attempts:
        try:
            # Clean up partial file from previous attempt
            if os.path.exists(filepath):
                os.remove(filepath)

            ydl_opts = get_ydl_opts({
                "format": fmt,
                "outtmpl": filepath.rsplit(".", 1)[0] + ".%(ext)s",
                "merge_output_format": ext,
            })

            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                info = ydl.extract_info(req.url, download=True)
            break
        except Exception as e:
            last_error = e
            continue

    if info is None:
        raise HTTPException(
            status_code=500,
            detail=str(last_error) if last_error else "Download failed.",
        )

    title = sanitize(info.get("title", "video"))

    # Find the actual downloaded file (extension might differ)
    actual_file = None
    for f in os.listdir(DOWNLOAD_DIR):
        if f.startswith(uid):
            actual_file = f
            break

    if not actual_file:
        raise HTTPException(status_code=500, detail="Download failed — file not found.")

    actual_ext = actual_file.rsplit(".", 1)[-1] if "." in actual_file else ext
    safe_name = f"{title}_{req.quality_label}.{actual_ext}"

    return {
        "download_url": f"/downloads/{actual_file}",
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
