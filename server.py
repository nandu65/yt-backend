from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from pydantic import BaseModel
import yt_dlp
import os
import uuid

app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

DOWNLOAD_DIR = "downloads"
os.makedirs(DOWNLOAD_DIR, exist_ok=True)

COOKIES_FILE = "cookies.txt"


class URLRequest(BaseModel):
    url: str


class DownloadRequest(BaseModel):
    url: str
    quality: str


@app.post("/api/fetch")
def fetch_video_info(req: URLRequest):
    try:
        ydl_opts = {
            "quiet": True,
            "no_warnings": True,
            "skip_download": True,
            "cookiefile": COOKIES_FILE,
        }
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(req.url, download=False)

        formats = info.get("formats", [])
        quality_options = []
        seen = set()

        for f in formats:
            height = f.get("height")
            if height and height >= 360:
                label = f"{height}p"
                if label not in seen:
                    seen.add(label)
                    quality_options.append({
                        "label": label,
                        "value": str(height),
                        "format_id": f.get("format_id", ""),
                    })

        quality_options.sort(key=lambda x: int(x["value"]), reverse=True)

        if not quality_options:
            quality_options.append({"label": "Best", "value": "best", "format_id": "best"})

        return {
            "title": info.get("title", "Unknown"),
            "thumbnail": info.get("thumbnail", ""),
            "duration": info.get("duration", 0),
            "views": info.get("view_count", 0),
            "qualities": quality_options,
        }
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))


@app.post("/api/download")
def download_video(req: DownloadRequest):
    try:
        file_id = str(uuid.uuid4())[:8]
        output_path = os.path.join(DOWNLOAD_DIR, f"{file_id}.%(ext)s")

        if req.quality == "best":
            format_str = "bestvideo[ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]/best"
        else:
            format_str = f"bestvideo[height<={req.quality}][ext=mp4]+bestaudio[ext=m4a]/best[height<={req.quality}][ext=mp4]/best"

        ydl_opts = {
            "format": format_str,
            "outtmpl": output_path,
            "quiet": True,
            "merge_output_format": "mp4",
            "cookiefile": COOKIES_FILE,
        }

        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(req.url, download=True)
            filename = ydl.prepare_filename(info)
            if not filename.endswith(".mp4"):
                filename = filename.rsplit(".", 1)[0] + ".mp4"

        return {
            "download_url": f"/api/file/{os.path.basename(filename)}",
            "filename": info.get("title", "video") + ".mp4",
        }
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))


@app.get("/api/file/{filename}")
def serve_file(filename: str):
    filepath = os.path.join(DOWNLOAD_DIR, filename)
    if not os.path.exists(filepath):
        raise HTTPException(status_code=404, detail="File not found")
    return FileResponse(filepath, media_type="video/mp4", filename=filename)
