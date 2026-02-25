from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from pydantic import BaseModel
import yt_dlp
import os
import re
import uuid
from typing import Any, Dict, List, Optional

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
GENERIC_SELECTORS = {
    "best",
    "b",
    "bestvideo+bestaudio/best",
    "bestvideo*+bestaudio/best",
    "bv*+ba/b",
    "bestvideo+bestaudio",
    "bestaudio/best",
}


def sanitize_filename(name: str, max_len: int = 90) -> str:
    clean = re.sub(r"[^\w\-. ]", "_", name or "")
    clean = re.sub(r"\s+", " ", clean).strip()
    return clean[:max_len] if clean else "video"


def parse_height(fmt: Dict[str, Any]) -> Optional[int]:
    # Direct numeric height
    h = fmt.get("height")
    if isinstance(h, int) and h > 0:
        return h

    # Parse from text fields like "720p", "1920x1080", etc.
    for field in ("format_note", "resolution", "format"):
        value = fmt.get(field)
        if not isinstance(value, str):
            continue

        m = re.search(r"(\d{3,4})p", value.lower())
        if m:
            return int(m.group(1))

        m2 = re.search(r"\b(\d{3,4})x(\d{3,4})\b", value.lower())
        if m2:
            return int(m2.group(2))

    return None


def format_score(fmt: Dict[str, Any]) -> float:
    score = 0.0
    ext = str(fmt.get("ext") or "").lower()
    protocol = str(fmt.get("protocol") or "").lower()
    vcodec = str(fmt.get("vcodec") or "none").lower()
    acodec = str(fmt.get("acodec") or "none").lower()

    if vcodec != "none":
        score += 1000
    if acodec != "none":
        score += 180  # prefer progressive if possible for compatibility
    if ext == "mp4":
        score += 120
    if protocol.startswith("http"):
        score += 60
    if "m3u8" in protocol:
        score -= 20

    score += float(fmt.get("tbr") or 0) / 20.0
    score += float(fmt.get("fps") or 0)

    return score


def get_ydl_opts(extra: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    opts: Dict[str, Any] = {
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        "retries": 3,
        "fragment_retries": 3,
        "extractor_retries": 2,
        "socket_timeout": 30,
        "http_headers": {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            )
        },
        # Helps with some YouTube format availability issues
        "extractor_args": {
            "youtube": {"player_client": ["android", "web", "tv_embedded"]}
        },
    }

    if os.path.exists(COOKIE_PATH):
        opts["cookiefile"] = COOKIE_PATH

    if extra:
        opts.update(extra)

    return opts


def build_qualities(info: Dict[str, Any]) -> List[Dict[str, Any]]:
    formats = info.get("formats") or []
    best_by_height: Dict[int, Dict[str, Any]] = {}
    audio_candidates: List[Dict[str, Any]] = []

    for fmt in formats:
        format_id = fmt.get("format_id")
        if not format_id:
            continue

        vcodec = str(fmt.get("vcodec") or "none").lower()
        acodec = str(fmt.get("acodec") or "none").lower()

        # Video format candidate
        if vcodec != "none":
            height = parse_height(fmt)
            if height and height >= 144:
                prev = best_by_height.get(height)
                if prev is None or format_score(fmt) > format_score(prev):
                    best_by_height[height] = fmt

        # Audio-only candidate
        if vcodec == "none" and acodec != "none":
            audio_candidates.append(fmt)

    qualities: List[Dict[str, Any]] = []

    for h in sorted(best_by_height.keys(), reverse=True):
        chosen = best_by_height[h]
        qualities.append(
            {
                "label": f"{h}p",
                "format_id": str(chosen["format_id"]),
                "type": "video",
                "height": h,
            }
        )

    # Always include a safe fallback
    qualities.append(
        {
            "label": "Best Available",
            "format_id": "best",
            "type": "video",
            "height": 0,
        }
    )

    if audio_candidates:
        # Prefer higher abr and m4a when possible
        def audio_score(a: Dict[str, Any]) -> float:
            s = float(a.get("abr") or 0)
            if str(a.get("ext") or "").lower() == "m4a":
                s += 50
            return s

        best_audio = max(audio_candidates, key=audio_score)
        qualities.append(
            {
                "label": "Audio Only",
                "format_id": str(best_audio.get("format_id")),
                "type": "audio",
                "height": 0,
            }
        )
    else:
        qualities.append(
            {
                "label": "Audio Only",
                "format_id": "bestaudio/best",
                "type": "audio",
                "height": 0,
            }
        )

    # Deduplicate by label (keep first occurrence)
    deduped: List[Dict[str, Any]] = []
    seen_labels = set()
    for q in qualities:
        if q["label"] in seen_labels:
            continue
        deduped.append(q)
        seen_labels.add(q["label"])

    return deduped


def cleanup_uid_files(uid: str) -> None:
    for name in os.listdir(DOWNLOAD_DIR):
        if name.startswith(uid):
            try:
                os.remove(os.path.join(DOWNLOAD_DIR, name))
            except Exception:
                pass


def find_downloaded_file(uid: str) -> Optional[str]:
    candidates = []
    for name in os.listdir(DOWNLOAD_DIR):
        if not name.startswith(uid):
            continue
        if name.endswith(".part") or name.endswith(".ytdl") or name.endswith(".tmp"):
            continue
        full = os.path.join(DOWNLOAD_DIR, name)
        if os.path.isfile(full):
            candidates.append(full)

    if not candidates:
        return None

    candidates.sort(key=lambda p: os.path.getmtime(p), reverse=True)
    return candidates[0]


def unique_keep_order(items: List[str]) -> List[str]:
    out: List[str] = []
    seen = set()
    for x in items:
        if x in seen:
            continue
        out.append(x)
        seen.add(x)
    return out


def build_format_attempts(req: DownloadRequest) -> List[str]:
    requested = (req.format_id or "").strip()
    req_type = (req.format_type or "video").lower()

    if req_type == "audio":
        attempts: List[str] = []
        if requested:
            attempts.append(requested)
        attempts.extend(
            [
                "bestaudio[ext=m4a]/bestaudio/best",
                "bestaudio/best",
                "best",
            ]
        )
        return unique_keep_order(attempts)

    # video
    attempts = []
    if requested and requested not in GENERIC_SELECTORS:
        attempts.extend(
            [
                f"{requested}+bestaudio/best",
                f"{requested}/best",
            ]
        )
    elif requested:
        attempts.append(requested)

    attempts.extend(
        [
            "bestvideo[ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]/best",
            "bestvideo*+bestaudio/best",
            "best[ext=mp4]/best",
            "best",
        ]
    )

    return unique_keep_order(attempts)


# =========================
# FETCH VIDEO INFO
# =========================
@app.post("/api/fetch")
def fetch_video(req: FetchRequest):
    ydl_opts = get_ydl_opts(
        {
            "skip_download": True,
            "ignore_no_formats_error": True,
            "format": None,
        }
    )

    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(req.url, download=False)
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))

    if not info:
        raise HTTPException(status_code=400, detail="Could not extract video info.")

    qualities = build_qualities(info)

    thumb = info.get("thumbnail")
    if not thumb:
        thumbs = info.get("thumbnails") or []
        if thumbs:
            thumb = thumbs[-1].get("url", "")

    return {
        "title": info.get("title", "Unknown"),
        "thumbnail": thumb or "",
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
    uid = uuid.uuid4().hex[:10]
    attempts = build_format_attempts(req)

    last_error: Optional[str] = None
    extracted_info: Optional[Dict[str, Any]] = None
    downloaded_path: Optional[str] = None

    for fmt in attempts:
        cleanup_uid_files(uid)

        try:
            ydl_opts = get_ydl_opts(
                {
                    "format": fmt,
                    "outtmpl": os.path.join(DOWNLOAD_DIR, f"{uid}.%(ext)s"),
                    "overwrites": True,
                }
            )

            # Only for video; for audio avoid forcing conversion to keep compatibility
            if (req.format_type or "").lower() == "video":
                ydl_opts["merge_output_format"] = "mp4"

            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                extracted_info = ydl.extract_info(req.url, download=True)

            downloaded_path = find_downloaded_file(uid)
            if downloaded_path:
                break

            last_error = "Download completed but output file was not found."

        except Exception as e:
            last_error = str(e)
            continue

    if not downloaded_path:
        raise HTTPException(
            status_code=500,
            detail=last_error or "Download failed after all fallback attempts.",
        )

    stored_name = os.path.basename(downloaded_path)
    ext = os.path.splitext(stored_name)[1].lstrip(".")
    ext = ext or ("mp4" if (req.format_type or "").lower() == "video" else "m4a")

    title = sanitize_filename((extracted_info or {}).get("title", "video"))
    quality = sanitize_filename(req.quality_label or "best", max_len=30)
    safe_name = f"{title}_{quality}.{ext}"

    return {
        "download_url": f"/downloads/{stored_name}",
        "filename": safe_name,
    }


# =========================
# SERVE FILE
# =========================
@app.get("/downloads/{filename}")
def serve_file(filename: str):
    safe_filename = os.path.basename(filename)
    if safe_filename != filename:
        raise HTTPException(status_code=400, detail="Invalid filename")

    filepath = os.path.join(DOWNLOAD_DIR, safe_filename)
    if not os.path.exists(filepath):
        raise HTTPException(status_code=404, detail="File not found")

    return FileResponse(filepath, filename=safe_filename)


# =========================
# START SERVER
# =========================
if __name__ == "__main__":
    import uvicorn

    port = int(os.environ.get("PORT", 10000))
    uvicorn.run(app, host="0.0.0.0", port=port)
