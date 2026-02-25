import asyncio
import os
import json
import uuid
import logging
from pathlib import Path
from fastapi import FastAPI, APIRouter, HTTPException
from fastapi.staticfiles import StaticFiles
from starlette.middleware.cors import CORSMiddleware
from motor.motor_asyncio import AsyncIOMotorClient
from dotenv import load_dotenv
from pydantic import BaseModel
from typing import List, Optional

ROOT_DIR = Path(__file__).parent
load_dotenv(ROOT_DIR / '.env')

DOWNLOADS_DIR = Path("/app/downloads")
DOWNLOADS_DIR.mkdir(exist_ok=True)

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

mongo_url = os.environ['MONGO_URL']
client = AsyncIOMotorClient(mongo_url)
db = client[os.environ['DB_NAME']]

app = FastAPI(title="Zenith Downloader API")
api_router = APIRouter(prefix="/api")


class FetchRequest(BaseModel):
    url: str


class QualityOption(BaseModel):
    label: str
    format_id: str
    type: str
    height: int


class VideoInfo(BaseModel):
    title: str
    thumbnail: str
    duration: Optional[int] = None
    uploader: Optional[str] = None
    view_count: Optional[int] = None
    qualities: List[QualityOption]


class DownloadRequest(BaseModel):
    url: str
    format_id: str
    quality_label: str
    format_type: str  # "video" or "audio"


class DownloadResult(BaseModel):
    filename: str
    download_url: str
    message: str


@api_router.get("/")
async def root():
    return {"message": "Zenith Downloader API"}


@api_router.post("/fetch", response_model=VideoInfo)
async def fetch_video(req: FetchRequest):
    try:
        proc = await asyncio.create_subprocess_exec(
            "yt-dlp", "--dump-json", "--no-playlist", req.url.strip(),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE
        )
        try:
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=30)
        except asyncio.TimeoutError:
            proc.kill()
            raise HTTPException(status_code=408, detail="Request timed out. Please try again.")

        if proc.returncode != 0:
            err = stderr.decode(errors="replace").strip()
            logger.error(f"yt-dlp fetch error: {err}")
            detail = err.split('\n')[-1] if err else "Failed to fetch video info."
            raise HTTPException(status_code=400, detail=detail)

        data = json.loads(stdout.decode(errors="replace"))
        formats = data.get("formats", [])

        # Collect unique heights with video codec
        available_heights = set()
        for fmt in formats:
            h = fmt.get("height")
            vcodec = fmt.get("vcodec") or "none"
            if h and vcodec not in ("none", ""):
                available_heights.add(h)

        height_labels = [
            (2160, "4K (2160p)"),
            (1440, "1440p (2K)"),
            (1080, "1080p"),
            (720, "720p"),
            (480, "480p"),
            (360, "360p"),
            (240, "240p"),
        ]

        quality_options = []
        for target_h, label in height_labels:
            if any(h >= target_h for h in available_heights):
                quality_options.append(QualityOption(
                    label=label,
                    format_id=f"bestvideo[height<={target_h}]+bestaudio/best[height<={target_h}]",
                    type="video",
                    height=target_h
                ))

        # Fallback if no video formats detected
        if not quality_options:
            quality_options.append(QualityOption(
                label="Best Quality",
                format_id="bestvideo+bestaudio/best",
                type="video",
                height=9999
            ))

        # Always add MP3 audio option
        quality_options.append(QualityOption(
            label="Audio only (MP3)",
            format_id="bestaudio/best",
            type="audio",
            height=0
        ))

        return VideoInfo(
            title=data.get("title", "Unknown"),
            thumbnail=data.get("thumbnail", ""),
            duration=data.get("duration"),
            uploader=data.get("uploader") or data.get("channel"),
            view_count=data.get("view_count"),
            qualities=quality_options
        )

    except HTTPException:
        raise
    except json.JSONDecodeError:
        raise HTTPException(status_code=400, detail="Could not parse video info. URL may be unsupported.")
    except Exception as e:
        logger.error(f"Fetch error: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@api_router.post("/download", response_model=DownloadResult)
async def download_video(req: DownloadRequest):
    try:
        file_id = str(uuid.uuid4())[:10]
        output_template = str(DOWNLOADS_DIR / f"{file_id}.%(ext)s")

        if req.format_type == "audio":
            cmd = [
                "yt-dlp",
                "-f", req.format_id,
                "--extract-audio", "--audio-format", "mp3",
                "-o", output_template,
                "--no-playlist",
                "--ffmpeg-location", "/usr/bin/ffmpeg",
                req.url.strip()
            ]
        else:
            cmd = [
                "yt-dlp",
                "-f", req.format_id,
                "-o", output_template,
                "--no-playlist",
                "--merge-output-format", "mp4",
                "--ffmpeg-location", "/usr/bin/ffmpeg",
                req.url.strip()
            ]

        logger.info(f"Starting download: {req.quality_label} - {req.url[:50]}")

        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE
        )
        try:
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=600)
        except asyncio.TimeoutError:
            proc.kill()
            raise HTTPException(status_code=408, detail="Download timed out (10 min limit). Try a lower quality.")

        if proc.returncode != 0:
            err = stderr.decode(errors="replace").strip()
            logger.error(f"yt-dlp download error: {err}")
            detail = err.split('\n')[-1] if err else "Download failed."
            raise HTTPException(status_code=400, detail=detail)

        downloaded_files = list(DOWNLOADS_DIR.glob(f"{file_id}.*"))
        if not downloaded_files:
            raise HTTPException(status_code=500, detail="Downloaded file not found on server.")

        filename = downloaded_files[0].name
        logger.info(f"Download complete: {filename}")

        return DownloadResult(
            filename=filename,
            download_url=f"/api/files/{filename}",
            message="Download complete!"
        )

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Download error: {e}")
        raise HTTPException(status_code=500, detail=str(e))


app.add_middleware(
    CORSMiddleware,
    allow_credentials=True,
    allow_origins=os.environ.get('CORS_ORIGINS', '*').split(','),
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(api_router)
app.mount("/api/files", StaticFiles(directory=str(DOWNLOADS_DIR)), name="files")


@app.on_event("shutdown")
async def shutdown_db_client():
    client.close()
