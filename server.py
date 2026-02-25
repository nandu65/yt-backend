from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from pydantic import BaseModel
import yt_dlp
import os
import re
import uuid
from typing import Dict, List, Optional, Tuple, Any

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
    allow_credentials=False,
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
    format_type: str  # "video" | "audio"


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
def sanitize(name: str) -> str:
    return re.sub(r"[^\w\-. ]", "_", (name or "file")).strip()[:90]


def get_ydl_opts(extra: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    opts: Dict[str, Any] = {
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        "http_headers": {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/122.0.0.0 Safari/537.36"
            )
        },
        # Better YouTube compatibility
        "extractor_args": {
            "youtube": {
                "player_client": ["android", "web", "tv_embedded"],
            }
        },
        "retries": 3,
        "fragment_retries": 3,
        "socket_timeout": 30,
    }

    if os.path.exists(COOKIE_PATH):
        opts["cookiefile"] = COOKIE_PATH

    if extra:
        opts.update(extra)

    return opts


def parse_height(fmt: Dict[str, Any]) -> int:
    # Direct height first
    h = fmt.get("height")
    if isinstance(h, int) and h > 0:
        return h

    # resolution like "1280x720"
    resolution = str(fmt.get("resolution") or "")
    m = re.search(r"(\d{3,4})x(\d{3,4})", resolution)
    if m:
        return int(m.group(2))

    # format_note like "720p", "1080p60"
    note = str(fmt.get("format_note") or "")
    m = re.search(r"(\d{3,4})p", note, re.IGNORECASE)
    if m:
        return int(m.group(1))

    return 0


def score_video_format(fmt: Dict[str, Any]) -> float:
    ext = str(fmt.get("ext") or "")
    protocol = str(fmt.get("protocol") or "")
    acodec = fmt.get("acodec")
    tbr = float(fmt.get("tbr") or 0)
    fps = float(fmt.get("fps") or 0)
    height = parse_height(fmt)

    score = 0.0
    if ext == "mp4":
        score += 60
    if "http" in protocol:
        score += 30
    if acodec and acodec != "none":
        score += 20
    score += min(tbr, 30000) / 400
    score += fps / 8
    score += height / 120
    return score


def extract_info_resilient(url: str) -> Dict[str, Any]:
    # Try multiple extraction profiles before failing
    profiles = [
        {"extractor_args": {"youtube": {"player_client": ["android", "web", "tv_embedded"]}}},
        {"extractor_args": {"youtube": {"player_client": ["ios", "mweb", "web"]}}},
    ]

    last_error: Optional[Exception] = None
    for profile in profiles:
        try:
            opts = get_ydl_opts({
                "skip_download": True,
                "ignore_no_formats_error": True,
                "format": None,
                **profile,
            })
            with yt_dlp.YoutubeDL(opts) as ydl:
                info = ydl.extract_info(url, download=False)
            if info:
                return info
        except Exception as e:
            last_error = e

    raise HTTPException(status_code=400, detail=str(last_error) if last_error else "Failed to fetch video info.")


def find_downloaded_file(uid: str) -> Tuple[Optional[str], Optional[str]]:
    candidates: List[Tuple[float, str, str]] = []
    prefix = f"{uid}."

    for name in os.listdir(DOWNLOAD_DIR):
        if not name.startswith(prefix):
            continue
        path = os.path.join(DOWNLOAD_DIR, name)
        if os.path.isfile(path) and os.path.getsize(path) > 0:
            candidates.append((os.path.getmtime(path), path, name))

    if not candidates:
        return None, None

    candidates.sort(key=lambda x: x[0], reverse=True)
    _, path, name = candidates[0]
    return path, name


def cleanup_uid_files(uid: str) -> None:
    prefix = f"{uid}."
    for name in os.listdir(DOWNLOAD_DIR):
        if name.startswith(prefix):
            try:
                os.remove(os.path.join(DOWNLOAD_DIR, name))
            except Exception:
                pass


def build_video_qualities(formats: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    by_height: Dict[int, Dict[str, Any]] = {}

    for f in formats:
        if not f.get("format_id"):
            continue
        if f.get("vcodec") in (None, "none"):
            continue

        h = parse_height(f)
        if h <= 0:
            continue

        prev = by_height.get(h)
        if prev is None or score_video_format(f) > score_video_format(prev):
            by_height[h] = f

    qualities: List[Dict[str, Any]] = []
    for h in sorted(by_height.keys(), reverse=True):
        f = by_height[h]
        qualities.append({
            "label": f"{h}p",
            "format_id": str(f["format_id"]),
            "type": "video",
            "height": h,
        })

    # Always include a reliable video fallback
    qualities.append({
        "label": "Best Available",
        "format_id": "best",
        "type": "video",
        "height": 0,
    })

    # Audio fallback
    qualities.append({
        "label": "Audio Only",
        "format_id": "bestaudio/best",
        "type": "audio",
        "height": 0,
    })

    # Deduplicate labels
    unique = []
    seen = set()
    for q in qualities:
        if q["label"] in seen:
            continue
        seen.add(q["label"])
        unique.append(q)

    return unique


def build_format_attempts(req: DownloadRequest) -> List[str]:
    requested = (req.format_id or "").strip()

    if req.format_type == "audio":
        attempts: List[str] = []
        if requested:
            attempts.append(requested)
        attempts.extend([
            "bestaudio[ext=m4a]/bestaudio/best",
            "bestaudio/best",
            "best",
        ])
        # unique preserve order
        return list(dict.fromkeys(attempts))

    # video attempts
    attempts = []
    if requested and requested not in ("best", "bv*+ba/b"):
        attempts.extend([
            f"{requested}+bestaudio/best",
            f"{requested}/best",
        ])

    attempts.extend([
        "bestvideo[ext=mp4]+bestaudio[ext=m4a]/bestvideo+bestaudio/best",
        "bv*+ba/b",
        "best",
    ])
    return list(dict.fromkeys(attempts))


# =========================
# FETCH VIDEO INFO
# =========================
@app.post("/api/fetch")
def fetch_video(req: FetchRequest):
    info = extract_info_resilient(req.url)
    formats = info.get("formats") or []

    if not isinstance(formats, list):
        formats = []

    qualities = build_video_qualities(formats)

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
    uid = str(uuid.uuid4())[:8]
    outtmpl = os.path.join(DOWNLOAD_DIR, f"{uid}.%(ext)s")

    format_attempts = build_format_attempts(req)
    last_error = "Download failed."

    downloaded_path: Optional[str] = None
    downloaded_name: Optional[str] = None
    final_info: Optional[Dict[str, Any]] = None

    for fmt in format_attempts:
        try:
            cleanup_uid_files(uid)

            ydl_opts = get_ydl_opts({
                "format": fmt,
                "outtmpl": outtmpl,
                "noplaylist": True,
                # If ffmpeg exists, this helps merge to mp4 when possible
                "merge_output_format": "mp4",
            })

            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                final_info = ydl.extract_info(req.url, download=True)

            path, name = find_downloaded_file(uid)
            if path and name:
                downloaded_path, downloaded_name = path, name
                break

            last_error = "Download finished but output file was not found."

        except Exception as e:
            last_error = str(e)
            continue

    if not downloaded_path or not downloaded_name:
        raise HTTPException(status_code=500, detail=last_error)

    title = sanitize((final_info or {}).get("title", "video"))
    quality = sanitize(req.quality_label or ("Audio" if req.format_type == "audio" else "Video"))

    ext = os.path.splitext(downloaded_name)[1].replace(".", "").lower() or "mp4"
    safe_name = f"{title}_{quality}.{ext}"

    return {
        "download_url": f"/downloads/{downloaded_name}",
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
