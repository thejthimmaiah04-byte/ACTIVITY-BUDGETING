#!/usr/bin/env python3
"""
RTSP capture agent for EthoGrid — third camera instance (port 8082, D-4 cam2).
Pulls frames from any WiFi camera (Tapo, Qubo, Hikvision, etc.) via RTSP,
saves lossless PNG stills to Google Drive, and serves MJPEG for the EthoGrid live stream.
No Raspberry Pi camera module required.
"""
import io, os, time, threading, subprocess, json
from datetime import datetime
from flask import Flask, Response, send_file
from PIL import Image, ImageOps, ImageFilter

# ── Config ────────────────────────────────────────────────────
ENCLOSURE_NAME  = "D-4"           # ← match enclosure name in EthoGrid app

# Tapo:  rtsp://[camera-account-user]:[camera-account-pass]@[camera-ip]:554/stream1
# Qubo:  rtsp://admin:[password]@[camera-ip]:554/Streaming/Channels/101
# Set CAMERA_RTSP to your camera's RTSP URL
CAMERA_RTSP     = "rtsp://USER:PASS@CAMERA_IP:554/stream1"
# ↑ Set this on the Pi to your camera's RTSP URL (keep out of git)

CAPTURE_DIR     = "/home/lenovo/captures3"
INTERVAL        = 300              # seconds between still captures (300 = 5 min)
GDRIVE_ROOT     = "1zT_6zGBlpo9FuukUZU6N_1dcVye-cSHS"
SNAPSHOT_LATEST = "/tmp/snap_latest_3.jpg"
APPS_SCRIPT_URL = "https://script.google.com/macros/s/AKfycbwzg8yZBWVOxzCeVJs2yLObmSVbck8VlqfQu24XeNbeHBYQUlMxoDQEWfG2_Ar-WEAFAA/exec"
RETRY_INTERVAL  = 300
PORT            = 8082
# ─────────────────────────────────────────────────────────────

os.makedirs(CAPTURE_DIR, exist_ok=True)
app = Flask(__name__)

# ── RTSP → MJPEG buffer ───────────────────────────────────────
class RTSPBuffer:
    """Single ffmpeg process pulling frames from RTSP. Reconnects automatically."""
    def __init__(self, rtsp_url):
        self.frame = None
        self.cond  = threading.Condition()
        threading.Thread(target=self._run, args=(rtsp_url,), daemon=True).start()

    def _run(self, url):
        while True:
            try:
                cmd = [
                    'ffmpeg', '-loglevel', 'error',
                    '-rtsp_transport', 'tcp',
                    '-i', url,
                    '-vf', 'fps=10',
                    '-f', 'image2pipe',
                    '-pix_fmt', 'yuvj420p',
                    '-vcodec', 'mjpeg',
                    '-q:v', '3',
                    'pipe:1'
                ]
                proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
                data = b''
                while True:
                    chunk = proc.stdout.read(8192)
                    if not chunk:
                        break
                    data += chunk
                    while True:
                        s = data.find(b'\xff\xd8')
                        e = data.find(b'\xff\xd9', s + 2) if s != -1 else -1
                        if s == -1 or e == -1:
                            break
                        frame = data[s:e + 2]
                        data  = data[e + 2:]
                        with self.cond:
                            self.frame = frame
                            self.cond.notify_all()
                proc.kill()
            except Exception as e:
                print(f"[RTSP] stream error: {e}")
            time.sleep(5)

class ScaledStreamBuffer:
    """Derives a lower-resolution MJPEG stream from the main RTSP buffer."""
    def __init__(self, source, size, quality):
        self.frame = None
        self.cond  = threading.Condition()
        threading.Thread(target=self._run, args=(source, size, quality), daemon=True).start()

    def _run(self, source, size, quality):
        while True:
            with source.cond:
                source.cond.wait()
                raw = source.frame
            if raw is None:
                continue
            try:
                img = Image.open(io.BytesIO(raw))
                img.thumbnail(size, Image.LANCZOS)
                out = io.BytesIO()
                img.save(out, 'JPEG', quality=quality)
                with self.cond:
                    self.frame = out.getvalue()
                    self.cond.notify_all()
            except Exception:
                pass

buf      = RTSPBuffer(CAMERA_RTSP)
buf_med  = ScaledStreamBuffer(buf, (1280, 720), quality=65)
buf_low  = ScaledStreamBuffer(buf, (640,  360), quality=40)

# ── Network check ─────────────────────────────────────────────
def has_internet():
    try:
        r = subprocess.run(
            ["ping", "-c", "1", "-W", "3", "8.8.8.8"],
            capture_output=True, timeout=5
        )
        return r.returncode == 0
    except Exception:
        return False

# ── Drive upload helpers ───────────────────────────────────────
def get_file_id(remote_folder, filename):
    try:
        result = subprocess.run([
            "rclone", "lsjson", f"gdrive:{remote_folder}",
            "--drive-root-folder-id", GDRIVE_ROOT, "--files-only"
        ], capture_output=True, text=True, timeout=30)
        for f in json.loads(result.stdout):
            if f.get("Name") == filename:
                return f.get("ID", "")
    except Exception:
        pass
    return ""

def log_to_sheet(timestamp_str, filename, file_id):
    try:
        file_url = f"https://drive.google.com/file/d/{file_id}/view" if file_id else ""
        payload  = json.dumps({
            "timestamp": timestamp_str,
            "filename":  filename,
            "file_url":  file_url,
            "enclosure": ENCLOSURE_NAME,
        })
        subprocess.run([
            "curl", "-s", "-L",
            "-H", "Content-Type: application/json",
            "-d", payload, APPS_SCRIPT_URL
        ], timeout=30)
    except Exception as e:
        print(f"  Sheet log failed: {e}")

def upload_file(local_path, drive_folder, filename, timestamp_str):
    try:
        r = subprocess.run([
            "rclone", "copy", local_path,
            f"gdrive:{drive_folder}",
            "--drive-root-folder-id", GDRIVE_ROOT,
            "--no-update-modtime",
            "--drive-chunk-size", "64M",
            "--buffer-size",      "128M",
        ], timeout=180)
        if r.returncode != 0:
            return False
        print(f"  Uploaded: {filename}")
        file_id = get_file_id(drive_folder, filename)
        log_to_sheet(timestamp_str, filename, file_id)
        os.remove(local_path)
        local_dir = os.path.dirname(local_path)
        if not os.listdir(local_dir):
            os.rmdir(local_dir)
        return True
    except Exception as e:
        print(f"  Upload failed: {e}")
        return False

# ── Retry loop ─────────────────────────────────────────────────
def retry_loop():
    while True:
        time.sleep(RETRY_INTERVAL)
        if not has_internet():
            continue
        for date_folder in sorted(os.listdir(CAPTURE_DIR)):
            day_dir = os.path.join(CAPTURE_DIR, date_folder)
            if not os.path.isdir(day_dir):
                continue
            for filename in sorted(os.listdir(day_dir)):
                if not filename.endswith(".png"):
                    continue
                local_path    = os.path.join(day_dir, filename)
                drive_folder  = f"images/{ENCLOSURE_NAME}/{date_folder}"
                try:
                    rest          = filename[len(ENCLOSURE_NAME) + 1:]
                    time_part     = rest[:5].replace("-", ":")
                    timestamp_str = f"{date_folder} {time_part}"
                except Exception:
                    timestamp_str = date_folder
                print(f"  Retrying: {filename}")
                upload_file(local_path, drive_folder, filename, timestamp_str)

threading.Thread(target=retry_loop, daemon=True).start()

# ── Capture loop ───────────────────────────────────────────────
def capture_loop():
    while True:
        time.sleep(INTERVAL)
        now           = datetime.now()
        date_str      = now.strftime("%Y-%m-%d")
        time_str      = now.strftime("%H-%M")
        image_name    = f"{ENCLOSURE_NAME}-{time_str}-{date_str}.png"
        timestamp_str = now.strftime("%Y-%m-%d %H:%M")
        drive_folder  = f"images/{ENCLOSURE_NAME}/{date_str}"
        local_day_dir = os.path.join(CAPTURE_DIR, date_str)
        os.makedirs(local_day_dir, exist_ok=True)
        local_path    = os.path.join(local_day_dir, image_name)
        tmp_jpg       = local_path.replace(".png", "_raw.jpg")

        try:
            r = subprocess.run([
                "ffmpeg", "-y", "-loglevel", "error",
                "-rtsp_transport", "tcp",
                "-i", CAMERA_RTSP,
                "-frames:v", "1",
                "-q:v", "1",
                tmp_jpg
            ], timeout=30)

            if r.returncode != 0 or not os.path.exists(tmp_jpg):
                print(f"[{time_str}] Frame capture failed")
                continue

            img = Image.open(tmp_jpg).convert('L')  # greyscale
            img.save(local_path, "PNG", compress_level=6, optimize=True)
            os.remove(tmp_jpg)

            img.thumbnail((1280, 720), Image.LANCZOS)
            img.save(SNAPSHOT_LATEST, "JPEG", quality=88)
            print(f"[{time_str}] Saved: {image_name} ({img.width}x{img.height})")

            if has_internet():
                threading.Thread(
                    target=upload_file,
                    args=(local_path, drive_folder, image_name, timestamp_str),
                    daemon=True
                ).start()
            else:
                print(f"  No internet — {image_name} queued for later upload")

        except Exception as e:
            print(f"[{time_str}] Capture error: {e}")
            if os.path.exists(tmp_jpg):
                os.remove(tmp_jpg)

threading.Thread(target=capture_loop, daemon=True).start()

# ── Flask routes ───────────────────────────────────────────────
def frames_from(buf_source):
    while True:
        with buf_source.cond:
            buf_source.cond.wait()
            frame = buf_source.frame
        if frame:
            yield (b'--frame\r\nContent-Type: image/jpeg\r\n\r\n' + frame + b'\r\n')

@app.route('/stream')
def stream():
    return Response(frames_from(buf), mimetype='multipart/x-mixed-replace; boundary=frame')

@app.route('/stream/medium')
def stream_medium():
    return Response(frames_from(buf_med), mimetype='multipart/x-mixed-replace; boundary=frame')

@app.route('/stream/low')
def stream_low():
    return Response(frames_from(buf_low), mimetype='multipart/x-mixed-replace; boundary=frame')

@app.route('/snapshot')
def snapshot():
    if os.path.exists(SNAPSHOT_LATEST):
        return send_file(SNAPSHOT_LATEST, mimetype='image/jpeg')
    blank = io.BytesIO()
    Image.new('RGB', (1280, 720), 30).save(blank, 'JPEG')
    blank.seek(0)
    return Response(blank.read(), mimetype='image/jpeg')

@app.route('/')
def index():
    return f"<h2>EthoGrid capture agent running</h2><p>Enclosure: {ENCLOSURE_NAME}</p><p><a href='/stream'>Live stream</a></p>"

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=PORT, threaded=True)
