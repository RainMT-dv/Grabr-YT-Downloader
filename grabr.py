"""
YT Downloader - servidor local (Flask + yt-dlp).

Baixa video/audio do YouTube (e outros sites suportados pelo yt-dlp) e guarda
tudo numa pasta fixa, criada do lado de onde o script roda / na pasta de
Downloads do sistema:

    Termux (Android):    ~/storage/shared/Download/YT_DOWNLOADER_MEDIA
    Linux / Mac / Windows: ~/Downloads/YT_DOWNLOADER_MEDIA

A deteccao do Termux usa a variavel de ambiente TERMUX_VERSION/PREFIX (nao a
simples existencia de uma pasta "storage/shared", que pode existir por outros
motivos em qualquer maquina e confundir a deteccao).

Pode-se sobrescrever o destino com a variavel de ambiente YT_DOWNLOAD_DIR.

Como rodar:
    pip install flask yt-dlp
    python app.py
Depois abrir http://localhost:5000 (ou http://<ip-do-celular>:5000 na mesma rede).
"""

from __future__ import annotations

import mimetypes
import os
import threading
import time
import uuid
from pathlib import Path
from typing import Optional

from flask import Flask, Response, jsonify, request, send_file

app = Flask(__name__)

# --------------------------------------------------------------------------
# Pasta de destino dos arquivos baixados
# --------------------------------------------------------------------------
MEDIA_FOLDER_NAME = "YT_DOWNLOADER_MEDIA"


def _is_termux() -> bool:
    """Deteccao real do Termux -- por env var, nao por pasta que pode existir
    por acaso em qualquer sistema (isso causava pasta errada no Windows)."""
    return bool(os.environ.get("TERMUX_VERSION")) or "com.termux" in os.environ.get("PREFIX", "")


def _resolve_download_dir() -> Path:
    override = os.environ.get("YT_DOWNLOAD_DIR")
    if override:
        return Path(override).expanduser()
    if _is_termux():
        return Path.home() / "storage" / "shared" / "Download" / MEDIA_FOLDER_NAME
    return Path.home() / "Downloads" / MEDIA_FOLDER_NAME


DOWNLOAD_DIR = _resolve_download_dir()
DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)
THUMB_DIR = DOWNLOAD_DIR / ".thumbs"
THUMB_DIR.mkdir(parents=True, exist_ok=True)

MEDIA_EXTENSIONS = (".mp4", ".mkv", ".webm", ".mp3", ".m4a")
VIDEO_EXTENSIONS = (".mp4", ".mkv", ".webm")
THUMB_EXTENSIONS = (".webp", ".jpg", ".jpeg", ".png")
AUDIO_BITRATES = ("128", "192", "256", "320")

import re as _re

def _strip_ansi(s: str) -> str:
    """Remove escape codes ANSI de mensagens de erro do yt-dlp."""
    return _re.sub(r'\x1b\[[0-9;]*[a-zA-Z]', '', s)

FORMAT_STRINGS = {
    "mp4": {
        # Progressivos: video+audio pre-merged, nao precisa de ffmpeg.
        # Cascata: mp4 progressivo na resolucao -> qualquer mp4 -> qualquer formato.
        "360p":  "best[height<=360][ext=mp4]/best[height<=360]/best[ext=mp4]/best",
        "720p":  "best[height<=720][ext=mp4]/best[height<=720]/best[ext=mp4]/best",
        "1080p": "best[height<=1080][ext=mp4]/best[height<=1080]/best[ext=mp4]/best",
        "best":  "best[ext=mp4]/best",
    },
    "mp3": "bestaudio/best",
}

tasks: dict[str, dict] = {}
tasks_lock = threading.Lock()


# --------------------------------------------------------------------------
# Seguranca: nunca deixar um nome de arquivo escapar de DOWNLOAD_DIR
# --------------------------------------------------------------------------
def _safe_path(filename: str) -> Optional[Path]:
    root = DOWNLOAD_DIR.resolve()
    candidate = (DOWNLOAD_DIR / filename).resolve()
    if candidate == root or root not in candidate.parents:
        return None
    return candidate


def _prune_old_tasks(max_age_seconds: int = 3600) -> None:
    now = time.time()
    with tasks_lock:
        stale = [
            tid for tid, t in tasks.items()
            if t.get("status") in ("done", "error", "cancelled") and now - t.get("created_at", now) > max_age_seconds
        ]
        for tid in stale:
            tasks.pop(tid, None)


# --------------------------------------------------------------------------
# Helpers de limpeza pos-download
# --------------------------------------------------------------------------
def _move_stray_thumbs() -> None:
    """Move thumbs (.webp/.jpg/.jpeg/.png) soltos em DOWNLOAD_DIR para THUMB_DIR.
    O yt-dlp sem ffmpeg ignora o outtmpl de thumbnail e grava na pasta principal."""
    for f in list(DOWNLOAD_DIR.iterdir()):
        if f.suffix.lower() in THUMB_EXTENSIONS and f.is_file():
            dest = THUMB_DIR / f.name
            try:
                f.rename(dest)
            except Exception:
                pass  # se falhar (dest ja existe, etc), ignora


def _cleanup_partial_files() -> None:
    """Remove arquivos .part deixados por downloads cancelados ou com erro."""
    for f in list(DOWNLOAD_DIR.iterdir()):
        if f.suffix.lower() == ".part" and f.is_file():
            try:
                f.unlink()
            except Exception:
                pass


# --------------------------------------------------------------------------
# Download em background (com suporte a playlist e cancelamento)
# --------------------------------------------------------------------------
def run_download(task_id: str, url: str, dtype: str, quality: str, playlist: bool, audio_bitrate: str) -> None:
    import yt_dlp
    from yt_dlp.utils import DownloadCancelled

    def progress_hook(d):
        with tasks_lock:
            if task_id not in tasks:
                return
            if tasks[task_id].get("cancel_requested"):
                raise DownloadCancelled("cancelado pelo usuario")

            info_d = d.get("info_dict") or {}
            idx, n, item_title = info_d.get("playlist_index"), info_d.get("n_entries"), info_d.get("title")
            if item_title:
                tasks[task_id]["title"] = f"{item_title} ({idx}/{n})" if idx and n else item_title

            if d.get("status") == "downloading":
                total = d.get("total_bytes") or d.get("total_bytes_estimate") or 0
                downloaded = d.get("downloaded_bytes", 0)
                tasks[task_id]["progress"] = round(downloaded / total * 100, 1) if total else 0
                tasks[task_id]["status"] = "downloading"
            elif d.get("status") == "finished":
                tasks[task_id]["progress"] = 100
                tasks[task_id]["status"] = "processing"

    media_tmpl = (
        str(DOWNLOAD_DIR / "%(playlist_index)02d - %(title)s.%(ext)s")
        if playlist else str(DOWNLOAD_DIR / "%(title)s.%(ext)s")
    )
    thumb_tmpl = (
        str(THUMB_DIR / "%(playlist_index)02d - %(title)s.%(ext)s")
        if playlist else str(THUMB_DIR / "%(title)s.%(ext)s")
    )
    ydl_opts = {
        "outtmpl": {"default": media_tmpl, "thumbnail": thumb_tmpl},
        "progress_hooks": [progress_hook],
        "writethumbnail": True,
        "noplaylist": not playlist,
        "quiet": True,
        "no_warnings": True,
        "windowsfilenames": True,
    }

    if dtype == "mp3":
        ydl_opts["format"] = FORMAT_STRINGS["mp3"]
        ydl_opts["postprocessors"] = [
            {"key": "FFmpegExtractAudio", "preferredcodec": "mp3",
             "preferredquality": audio_bitrate if audio_bitrate in AUDIO_BITRATES else "192"},
            {"key": "FFmpegThumbnailsConvertor", "format": "jpg", "when": "before_dl"},
        ]
    else:
        ydl_opts["format"] = FORMAT_STRINGS["mp4"].get(quality, FORMAT_STRINGS["mp4"]["best"])
        ydl_opts["merge_output_format"] = "mp4"

    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            if not playlist:
                info = ydl.extract_info(url, download=False)
                with tasks_lock:
                    if task_id in tasks:
                        tasks[task_id]["title"] = info.get("title", "Video")
            ydl.download([url])
        with tasks_lock:
            if task_id in tasks:
                tasks[task_id]["status"] = "done"
                tasks[task_id]["progress"] = 100
    except DownloadCancelled:
        with tasks_lock:
            if task_id in tasks:
                tasks[task_id]["status"] = "cancelled"
                tasks[task_id]["error"] = "Cancelado"
        _cleanup_partial_files()
    except Exception as exc:
        with tasks_lock:
            if task_id in tasks:
                tasks[task_id]["status"] = "error"
                tasks[task_id]["error"] = _strip_ansi(str(exc))
        _cleanup_partial_files()
    finally:
        _move_stray_thumbs()
        _prune_old_tasks()


# --------------------------------------------------------------------------
# Rotas
# --------------------------------------------------------------------------
@app.route("/")
def index():
    return HTML_PAGE


@app.get("/favicon.ico")
def favicon():
    return Response(status=204)


@app.get("/api/folder")
def api_folder():
    return jsonify(path=str(DOWNLOAD_DIR))


@app.post("/api/info")
def api_info():
    """Busca titulo/thumbnail/duracao (ou tamanho de playlist) sem baixar."""
    data = request.get_json(silent=True) or {}
    url = (data.get("url") or "").strip()
    if not url:
        return jsonify(error="URL obrigatoria"), 400

    import yt_dlp

    opts = {"quiet": True, "no_warnings": True, "skip_download": True, "extract_flat": "in_playlist"}
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=False)
    except Exception as exc:
        return jsonify(error=str(exc)), 400

    entries = info.get("entries")
    if entries is not None:
        entries = list(entries)
        first = entries[0] if entries else {}
        thumb = first.get("thumbnail")
        if not thumb and first.get("thumbnails"):
            thumb = first["thumbnails"][-1].get("url")
        return jsonify(is_playlist=True, title=info.get("title") or "Playlist",
                       count=len(entries), thumbnail=thumb)

    thumb = info.get("thumbnail")
    if not thumb and info.get("thumbnails"):
        thumb = info["thumbnails"][-1].get("url")
    return jsonify(is_playlist=False, title=info.get("title", "Video"),
                   duration=info.get("duration"), thumbnail=thumb)


@app.post("/api/download")
def api_download():
    data = request.get_json(silent=True) or {}
    url = (data.get("url") or "").strip()
    dtype = data.get("type", "mp4")
    quality = data.get("quality", "best")
    playlist = bool(data.get("playlist", False))
    audio_bitrate = str(data.get("audio_bitrate", "192"))

    if not url:
        return jsonify(error="URL obrigatoria"), 400
    if dtype not in ("mp4", "mp3"):
        return jsonify(error="Tipo invalido"), 400
    if audio_bitrate not in AUDIO_BITRATES:
        audio_bitrate = "192"

    task_id = str(uuid.uuid4())[:8]
    with tasks_lock:
        tasks[task_id] = {
            "status": "starting",
            "progress": 0,
            "title": "",
            "error": "",
            "created_at": time.time(),
            "cancel_requested": False,
        }
    threading.Thread(
        target=run_download, args=(task_id, url, dtype, quality, playlist, audio_bitrate), daemon=True
    ).start()
    return jsonify(task_id=task_id)


@app.post("/api/cancel/<task_id>")
def api_cancel(task_id):
    with tasks_lock:
        if task_id not in tasks:
            return jsonify(error="tarefa nao encontrada"), 404
        tasks[task_id]["cancel_requested"] = True
    return jsonify(ok=True)


@app.get("/api/status")
def api_status():
    with tasks_lock:
        return jsonify(tasks=dict(tasks))


@app.get("/api/files")
def api_files():
    files = []
    for f in sorted(DOWNLOAD_DIR.iterdir(), key=lambda x: x.stat().st_mtime, reverse=True):
        if f.suffix.lower() not in MEDIA_EXTENSIONS:
            continue
        has_thumb = any((THUMB_DIR / (f.stem + ext)).exists() for ext in THUMB_EXTENSIONS)
        st = f.stat()
        files.append({
            "name": f.name,
            "size_mb": round(st.st_size / (1024 * 1024), 1),
            "size": f"{st.st_size / (1024 * 1024):.1f} MB",
            "date": time.strftime("%Y-%m-%d %H:%M", time.localtime(st.st_mtime)),
            "mtime": st.st_mtime,
            "has_thumb": has_thumb,
            "type": "video" if f.suffix.lower() in VIDEO_EXTENSIONS else "audio",
        })
    return jsonify(files)


@app.get("/api/thumbnail/<path:filename>")
def api_thumbnail(filename):
    # _safe_path valida contra DOWNLOAD_DIR; para thumbs usamos THUMB_DIR direto
    stem = Path(filename).stem
    if not stem or ".." in stem or "/" in stem or "\\" in stem:
        return Response("Caminho invalido", status=400)
    for ext in THUMB_EXTENSIONS:
        p = THUMB_DIR / (stem + ext)
        if p.exists():
            mt = mimetypes.guess_type(str(p))[0] or "image/webp"
            return send_file(str(p), mimetype=mt)
    return Response("Not found", status=404)


@app.get("/api/file/<path:filename>")
def api_file(filename):
    safe = _safe_path(filename)
    if safe is None or not safe.exists():
        return Response("Not found", status=404)
    mt_map = {
        ".mp4": "video/mp4", ".mkv": "video/x-matroska", ".webm": "video/webm",
        ".mp3": "audio/mpeg", ".m4a": "audio/mp4",
    }
    mt = mt_map.get(safe.suffix.lower(), mimetypes.guess_type(str(safe))[0] or "application/octet-stream")
    return send_file(str(safe), mimetype=mt)


@app.delete("/api/delete/<path:filename>")
def api_delete(filename):
    safe = _safe_path(filename)
    if safe is None:
        return jsonify(error="Caminho invalido"), 400
    if safe.exists():
        safe.unlink()
    for ext in THUMB_EXTENSIONS:
        for tdir in (DOWNLOAD_DIR, THUMB_DIR):
            thumb = tdir / (safe.stem + ext)
            if thumb.exists():
                thumb.unlink()
    return jsonify(ok=True)


# --------------------------------------------------------------------------
# Frontend redesenhado: layout split sidebar + area principal
# --------------------------------------------------------------------------
HTML_PAGE = r"""<!DOCTYPE html>
<html lang="pt-BR">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>grabr</title>
<style>
@import url('https://fonts.googleapis.com/css2?family=JetBrains+Mono:wght@400;500;600;700&display=swap');

*,*::before,*::after{box-sizing:border-box;margin:0;padding:0}

:root{
  --bg:#07070f;
  --sidebar:#0c0c18;
  --surface:#11111f;
  --surface2:#181828;
  --surface3:#1f1f32;
  --border:rgba(255,255,255,.06);
  --border-bright:rgba(255,255,255,.12);

  --cyan:#22d3ee;
  --cyan-dim:rgba(34,211,238,.12);
  --cyan-glow:rgba(34,211,238,.25);
  --amber:#f59e0b;
  --amber-dim:rgba(245,158,11,.12);
  --red:#f43f5e;
  --green:#10b981;

  --text:#e2e2f0;
  --text2:#7878a0;
  --text3:#404060;

  --mono:'JetBrains Mono',ui-monospace,SFMono-Regular,Menlo,monospace;
  --sans:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;

  --radius:10px;
  --radius-sm:6px;
  --sidebar-w:240px;
}

html{background:var(--bg);color:var(--text);font-family:var(--sans);font-size:14px;line-height:1.5;-webkit-tap-highlight-color:transparent}

/* ---- LAYOUT ---- */
.app{display:grid;grid-template-columns:var(--sidebar-w) 1fr;min-height:100dvh}

/* ---- SIDEBAR ---- */
.sidebar{
  background:var(--sidebar);
  border-right:1px solid var(--border);
  display:flex;flex-direction:column;
  position:sticky;top:0;height:100dvh;overflow:hidden;
}
.sidebar-logo{
  padding:24px 20px 20px;
  border-bottom:1px solid var(--border);
}
.logo-mark{
  display:flex;align-items:center;gap:10px;margin-bottom:4px;
}
.logo-icon{
  width:32px;height:32px;border-radius:8px;
  background:linear-gradient(135deg,var(--cyan),#0891b2);
  display:flex;align-items:center;justify-content:center;flex-shrink:0;
}
.logo-icon svg{width:16px;height:16px;fill:none;stroke:#07070f;stroke-width:2.5;stroke-linecap:round;stroke-linejoin:round}
.logo-name{font-family:var(--mono);font-size:.95rem;font-weight:700;letter-spacing:-.02em;color:var(--text)}
.logo-sub{font-family:var(--mono);font-size:.65rem;color:var(--text3);letter-spacing:.08em}

.sidebar-nav{padding:16px 10px;flex:1;display:flex;flex-direction:column;gap:2px}
.nav-item{
  display:flex;align-items:center;gap:10px;
  padding:9px 12px;border-radius:var(--radius-sm);
  font-size:.82rem;font-weight:500;color:var(--text2);
  cursor:pointer;border:none;background:none;width:100%;text-align:left;
  transition:all .15s;font-family:var(--sans);
  position:relative;
}
.nav-item svg{width:16px;height:16px;flex-shrink:0;opacity:.7;transition:opacity .15s}
.nav-item:hover{background:var(--surface);color:var(--text)}
.nav-item:hover svg{opacity:1}
.nav-item.active{background:var(--surface2);color:var(--cyan);font-weight:600}
.nav-item.active svg{opacity:1;stroke:var(--cyan)}
.nav-item.active::before{
  content:'';position:absolute;left:0;top:50%;transform:translateY(-50%);
  width:3px;height:60%;background:var(--cyan);border-radius:0 3px 3px 0;
}
.nav-badge{margin-left:auto;font-family:var(--mono);font-size:.65rem;
  background:var(--surface3);color:var(--text3);padding:2px 6px;border-radius:99px}
.nav-item.active .nav-badge{background:var(--cyan-dim);color:var(--cyan)}

.sidebar-footer{
  padding:16px 20px;border-top:1px solid var(--border);
}
.folder-path{
  font-family:var(--mono);font-size:.62rem;color:var(--text3);
  word-break:break-all;line-height:1.4;
}
.folder-label{font-size:.6rem;color:var(--text3);text-transform:uppercase;
  letter-spacing:.1em;margin-bottom:4px;display:block}

/* ---- MAIN ---- */
.main{display:flex;flex-direction:column;min-height:100dvh;overflow:hidden}
.main-header{
  padding:24px 32px 0;border-bottom:1px solid var(--border);
  display:flex;align-items:flex-end;gap:0;
}
.tab-nav{display:flex;gap:0}
.tab-btn{
  padding:12px 20px;background:none;border:none;
  font-size:.82rem;font-weight:500;color:var(--text2);cursor:pointer;
  border-bottom:2px solid transparent;margin-bottom:-1px;
  transition:all .15s;font-family:var(--sans);
  display:flex;align-items:center;gap:7px;
}
.tab-btn svg{width:14px;height:14px;opacity:.6}
.tab-btn:hover{color:var(--text)}
.tab-btn.active{color:var(--cyan);border-bottom-color:var(--cyan)}
.tab-btn.active svg{opacity:1;stroke:var(--cyan)}

.main-body{flex:1;padding:28px 32px;overflow-y:auto}

/* ---- PANELS ---- */
.panel{display:none}
.panel.active{display:block}

/* ---- URL SECTION ---- */
.url-section{margin-bottom:20px}
.field-label{
  font-family:var(--mono);font-size:.68rem;font-weight:600;
  color:var(--text2);text-transform:uppercase;letter-spacing:.1em;
  margin-bottom:8px;display:block;
}
.url-row{display:flex;gap:8px;align-items:stretch}
.url-input{
  flex:1;padding:12px 14px;
  background:var(--surface);border:1px solid var(--border);border-radius:var(--radius);
  color:var(--text);font-size:.9rem;font-family:var(--mono);outline:none;
  transition:border-color .2s,box-shadow .2s;
}
.url-input::placeholder{color:var(--text3);font-size:.82rem}
.url-input:focus{border-color:var(--cyan);box-shadow:0 0 0 3px var(--cyan-dim)}
.url-input.error{border-color:var(--red);box-shadow:0 0 0 3px rgba(244,63,94,.15)}
.paste-btn{
  padding:0 14px;background:var(--surface);border:1px solid var(--border);
  border-radius:var(--radius);color:var(--text2);cursor:pointer;
  font-size:.8rem;font-family:var(--mono);white-space:nowrap;
  transition:all .15s;display:flex;align-items:center;gap:6px;
}
.paste-btn svg{width:14px;height:14px}
.paste-btn:hover{border-color:var(--border-bright);color:var(--text)}
.paste-btn:active{transform:scale(.97)}

/* ---- PREVIEW ---- */
.preview-card{
  display:flex;gap:14px;align-items:center;
  background:var(--surface);border:1px solid var(--border);
  border-left:3px solid var(--cyan);border-radius:var(--radius);
  padding:14px;margin-bottom:20px;
  animation:slideIn .2s ease;
}
.preview-card.playlist{border-left-color:var(--amber)}
.preview-thumb{width:100px;height:62px;border-radius:6px;object-fit:cover;
  background:var(--surface2);flex-shrink:0}
.preview-info{min-width:0;flex:1}
.preview-title{font-size:.88rem;font-weight:600;white-space:nowrap;
  overflow:hidden;text-overflow:ellipsis;margin-bottom:4px}
.preview-meta{font-family:var(--mono);font-size:.7rem;color:var(--text2)}
.preview-meta.pl{color:var(--amber)}
.preview-loading{display:flex;align-items:center;gap:8px;
  font-family:var(--mono);font-size:.72rem;color:var(--text3)}

/* ---- OPTIONS GRID ---- */
.options-grid{
  display:grid;grid-template-columns:1fr 1fr 1fr;gap:12px;margin-bottom:16px;
}
.opt-block{display:flex;flex-direction:column;gap:6px}

select,input[type=text]{font-family:inherit}
.select-field{
  width:100%;padding:10px 12px;
  background:var(--surface);border:1px solid var(--border);
  border-radius:var(--radius-sm);color:var(--text);font-size:.84rem;outline:none;
  cursor:pointer;transition:border-color .15s;
  -webkit-appearance:none;appearance:none;
  background-image:url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' width='10' height='6'%3E%3Cpath d='M0 0l5 6 5-6z' fill='%237878a0'/%3E%3C/svg%3E");
  background-repeat:no-repeat;background-position:right 10px center;
  padding-right:28px;
}
.select-field:focus{border-color:var(--cyan)}

/* ---- TOGGLE ROW ---- */
.toggle-row{
  display:flex;align-items:center;gap:10px;
  padding:10px 14px;background:var(--surface);
  border:1px solid var(--border);border-radius:var(--radius-sm);
  margin-bottom:16px;cursor:pointer;
}
.toggle-row input[type=checkbox]{
  width:16px;height:16px;accent-color:var(--amber);cursor:pointer;flex-shrink:0;
}
.toggle-label{font-size:.82rem;color:var(--text2);cursor:pointer}

/* ---- DOWNLOAD BTN ---- */
.btn-download{
  width:100%;padding:13px;
  background:var(--cyan);color:#06111a;
  border:none;border-radius:var(--radius);
  font-size:.9rem;font-weight:700;font-family:var(--mono);
  cursor:pointer;letter-spacing:.02em;
  transition:all .2s;
  display:flex;align-items:center;justify-content:center;gap:8px;
}
.btn-download-icon{width:16px;height:16px;display:inline-block;flex-shrink:0;background:url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 24 24' fill='none' stroke='%2306111a' stroke-width='2.5' stroke-linecap='round' stroke-linejoin='round'%3E%3Cpath d='M12 3v13'/%3E%3Cpolyline points='7 11 12 16 17 11'/%3E%3Cpath d='M3 19h18'/%3E%3C/svg%3E") center/contain no-repeat;}
.btn-download:hover{background:#06b6d4;box-shadow:0 0 28px var(--cyan-glow)}
.btn-download:active{transform:scale(.98)}

/* ---- QUEUE ---- */
.queue{margin-top:20px;display:flex;flex-direction:column;gap:8px}
.queue-section-title{
  font-family:var(--mono);font-size:.65rem;font-weight:600;color:var(--text3);
  text-transform:uppercase;letter-spacing:.12em;margin-bottom:4px;
}
.queue-item{
  background:var(--surface);border:1px solid var(--border);
  border-left:3px solid var(--cyan);border-radius:var(--radius);
  padding:12px 14px;animation:slideIn .2s ease;
}
.queue-item.audio{border-left-color:var(--amber)}
.queue-item.done{border-left-color:var(--green);opacity:.7}
.queue-item.error,.queue-item.cancelled{border-left-color:var(--red);opacity:.7}

.queue-head{display:flex;justify-content:space-between;align-items:flex-start;gap:8px;margin-bottom:8px}
.queue-title{font-size:.82rem;font-weight:500;line-height:1.3;flex:1;
  display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical;overflow:hidden}
.queue-type-badge{
  font-family:var(--mono);font-size:.58rem;font-weight:700;text-transform:uppercase;
  width:36px;height:20px;border-radius:99px;flex-shrink:0;
  background:var(--cyan-dim);color:var(--cyan);
  display:inline-flex;align-items:center;justify-content:center;
  letter-spacing:.04em;line-height:1;
}
.queue-item.audio .queue-type-badge{background:var(--amber-dim);color:var(--amber)}

.queue-progress-row{display:flex;align-items:center;gap:8px}
.queue-bar-bg{flex:1;height:3px;background:var(--surface3);border-radius:99px;overflow:hidden}
.queue-bar{height:100%;width:0%;border-radius:99px;
  background:linear-gradient(90deg,var(--cyan),#67e8f9);
  transition:width .3s ease;animation:pulse-bar 1.4s ease-in-out infinite}
.queue-item.audio .queue-bar{background:linear-gradient(90deg,var(--amber),#fcd34d)}
.queue-bar.idle{animation:none}
.queue-item.done .queue-bar{background:var(--green)}
.queue-item.error .queue-bar,.queue-item.cancelled .queue-bar{background:var(--red)}
.queue-pct{font-family:var(--mono);font-size:.65rem;color:var(--text2);min-width:32px;text-align:right}

.queue-status{font-family:var(--mono);font-size:.7rem;color:var(--text2);margin-top:5px}
.queue-status.done{color:var(--green)}
.queue-status.error{color:var(--red)}

.queue-actions{display:flex;gap:4px;flex-shrink:0}
.q-btn{
  width:26px;height:26px;border:none;border-radius:5px;
  background:var(--surface2);color:var(--text2);cursor:pointer;
  font-size:.75rem;display:flex;align-items:center;justify-content:center;
  transition:all .15s;
}
.q-btn:hover{background:var(--red);color:#fff}
.q-btn.remove:hover{background:var(--surface3);color:var(--text)}

/* ---- FILES PANEL ---- */
.files-header{
  display:flex;align-items:center;gap:10px;margin-bottom:16px;flex-wrap:wrap;
}
.files-search{
  flex:1;min-width:180px;padding:9px 12px;
  background:var(--surface);border:1px solid var(--border);
  border-radius:var(--radius-sm);color:var(--text);font-size:.84rem;outline:none;
  transition:border-color .2s;font-family:var(--mono);
}
.files-search::placeholder{color:var(--text3)}
.files-search:focus{border-color:var(--cyan)}
.files-sort{
  padding:9px 28px 9px 10px;
  background:var(--surface);border:1px solid var(--border);
  border-radius:var(--radius-sm);color:var(--text2);font-size:.82rem;outline:none;
  cursor:pointer;-webkit-appearance:none;appearance:none;
  background-image:url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' width='10' height='6'%3E%3Cpath d='M0 0l5 6 5-6z' fill='%237878a0'/%3E%3C/svg%3E");
  background-repeat:no-repeat;background-position:right 8px center;
}
.files-sort:focus{border-color:var(--cyan)}
.files-stats{
  font-family:var(--mono);font-size:.65rem;color:var(--text3);
  margin-bottom:12px;text-align:right;
}

.file-grid{display:flex;flex-direction:column;gap:6px}
.file-card{
  display:grid;grid-template-columns:72px 1fr auto;
  gap:12px;align-items:center;
  background:var(--surface);border:1px solid var(--border);
  border-radius:var(--radius);padding:10px 12px;
  transition:all .15s;
}
.file-card:hover{border-color:var(--border-bright);background:var(--surface2)}
.file-card.audio{border-left:3px solid var(--amber)}
.file-card.video{border-left:3px solid var(--cyan)}

.file-thumb{width:72px;height:44px;border-radius:5px;object-fit:cover;
  background:var(--surface2)}
.file-thumb-ph{
  width:72px;height:44px;border-radius:5px;
  display:flex;align-items:center;justify-content:center;
}
.file-thumb-ph.video{background:var(--cyan-dim)}
.file-thumb-ph.audio{background:var(--amber-dim)}
.file-thumb-ph svg{width:20px;height:20px}
.file-thumb-ph.video svg{stroke:var(--cyan)}
.file-thumb-ph.audio svg{stroke:var(--amber)}

.file-info{min-width:0}
.file-name{font-size:.84rem;font-weight:500;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.file-meta{font-family:var(--mono);font-size:.67rem;color:var(--text2);margin-top:2px}

.file-btns{display:flex;gap:4px}
.file-btn{
  width:32px;height:32px;border:none;border-radius:var(--radius-sm);
  background:var(--surface2);color:var(--text2);cursor:pointer;
  display:flex;align-items:center;justify-content:center;
  transition:all .15s;flex-shrink:0;
}
.file-btn svg{width:14px;height:14px;stroke:currentColor;stroke-width:2;stroke-linecap:round;stroke-linejoin:round;fill:none}
.file-btn:hover{background:var(--surface3);color:var(--text)}
.file-btn.play:hover{background:var(--cyan);color:#06111a}
.file-btn.play:hover svg{stroke:#06111a}
.file-btn.del:hover{background:var(--red);color:#fff}
.file-btn.del:hover svg{stroke:#fff}
.file-btn:active{transform:scale(.92)}

/* ---- EMPTY / LOADING ---- */
.empty{text-align:center;padding:60px 20px;color:var(--text3)}
.empty svg{width:40px;height:40px;margin-bottom:14px;opacity:.3;stroke:var(--text3);stroke-width:1.5;fill:none}
.empty p{font-size:.84rem}
.spinner{width:22px;height:22px;border:2px solid var(--surface3);
  border-top-color:var(--cyan);border-radius:50%;animation:spin .5s linear infinite;margin:40px auto}

/* ---- MODAL ---- */
.modal-overlay{
  position:fixed;inset:0;background:rgba(0,0,0,.88);
  z-index:100;display:none;align-items:center;justify-content:center;padding:20px;
}
.modal-overlay.show{display:flex;animation:fadeIn .15s ease}
.modal-box{
  background:var(--surface);border:1px solid var(--border);
  border-radius:14px;width:100%;max-width:860px;overflow:hidden;
  position:relative;box-shadow:0 24px 64px rgba(0,0,0,.6);
}
.modal-close{
  position:absolute;top:10px;right:10px;width:30px;height:30px;
  border:none;background:rgba(0,0,0,.5);color:#fff;border-radius:50%;
  font-size:1rem;cursor:pointer;z-index:2;display:flex;align-items:center;justify-content:center;
  transition:background .15s;
}
.modal-close:hover{background:var(--red)}
.modal-box video,.modal-box audio{width:100%;display:block;outline:none}
.modal-box audio{padding:24px}
.modal-title{
  padding:10px 14px;font-family:var(--mono);font-size:.7rem;
  color:var(--text2);white-space:nowrap;overflow:hidden;text-overflow:ellipsis;
  border-top:1px solid var(--border);
}

/* ---- ANIMATIONS ---- */
@keyframes spin{to{transform:rotate(360deg)}}
@keyframes pulse-bar{0%,100%{opacity:1}50%{opacity:.6}}
@keyframes slideIn{from{opacity:0;transform:translateY(6px)}to{opacity:1;transform:none}}
@keyframes fadeIn{from{opacity:0}to{opacity:1}}

:focus-visible{outline:2px solid var(--cyan);outline-offset:2px}

/* ---- MOBILE ---- */
@media(max-width:767px){
  .app{grid-template-columns:1fr;grid-template-rows:auto 1fr}
  .sidebar{
    position:static;height:auto;flex-direction:row;
    border-right:none;border-bottom:1px solid var(--border);
    overflow-x:auto;
  }
  .sidebar-logo{padding:12px 16px;border-bottom:none;border-right:1px solid var(--border);white-space:nowrap}
  .logo-sub{display:none}
  .sidebar-nav{flex-direction:row;padding:8px;flex:1}
  .nav-item{padding:8px 12px;font-size:.78rem;white-space:nowrap}
  .nav-item.active::before{top:auto;bottom:0;left:50%;transform:translateX(-50%);
    width:60%;height:2px;border-radius:2px 2px 0 0}
  .nav-badge{display:none}
  .sidebar-footer{display:none}
  .main-header{display:none}
  .main-body{padding:16px}
  .options-grid{grid-template-columns:1fr 1fr}
  .file-card{grid-template-columns:56px 1fr auto}
  .file-thumb,.file-thumb-ph{width:56px;height:36px}
}

@media(min-width:768px) and (max-width:1199px){
  :root{--sidebar-w:200px}
  .main-body{padding:24px}
}
</style>
</head>
<body>
<div class="app">

  <!-- SIDEBAR -->
  <aside class="sidebar">
    <div class="sidebar-logo">
      <div class="logo-mark">
        <div class="logo-icon">
          <svg viewBox="0 0 24 24"><path d="M21 15v4a2 2 0 01-2 2H5a2 2 0 01-2-2v-4"/><polyline points="7 10 12 15 17 10"/><line x1="12" y1="15" x2="12" y2="3"/></svg>
        </div>
        <span class="logo-name">grabr</span>
      </div>
      <div class="logo-sub">media grabber</div>
    </div>

    <nav class="sidebar-nav">
      <button class="nav-item active" data-view="download">
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="10"/><polyline points="8 12 12 16 16 12"/><line x1="12" y1="8" x2="12" y2="16"/></svg>
        Download
        <span class="nav-badge" id="active-count"></span>
      </button>
      <button class="nav-item" data-view="files">
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M22 19a2 2 0 01-2 2H4a2 2 0 01-2-2V5a2 2 0 012-2h5l2 3h9a2 2 0 012 2z"/></svg>
        Arquivos
        <span class="nav-badge" id="file-count-nav"></span>
      </button>
    </nav>

    <div class="sidebar-footer">
      <span class="folder-label">pasta de saida</span>
      <div class="folder-path" id="folder-path">carregando...</div>
    </div>
  </aside>

  <!-- MAIN -->
  <main class="main">
    <div class="main-header">
      <div class="tab-nav">
        <button class="tab-btn active" data-view="download">
          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="10"/><polyline points="8 12 12 16 16 12"/><line x1="12" y1="8" x2="12" y2="16"/></svg>
          Download
        </button>
        <button class="tab-btn" data-view="files">
          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M22 19a2 2 0 01-2 2H4a2 2 0 01-2-2V5a2 2 0 012-2h5l2 3h9a2 2 0 012 2z"/></svg>
          Arquivos
        </button>
      </div>
    </div>

    <div class="main-body">

      <!-- DOWNLOAD PANEL -->
      <div class="panel active" id="panel-download">

        <div class="url-section">
          <span class="field-label">URL do video ou playlist</span>
          <div class="url-row">
            <input class="url-input" id="url-input" type="text"
              placeholder="https://youtube.com/watch?v=..." autocomplete="off">
            <button class="paste-btn" id="btn-paste">
              <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M16 4h2a2 2 0 012 2v14a2 2 0 01-2 2H6a2 2 0 01-2-2V6a2 2 0 012-2h2"/><rect x="8" y="2" width="8" height="4" rx="1" ry="1"/></svg>
              Colar
            </button>
          </div>
        </div>

        <div id="preview-card" class="preview-card" style="display:none">
          <img id="preview-thumb" class="preview-thumb" src="" alt="">
          <div class="preview-info">
            <div class="preview-title" id="preview-title"></div>
            <div class="preview-meta" id="preview-meta"></div>
          </div>
        </div>

        <div class="options-grid">
          <div class="opt-block">
            <span class="field-label">Formato</span>
            <select class="select-field" id="type-select">
              <option value="mp4">MP4 - Video</option>
              <option value="mp3">MP3 - Audio</option>
            </select>
          </div>
          <div class="opt-block" id="quality-block">
            <span class="field-label">Qualidade</span>
            <select class="select-field" id="quality-select">
              <option value="best">Melhor disponivel</option>
              <option value="1080p">1080p</option>
              <option value="720p">720p</option>
              <option value="360p">360p</option>
            </select>
          </div>
          <div class="opt-block" id="bitrate-block" style="display:none">
            <span class="field-label">Bitrate</span>
            <select class="select-field" id="bitrate-select">
              <option value="128">128 kbps</option>
              <option value="192" selected>192 kbps</option>
              <option value="256">256 kbps</option>
              <option value="320">320 kbps</option>
            </select>
          </div>
        </div>

        <label class="toggle-row" for="playlist-toggle">
          <input type="checkbox" id="playlist-toggle">
          <span class="toggle-label">Baixar playlist inteira (se a URL for de uma playlist)</span>
        </label>

        <button class="btn-download" id="btn-download">
          <span class="btn-download-icon"></span>
          Adicionar a fila
        </button>

        <div class="queue" id="queue"></div>
      </div>

      <!-- FILES PANEL -->
      <div class="panel" id="panel-files">
        <div class="files-header">
          <input class="files-search" id="files-search" type="text" placeholder="Buscar por nome...">
          <select class="files-sort" id="files-sort">
            <option value="date">Mais recentes</option>
            <option value="name">A - Z</option>
            <option value="size">Maior tamanho</option>
          </select>
        </div>
        <div class="files-stats" id="files-stats"></div>
        <div id="files-container"><div class="spinner"></div></div>
      </div>

    </div>
  </main>
</div>

<!-- MODAL -->
<div class="modal-overlay" id="modal">
  <div class="modal-box">
    <button class="modal-close" id="modal-close">&#x2715;</button>
    <div id="modal-content"></div>
    <div class="modal-title" id="modal-title"></div>
  </div>
</div>

<script>
const $=s=>document.querySelector(s);
const $$=s=>[...document.querySelectorAll(s)];

// ---------- Folder path ----------
fetch('/api/folder').then(r=>r.json()).then(d=>{
  $('#folder-path').textContent=d.path;
}).catch(()=>{$('#folder-path').textContent='n/d'});

// ---------- Navigation ----------
function setView(view){
  $$('.nav-item').forEach(b=>{b.classList.toggle('active',b.dataset.view===view)});
  $$('.tab-btn').forEach(b=>{b.classList.toggle('active',b.dataset.view===view)});
  $$('.panel').forEach(p=>{p.classList.remove('active')});
  const panel=$('#panel-'+view);
  if(panel) panel.classList.add('active');
  if(view==='files') loadFiles();
}
$$('.nav-item, .tab-btn').forEach(btn=>{
  btn.addEventListener('click',()=>setView(btn.dataset.view));
});

// ---------- Format switch ----------
$('#type-select').addEventListener('change',()=>{
  const isAudio=$('#type-select').value==='mp3';
  $('#quality-block').style.display=isAudio?'none':'';
  $('#bitrate-block').style.display=isAudio?'':'none';
});

// ---------- Paste ----------
$('#btn-paste').addEventListener('click',async()=>{
  try{
    const t=await navigator.clipboard.readText();
    if(t){$('#url-input').value=t.trim();fetchPreview();}
  }catch(e){}
});

// ---------- Preview ----------
let previewTimer=null;
$('#url-input').addEventListener('input',()=>{
  clearTimeout(previewTimer);
  previewTimer=setTimeout(fetchPreview,600);
});
$('#url-input').addEventListener('keydown',e=>{
  if(e.key==='Enter')$('#btn-download').click();
});

async function fetchPreview(){
  const url=$('#url-input').value.trim();
  if(!url){$('#preview-card').style.display='none';return;}
  $('#preview-card').style.display='flex';
  $('#preview-title').textContent='Carregando...';
  $('#preview-meta').textContent='';
  $('#preview-thumb').src='';
  try{
    const r=await fetch('/api/info',{method:'POST',
      headers:{'Content-Type':'application/json'},
      body:JSON.stringify({url})});
    const d=await r.json();
    if(d.error){$('#preview-card').style.display='none';return;}
    $('#preview-thumb').src=d.thumbnail||'';
    $('#preview-title').textContent=d.title||'';
    if(d.is_playlist){
      $('#preview-meta').textContent=`Playlist - ${d.count} video(s)`;
      $('#preview-meta').className='preview-meta pl';
      $('#preview-card').className='preview-card playlist';
      $('#playlist-toggle').checked=true;
    }else{
      const mins=d.duration?Math.floor(d.duration/60)+':'+String(Math.floor(d.duration%60)).padStart(2,'0'):'';
      $('#preview-meta').textContent=mins?`Duracao ${mins}`:'';
      $('#preview-meta').className='preview-meta';
      $('#preview-card').className='preview-card';
    }
  }catch(e){$('#preview-card').style.display='none';}
}

// ---------- Download queue ----------
const activeTasks={};
let pollTimer=null;

$('#btn-download').addEventListener('click',async()=>{
  const url=$('#url-input').value.trim();
  if(!url){$('#url-input').classList.add('error');$('#url-input').focus();return;}
  $  ('#url-input').classList.remove('error');
  const type=$('#type-select').value;
  const quality=$('#quality-select').value;
  const audio_bitrate=$('#bitrate-select').value;
  const playlist=$('#playlist-toggle').checked;
  try{
    const r=await fetch('/api/download',{method:'POST',
      headers:{'Content-Type':'application/json'},
      body:JSON.stringify({url,type,quality,playlist,audio_bitrate})});
    const d=await r.json();
    if(d.error)throw new Error(d.error);
    addQueueItem(d.task_id,type,$('#preview-title').textContent||'Preparando...');
    $('#url-input').value='';
    $('#preview-card').style.display='none';
    $('#playlist-toggle').checked=false;
    ensurePolling();
  }catch(e){
    alert('Erro ao iniciar download: '+e.message);
  }
});

function addQueueItem(taskId,type,title){
  const el=document.createElement('div');
  el.className='queue-item'+(type==='mp3'?' audio':'');
  el.dataset.taskId=taskId;
  el.innerHTML=`
    <div class="queue-head">
      <div class="queue-title">${escHtml(title)}</div>
      <div class="queue-actions">
        <span class="queue-type-badge">${type.toUpperCase()}</span>
        <button class="q-btn cancel-btn" title="Cancelar">&#x2715;</button>
      </div>
    </div>
    <div class="queue-progress-row">
      <div class="queue-bar-bg"><div class="queue-bar"></div></div>
      <span class="queue-pct">0%</span>
    </div>
    <div class="queue-status">Iniciando...</div>`;
  el.querySelector('.cancel-btn').addEventListener('click',async()=>{
    await fetch('/api/cancel/'+taskId,{method:'POST'});
  });
  $('#queue').prepend(el);
  activeTasks[taskId]=el;
  updateActiveCount();
}

function ensurePolling(){
  if(pollTimer)return;
  pollTimer=setInterval(async()=>{
    const ids=Object.keys(activeTasks);
    if(!ids.length){clearInterval(pollTimer);pollTimer=null;return;}
    try{
      const r=await fetch('/api/status');
      const d=await r.json();
      ids.forEach(id=>{
        const t=d.tasks[id];
        const el=activeTasks[id];
        if(!t||!el)return;
        if(t.title)el.querySelector('.queue-title').textContent=t.title;
        const bar=el.querySelector('.queue-bar');
        const pct=el.querySelector('.queue-pct');
        const status=el.querySelector('.queue-status');
        const p=t.progress||0;
        bar.style.width=p+'%';
        pct.textContent=p+'%';
        if(t.status==='downloading'){
          status.textContent='Baixando '+p+'%';status.className='queue-status';
          bar.classList.remove('idle');
        }else if(t.status==='processing'){
          status.textContent='Processando (convertendo)...';status.className='queue-status';
        }else if(t.status==='done'){
          status.textContent='Concluido';status.className='queue-status done';
          bar.classList.add('idle');el.classList.add('done');finishItem(id);
        }else if(t.status==='error'){
          status.textContent='Erro: '+t.error;status.className='queue-status error';
          bar.classList.add('idle');el.classList.add('error');finishItem(id);
        }else if(t.status==='cancelled'){
          status.textContent='Cancelado';status.className='queue-status error';
          bar.classList.add('idle');el.classList.add('cancelled');finishItem(id);
        }
      });
    }catch(e){}
  },800);
}

function finishItem(id){
  const el=activeTasks[id];
  if(!el)return;
  delete activeTasks[id];
  const actions=el.querySelector('.queue-actions');
  const badge=actions.querySelector('.queue-type-badge');
  actions.innerHTML='';
  if(badge)actions.appendChild(badge);
  const rmBtn=document.createElement('button');
  rmBtn.className='q-btn remove';rmBtn.title='Remover';rmBtn.innerHTML='&#x2715;';
  rmBtn.addEventListener('click',()=>el.remove());
  actions.appendChild(rmBtn);
  updateActiveCount();
}

function updateActiveCount(){
  const n=Object.keys(activeTasks).length;
  $('#active-count').textContent=n||'';
}

// ---------- Files ----------
let allFiles=[];
$('#files-search').addEventListener('input',renderFiles);
$('#files-sort').addEventListener('change',renderFiles);

async function loadFiles(){
  $('#files-container').innerHTML='<div class="spinner"></div>';
  try{
    const r=await fetch('/api/files');
    allFiles=await r.json();
    renderFiles();
    $('#file-count-nav').textContent=allFiles.length||'';
  }catch(e){
    $('#files-container').innerHTML=`<div class="empty"><svg viewBox="0 0 24 24"><circle cx="12" cy="12" r="10"/><line x1="12" y1="8" x2="12" y2="12"/><line x1="12" y1="16" x2="12.01" y2="16"/></svg><p>Erro ao carregar arquivos</p></div>`;
  }
}

function renderFiles(){
  const q=$('#files-search').value.trim().toLowerCase();
  const sort=$('#files-sort').value;
  let files=allFiles.filter(f=>f.name.toLowerCase().includes(q));
  if(sort==='name')files=[...files].sort((a,b)=>a.name.localeCompare(b.name));
  else if(sort==='size')files=[...files].sort((a,b)=>b.size_mb-a.size_mb);
  else files=[...files].sort((a,b)=>b.mtime-a.mtime);

  const total=allFiles.reduce((s,f)=>s+f.size_mb,0);
  $('#files-stats').textContent=allFiles.length
    ?`${allFiles.length} arquivo(s)  |  ${total>=1024?(total/1024).toFixed(2)+' GB':total.toFixed(1)+' MB'} total`
    :'';

  if(!allFiles.length){
    $('#files-container').innerHTML=`<div class="empty">
      <svg viewBox="0 0 24 24"><path d="M22 19a2 2 0 01-2 2H4a2 2 0 01-2-2V5a2 2 0 012-2h5l2 3h9a2 2 0 012 2z"/></svg>
      <p>Nenhum arquivo baixado ainda</p></div>`;
    return;
  }
  if(!files.length){
    $('#files-container').innerHTML=`<div class="empty">
      <svg viewBox="0 0 24 24"><circle cx="11" cy="11" r="8"/><line x1="21" y1="21" x2="16.65" y2="16.65"/></svg>
      <p>Nenhum resultado para "${escHtml(q)}"</p></div>`;
    return;
  }

  const c=$('#files-container');
  c.innerHTML='<div class="file-grid"></div>';
  const grid=c.querySelector('.file-grid');
  files.forEach(f=>{
    const card=document.createElement('div');
    card.className='file-card '+f.type;
    const thumbHtml=f.has_thumb
      ?`<img class="file-thumb" src="/api/thumbnail/${encodeURIComponent(f.name)}" alt="" loading="lazy">`
      :`<div class="file-thumb-ph ${f.type}">${f.type==='video'
        ?'<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"><polygon points="23 7 16 12 23 17 23 7"/><rect x="1" y="5" width="15" height="14" rx="2" ry="2"/></svg>'
        :'<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"><path d="M9 18V5l12-2v13"/><circle cx="6" cy="18" r="3"/><circle cx="18" cy="16" r="3"/></svg>'
      }</div>`;
    card.innerHTML=`
      ${thumbHtml}
      <div class="file-info">
        <div class="file-name" title="${escHtml(f.name)}">${escHtml(f.name)}</div>
        <div class="file-meta">${f.size}  |  ${f.date}</div>
      </div>
      <div class="file-btns">
        <button class="file-btn play" title="Reproduzir" data-play="${escHtml(f.name)}" data-type="${f.type}">
          <svg viewBox="0 0 24 24"><polygon points="5 3 19 12 5 21 5 3"/></svg>
        </button>
        <button class="file-btn dl" title="Baixar" data-dl="${escHtml(f.name)}">
          <svg viewBox="0 0 24 24"><polyline points="8 17 12 21 16 17"/><line x1="12" y1="12" x2="12" y2="21"/><path d="M20.88 18.09A5 5 0 0018 9h-1.26A8 8 0 103 16.29"/></svg>
        </button>
        <button class="file-btn del" title="Excluir" data-del="${escHtml(f.name)}">
          <svg viewBox="0 0 24 24"><polyline points="3 6 5 6 21 6"/><path d="M19 6l-1 14a2 2 0 01-2 2H8a2 2 0 01-2-2L5 6"/><path d="M10 11v6"/><path d="M14 11v6"/><path d="M9 6V4a1 1 0 011-1h4a1 1 0 011 1v2"/></svg>
        </button>
      </div>`;
    card.querySelector('[data-play]').addEventListener('click',()=>openPlayer(f.name,f.type));
    card.querySelector('[data-dl]').addEventListener('click',()=>{
      const a=document.createElement('a');
      a.href='/api/file/'+encodeURIComponent(f.name);
      a.download=f.name;a.click();
    });
    card.querySelector('[data-del]').addEventListener('click',async()=>{
      if(!confirm('Excluir "'+f.name+'"?'))return;
      await fetch('/api/delete/'+encodeURIComponent(f.name),{method:'DELETE'});
      loadFiles();
    });
    grid.appendChild(card);
  });
}

function openPlayer(name,type){
  const mc=$('#modal-content');
  mc.innerHTML=type==='video'
    ?`<video controls autoplay playsinline src="/api/file/${encodeURIComponent(name)}"></video>`
    :`<audio controls autoplay src="/api/file/${encodeURIComponent(name)}"></audio>`;
  $('#modal-title').textContent=name;
  $('#modal').classList.add('show');
}

$('#modal-close').addEventListener('click',closeModal);
$('#modal').addEventListener('click',e=>{if(e.target===$('#modal'))closeModal();});
function closeModal(){
  $('#modal').classList.remove('show');
  const v=$('#modal-content video')||$('#modal-content audio');
  if(v)v.pause();
  $('#modal-content').innerHTML='';
}
document.addEventListener('keydown',e=>{if(e.key==='Escape')closeModal();});

function escHtml(s){
  return String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;');
}
</script>
</body>
</html>
"""

if __name__ == "__main__":
    print(f"[YT Downloader] pasta de midia: {DOWNLOAD_DIR}")
    app.run(host="0.0.0.0", port=5000, debug=False)