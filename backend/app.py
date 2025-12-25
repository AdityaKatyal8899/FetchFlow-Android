from flask import Flask, request, jsonify, send_from_directory
from flask_cors import CORS
import yt_dlp
import os
import threading
import uuid
import time
import subprocess
import re

app = Flask(__name__)
CORS(app)

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
TEMP_DIR = os.path.join(BASE_DIR, "jobs")
MERGED_DIR = os.path.join(BASE_DIR, "merged")
COOKIES_FILE = os.path.join(BASE_DIR, "cookies.txt")

os.makedirs(TEMP_DIR, exist_ok=True)
os.makedirs(MERGED_DIR, exist_ok=True)

FILE_TTL = 180
CLEANUP_INTERVAL = 60

jobs = {}

YTDLP_BASE_OPTS = {
    "quiet": True,
    "cookiefile": COOKIES_FILE,
    "nocheckcertificate": True,
}

def safe_filename(name: str):
    name = re.sub(r'[\\/*?:"<>|]', "", name)
    return name.strip()

@app.route("/extract", methods=["POST"])
def extract_info():
    url = request.json.get("url")
    if not url:
        return jsonify({"status": "error"}), 400

    with yt_dlp.YoutubeDL({**YTDLP_BASE_OPTS, "skip_download": True}) as ydl:
        info = ydl.extract_info(url, download=False)

    return jsonify({
        "status": "ok",
        "title": info.get("title"),
        "thumbnail": info.get("thumbnail"),
        "duration": info.get("duration"),
        "uploader": info.get("uploader"),
        "formats": [
            {
                "height": f.get("height"),
                "filesize": f.get("filesize") or f.get("filesize_approx"),
                "has_video": f.get("vcodec") != "none",
                "has_audio": f.get("acodec") != "none",
            }
            for f in info.get("formats", [])
        ]
    })

@app.route("/download", methods=["POST"])
def download_media():
    data = request.json
    url = data.get("url")
    dtype = data.get("type", "both")
    quality = data.get("quality", 1080)

    if not url:
        return jsonify({"status": "error"}), 400

    job_id = str(uuid.uuid4())
    job_dir = os.path.join(TEMP_DIR, job_id)
    os.makedirs(job_dir, exist_ok=True)

    jobs[job_id] = {
        "status": "downloading",
        "filename": None,
        "size": None,
        "created_at": None,
        "error": None
    }

    def worker():
        try:
            with yt_dlp.YoutubeDL(YTDLP_BASE_OPTS) as ydl:
                info = ydl.extract_info(url, download=False)

            title = safe_filename(info.get("title", job_id))

            # -------- AUDIO ONLY (MP3) --------
            if dtype == "audio":
                out_path = os.path.join(MERGED_DIR, f"{title}.mp3")

                yt_dlp.YoutubeDL({
                    **YTDLP_BASE_OPTS,
                    "format": "bestaudio/best",
                    "outtmpl": os.path.join(job_dir, "audio.%(ext)s"),
                    "writethumbnail": True,
                    "postprocessors": [
                        {
                            "key": "FFmpegExtractAudio",
                            "preferredcodec": "mp3",
                            "preferredquality": "192",
                        },
                        {
                            "key": "FFmpegThumbnailsConvertor",
                            "format": "jpg",
                        },
                        {"key": "EmbedThumbnail"},
                        {"key": "FFmpegMetadata"},
                    ],
                }).download([url])

                audio_file = next(
                    os.path.join(job_dir, f)
                    for f in os.listdir(job_dir)
                    if f.endswith(".mp3")
                )

                os.rename(audio_file, out_path)
                filename = os.path.basename(out_path)

            # -------- VIDEO ONLY --------
            elif dtype == "video":
                out_path = os.path.join(MERGED_DIR, f"{title}.mp4")

                yt_dlp.YoutubeDL({
                    **YTDLP_BASE_OPTS,
                    "format": f"bestvideo[height<={quality}]/bestvideo",
                    "outtmpl": out_path,
                }).download([url])

                filename = os.path.basename(out_path)

            # -------- VIDEO + AUDIO (MERGED BY YT-DLP) --------
            else:
                out_path = os.path.join(MERGED_DIR, f"{title}.mp4")

                yt_dlp.YoutubeDL({
                    **YTDLP_BASE_OPTS,
                    "format": f"bestvideo[height<={quality}]+bestaudio/best",
                    "outtmpl": out_path,
                    "merge_output_format": "mp4",
                }).download([url])

                filename = os.path.basename(out_path)

            filepath = os.path.join(MERGED_DIR, filename)

            jobs[job_id]["status"] = "done"
            jobs[job_id]["filename"] = filename
            jobs[job_id]["size"] = os.path.getsize(filepath)
            jobs[job_id]["created_at"] = time.time()

        except Exception as e:
            jobs[job_id]["status"] = "error"
            jobs[job_id]["error"] = str(e)

    threading.Thread(target=worker, daemon=True).start()
    return jsonify({"status": "ok", "job_id": job_id})

@app.route("/job/<job_id>")
def job_status(job_id):
    job = jobs.get(job_id)
    if not job:
        return jsonify({"status": "error"}), 404
    return jsonify(job)

@app.route("/files/<filename>")
def serve_file(filename):
    for job in jobs.values():
        if job["status"] == "done" and job["filename"] == filename:
            return send_from_directory(MERGED_DIR, filename, as_attachment=True)
    return jsonify({"status": "expired"}), 404

def cleanup_worker():
    while True:
        now = time.time()
        for job_id, job in list(jobs.items()):
            if job.get("created_at") and now - job["created_at"] > FILE_TTL:
                if job["filename"]:
                    path = os.path.join(MERGED_DIR, job["filename"])
                    if os.path.exists(path):
                        os.remove(path)

                temp_path = os.path.join(TEMP_DIR, job_id)
                if os.path.exists(temp_path):
                    for f in os.listdir(temp_path):
                        os.remove(os.path.join(temp_path, f))
                    os.rmdir(temp_path)

                jobs.pop(job_id)
        time.sleep(CLEANUP_INTERVAL)

@app.route("/health", methods=["GET"])
def health_check():
    return jsonify({"status": "ok", "uptime": time.time()})

if __name__ == "__main__":
    threading.Thread(target=cleanup_worker, daemon=True).start()
    app.run(host="0.0.0.0", port=8000, debug=True)
