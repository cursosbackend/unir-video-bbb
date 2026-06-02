import asyncio
import logging
import os
import re
import shutil
import uuid
from datetime import datetime
from pathlib import Path
from typing import Optional
from urllib.parse import urlparse

import httpx
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel
from starlette.requests import Request

BASE_DIR = Path(__file__).parent
CLASES_DIR = Path(os.getenv("CLASES_DIR", BASE_DIR.parent / "clases"))
TEMPLATES_DIR = BASE_DIR / "templates"

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("bbb_dashboard")

app = FastAPI(title="Unir Clases BBB")
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))

classes_db: dict[str, dict] = {}


class CreateClassRequest(BaseModel):
    name: str
    url1: str
    url2: Optional[str] = None


class ClassResponse(BaseModel):
    id: str
    name: str
    url1: str
    url2: Optional[str]
    status: str
    progress: int
    error: Optional[str]
    output_filename: Optional[str]
    created_at: str


def parse_bbb_url(playback_url: str) -> tuple[str, str]:
    parsed = urlparse(playback_url)
    server = parsed.netloc
    path_parts = parsed.path.rstrip("/").split("/")
    recording_id = path_parts[-1]
    return server, recording_id


def sanitize_folder_name(name: str) -> str:
    name = re.sub(r'[<>:"/\\|?*]', "_", name)
    name = re.sub(r'\s+', "_", name.strip())
    return name or "clase"


async def download_file(client: httpx.AsyncClient, url: str, dest: Path) -> None:
    logger.info(f"Downloading {url} -> {dest}")
    async with client.stream("GET", url, follow_redirects=True) as resp:
        resp.raise_for_status()
        total = int(resp.headers.get("content-length", 0))

        # Verificar content-type antes de descargar
        ct = resp.headers.get("content-type", "")
        if "text/html" in ct:
            raise RuntimeError(f"URL returned HTML instead of video (content-type: {ct})")

        downloaded = 0
        with open(dest, "wb") as f:
            async for chunk in resp.aiter_bytes(chunk_size=65536):
                f.write(chunk)
                downloaded += len(chunk)
        logger.info(f"Downloaded {dest} ({downloaded}/{total} bytes)")

        # Post-download validation: check file size
        if dest.stat().st_size < 100:
            raise RuntimeError(f"Downloaded file too small ({dest.stat().st_size} bytes), likely invalid")

        # Check for HTML signature
        with open(dest, "rb") as f:
            header = f.read(100)
            if b"<!doctype html" in header.lower() or b"<html" in header.lower():
                dest.unlink()  # delete the bad file
                raise RuntimeError(f"Downloaded file is HTML, not a video")


async def run_ffmpeg(args: list[str], cwd: Optional[Path] = None) -> tuple[int, str, str]:
    logger.info(f"Running ffmpeg: {' '.join(args)}")
    proc = await asyncio.create_subprocess_exec(
        "ffmpeg", *args,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        cwd=str(cwd) if cwd else None,
    )
    stdout, stderr = await proc.communicate()
    out = stdout.decode("utf-8", errors="replace")
    err = stderr.decode("utf-8", errors="replace")
    logger.info(f"ffmpeg exit code: {proc.returncode}")
    return proc.returncode, out, err


def update_progress(class_id: str, status: str, progress: int, error: Optional[str] = None):
    if class_id in classes_db:
        classes_db[class_id]["status"] = status
        classes_db[class_id]["progress"] = progress
        if error:
            classes_db[class_id]["error"] = error
        logger.info(f"[{class_id}] {status} {progress}%")


def get_raw_url_patterns(server: str, recording_id: str) -> list[dict[str, str]]:
    bases = [
        f"https://{server}/presentation/{recording_id}/video",
        f"https://{server}/presentation/{recording_id}",
        f"https://{server}/playback/presentation/2.3/{recording_id}",
        f"https://{server}/playback/presentation/2.0/{recording_id}",
        f"https://{server}/playback/presentation/{recording_id}",
    ]
    return [
        {"deskshare": f"{b}/deskshare.webm", "webcams": f"{b}/webcams.webm"}
        for b in dict.fromkeys(bases)
    ]


async def process_class(class_id: str):
    cls = classes_db[class_id]
    class_name = cls["name"]
    folder_name = sanitize_folder_name(class_name)
    class_dir = CLASES_DIR / f"{folder_name}_{class_id[:8]}"
    class_dir.mkdir(parents=True, exist_ok=True)

    sessions = []
    sessions.append(cls["url1"])
    if cls.get("url2"):
        sessions.append(cls["url2"])

    merged_files = []

    try:
        for idx, url in enumerate(sessions):
            sess_label = f"Sesión {idx + 1}"
            update_progress(class_id, f"Descargando {sess_label}...", int((idx / len(sessions)) * 40))

            server, rec_id = parse_bbb_url(url)
            url_patterns = get_raw_url_patterns(server, rec_id)

            session_dir = class_dir / f"sesion_{idx + 1}"
            session_dir.mkdir(exist_ok=True)

            deskshare_path = session_dir / "deskshare.webm"
            webcams_path = session_dir / "webcams.webm"
            merged_path = session_dir / "merged.webm"

            async with httpx.AsyncClient(timeout=httpx.Timeout(300.0)) as client:
                # Descargar deskshare (opcional)
                deskshare_ok = False
                for pattern in url_patterns:
                    try:
                        await download_file(client, pattern["deskshare"], deskshare_path)
                        deskshare_ok = True
                        break
                    except Exception as e:
                        logger.warning(f"Fallo deskshare {pattern['deskshare']}: {e}")

                # Descargar webcams (obligatorio)
                webcams_ok = False
                for pattern in url_patterns:
                    try:
                        await download_file(client, pattern["webcams"], webcams_path)
                        webcams_ok = True
                        break
                    except Exception as e:
                        logger.warning(f"Fallo webcams {pattern['webcams']}: {e}")

                if not webcams_ok:
                    raise RuntimeError(f"No se pudo descargar webcams.webm para {sess_label}")

            update_progress(class_id, f"Fusionando {sess_label}...", 40 + int((idx / len(sessions)) * 30))

            # Si deskshare no está disponible, usar solo webcams
            if not deskshare_ok:
                logger.info(f"No hay deskshare para {sess_label}, usando solo webcams")
                shutil.copy2(webcams_path, merged_path)
                merged_files.append(merged_path)
                continue  # saltar merge, ir a la siguiente sesión

            if not deskshare_path.exists() or not webcams_path.exists():
                raise RuntimeError(f"Archivos no encontrados para {sess_label}")

            merge_args = [
                "-i", str(deskshare_path),
                "-i", str(webcams_path),
                "-map", "0:v:0",
                "-map", "1:a:0",
                "-c:v", "copy",
                "-c:a", "copy",
                "-shortest",
                str(merged_path),
            ]

            ret, _, err = await run_ffmpeg(merge_args, cwd=session_dir)
            if ret != 0 or not merged_path.exists():
                raise RuntimeError(f"Error fusionando {sess_label}: {err}")

            merged_files.append(merged_path)

        update_progress(class_id, "Concatenando sesiones...", 80)

        if len(merged_files) == 1:
            final_path = class_dir / f"{sanitize_folder_name(class_name)}_completa.webm"
            shutil.copy2(merged_files[0], final_path)
        else:
            final_path = class_dir / f"{sanitize_folder_name(class_name)}_completa.webm"
            filelist = class_dir / "filelist.txt"
            with open(filelist, "w") as f:
                for mf in merged_files:
                    f.write(f"file '{mf}'\n")

            concat_args = [
                "-f", "concat",
                "-safe", "0",
                "-i", str(filelist),
                "-c", "copy",
                str(final_path),
            ]
            ret, _, err = await run_ffmpeg(concat_args, cwd=class_dir)
            if ret != 0 or not final_path.exists():
                logger.warning(f"Concat con copy falló, reintentando con re-encode: {err}")
                concat_args = [
                    "-i", str(merged_files[0]),
                    "-i", str(merged_files[1]),
                    "-filter_complex",
                    "[0:v][0:a][1:v][1:a]concat=n=2:v=1:a=1",
                    "-c:v", "libvpx-vp9",
                    "-c:a", "libvorbis",
                    "-cpu-used", "2",
                    "-deadline", "realtime",
                    str(final_path),
                ]
                ret, _, err2 = await run_ffmpeg(concat_args, cwd=class_dir)
                if ret != 0 or not final_path.exists():
                    raise RuntimeError(f"Error concatenando sesiones: {err2}")

        update_progress(class_id, "completed", 100)

        cls["output_path"] = str(final_path)
        cls["output_filename"] = final_path.name

    except Exception as e:
        logger.error(f"Error procesando clase {class_id}: {e}")
        update_progress(class_id, "error", 0, str(e))
        cls["error"] = str(e)


@app.on_event("startup")
async def startup():
    CLASES_DIR.mkdir(parents=True, exist_ok=True)
    logger.info(f"Directorio de salida: {CLASES_DIR}")


@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    return templates.TemplateResponse(request, "index.html")


@app.get("/api/classes", response_model=list[ClassResponse])
async def list_classes():
    return [
        ClassResponse(
            id=cid,
            name=c["name"],
            url1=c["url1"],
            url2=c.get("url2"),
            status=c["status"],
            progress=c["progress"],
            error=c.get("error"),
            output_filename=c.get("output_filename"),
            created_at=c["created_at"],
        )
        for cid, c in sorted(classes_db.items(), key=lambda x: x[1]["created_at"], reverse=True)
    ]


@app.post("/api/classes", response_model=ClassResponse)
async def create_class(req: CreateClassRequest):
    if not req.name.strip():
        raise HTTPException(status_code=400, detail="El nombre de la clase es requerido")
    if not req.url1.strip():
        raise HTTPException(status_code=400, detail="La URL de la sesión 1 es requerida")

    class_id = uuid.uuid4().hex
    now = datetime.now().isoformat()

    classes_db[class_id] = {
        "name": req.name.strip(),
        "url1": req.url1.strip(),
        "url2": req.url2.strip() if req.url2 else None,
        "status": "pending",
        "progress": 0,
        "error": None,
        "output_path": None,
        "output_filename": None,
        "created_at": now,
    }

    asyncio.create_task(process_class(class_id))

    return ClassResponse(
        id=class_id,
        name=classes_db[class_id]["name"],
        url1=classes_db[class_id]["url1"],
        url2=classes_db[class_id].get("url2"),
        status="pending",
        progress=0,
        error=None,
        output_filename=None,
        created_at=now,
    )


@app.get("/api/classes/{class_id}", response_model=ClassResponse)
async def get_class(class_id: str):
    cls = classes_db.get(class_id)
    if not cls:
        raise HTTPException(status_code=404, detail="Clase no encontrada")
    return ClassResponse(
        id=class_id,
        name=cls["name"],
        url1=cls["url1"],
        url2=cls.get("url2"),
        status=cls["status"],
        progress=cls["progress"],
        error=cls.get("error"),
        output_filename=cls.get("output_filename"),
        created_at=cls["created_at"],
    )


@app.delete("/api/classes/{class_id}")
async def delete_class(class_id: str):
    cls = classes_db.pop(class_id, None)
    if not cls:
        raise HTTPException(status_code=404, detail="Clase no encontrada")
    return {"ok": True}


@app.get("/api/download/{class_id}")
async def download_class(class_id: str):
    cls = classes_db.get(class_id)
    if not cls:
        raise HTTPException(status_code=404, detail="Clase no encontrada")
    if cls["status"] != "completed":
        raise HTTPException(status_code=400, detail="La clase aún no está completa")
    output_path = cls.get("output_path")
    if not output_path or not Path(output_path).exists():
        raise HTTPException(status_code=404, detail="Archivo no encontrado")
    filename = cls.get("output_filename", "video.webm")
    return FileResponse(output_path, filename=filename, media_type="video/webm")


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=True)
