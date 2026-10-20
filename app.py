import streamlit as st
import subprocess
import os
import json
import shutil
import time
import uuid
import math
import random
import logging
import threading
from pathlib import Path
from datetime import datetime, timezone

# صفحہ کی سیٹنگز
st.set_page_config(
    page_title="Video Re-Encoder & Creative Reframer (V4)",
    page_icon="🎬",
    layout="centered"
)

# لاگنگ سسٹم
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s"
)
logger = logging.getLogger("VideoReframerV4")

# آؤٹ پٹ ڈائریکٹری اور فائل منیجمنٹ
OUTPUT_DIR = Path("processed_outputs")
OUTPUT_DIR.mkdir(exist_ok=True)
ACTIVE_JOBS = set()
JOBS_LOCK = threading.Lock()

MAX_DURATION_SEC = 90.0
MAX_FILE_SIZE_MB = 150.0
MAX_DIMENSION = 3840
FFPROBE_TIMEOUT_SEC = 15
FFMPEG_TIMEOUT_SEC = 150
CLEANUP_MAX_AGE_SEC = 1800

# ایگزیکیوٹیبل چیک
def verify_binaries():
    if not shutil.which("ffmpeg") or not shutil.which("ffprobe"):
        logger.critical("FFmpeg or FFprobe binary is missing!")
        st.error("سرور پر FFmpeg یا FFprobe انسٹال نہیں ہے۔")
        st.stop()

verify_binaries()

# بیک گراؤنڈ کلین اپ
def cleanup_old_files():
    now = time.time()
    for file_path in OUTPUT_DIR.glob("job_*.mp4"):
        try:
            with JOBS_LOCK:
                if file_path.name in ACTIVE_JOBS:
                    continue
            if file_path.is_file() and (now - file_path.stat().st_mtime) > CLEANUP_MAX_AGE_SEC:
                file_path.unlink()
        except Exception as e:
            logger.warning(f"Failed to delete old file: {e}")

def start_background_cleaner():
    def cleaner_loop():
        while True:
            time.sleep(300)
            cleanup_old_files()
    t = threading.Thread(target=cleaner_loop, daemon=True)
    t.start()

start_background_cleaner()

# میڈیا انسپکشن اور ویلیڈیشن
def inspect_media_file(file_path: str) -> dict:
    if not os.path.exists(file_path):
        raise Exception("اپلوڈ کردہ فائل سرور پر نہیں ملی۔")
    
    file_size_mb = os.path.getsize(file_path) / (1024 * 1024)
    if file_size_mb <= 0:
        raise Exception("فائل خالی ہے (0 بائٹس)۔")
    if file_size_mb > MAX_FILE_SIZE_MB:
        raise Exception(f"فائل کا سائز حد سے زیادہ ہے۔ زیادہ سے زیادہ حد {int(MAX_FILE_SIZE_MB)}MB ہے۔")

    cmd = ["ffprobe", "-v", "error", "-show_format", "-show_streams", "-of", "json", file_path]
    try:
        res = subprocess.run(cmd, capture_output=True, text=True, timeout=FFPROBE_TIMEOUT_SEC)
    except subprocess.TimeoutExpired:
        raise Exception("ویڈیو فائل چیک کرنے میں بہت زیادہ وقت لگ رہا ہے۔")

    if res.returncode != 0:
        raise Exception("ویڈیو فائل پڑھنے میں ناکامی۔ براہ کرم درست MP4/MOV فائل اپلوڈ کریں۔")

    data = json.loads(res.stdout)
    streams = data.get("streams", [])
    fmt = data.get("format", {})

    video_stream = None
    for s in streams:
        if s.get("codec_type") == "video":
            if s.get("disposition", {}).get("attached_pic", 0) == 0:
                video_stream = s
                break

    if not video_stream:
        raise Exception("اس فائل میں کوئی حقیقی ویڈیو اسٹریم موجود نہیں ہے۔")

    width = int(video_stream.get("width", 0))
    height = int(video_stream.get("height", 0))
    if width <= 0 or height <= 0:
        raise Exception("ویڈیو کی ریزولوشن درست نہیں ہے۔")

    raw_duration = fmt.get("duration") or video_stream.get("duration")
    try:
        duration = float(raw_duration)
    except (TypeError, ValueError):
        raise Exception("ویڈیو کا دورانیہ معلوم نہیں ہو سکا۔")

    if not math.isfinite(duration) or duration <= 0.1:
        raise Exception("ویڈیو کا دورانیہ غیر معمولی یا صفر ہے۔")
    if duration > MAX_DURATION_SEC:
        raise Exception(f"یہ ویڈیو {duration:.1f} سیکنڈ کی ہے۔ زیادہ سے زیادہ حد {int(MAX_DURATION_SEC)} سیکنڈ ہے۔")

    audio_stream = next((s for s in streams if s.get("codec_type") == "audio"), None)
    has_audio = audio_stream is not None

    return {
        "duration": duration,
        "width": width,
        "height": height,
        "v_index": video_stream.get("index", 0),
        "has_audio": has_audio,
        "a_index": audio_stream.get("index") if has_audio else None
    }

def verify_output_file(out_path: str, expected_audio: bool):
    if not os.path.exists(out_path) or os.path.getsize(out_path) == 0:
        raise Exception("پراسیسنگ کے بعد آؤٹ پٹ فائل تیار نہیں ہو سکی۔")
    
    cmd = ["ffprobe", "-v", "error", "-show_streams", "-of", "json", out_path]
    try:
        res = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
    except subprocess.TimeoutExpired:
        raise Exception("تیار شدہ فائل کی تصدیق میں زیادہ وقت لگ رہا ہے۔")

    streams = json.loads(res.stdout).get("streams", [])
    types = [s.get("codec_type") for s in streams]
    if "video" not in types:
        raise Exception("آؤٹ پٹ فائل میں ویڈیو ٹریک غائب ہے۔")

# مین پراسیسنگ انجن
def run_advanced_reframe(input_video_path, flip_video, process_mode):
    info = inspect_media_file(input_video_path)

    target_w = (info["width"] // 2) * 2
    target_h = (info["height"] // 2) * 2

    job_filename = f"job_{uuid.uuid4().hex}.mp4"
    output_path = OUTPUT_DIR / job_filename

    with JOBS_LOCK:
        ACTIVE_JOBS.add(job_filename)

    try:
        now_str = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

        if process_mode == "Standard Clean (صرف میٹا ڈیٹا اور فارمیٹ کلین اپ)":
            vf_filters = [
                "setpts=PTS-STARTPTS",
                f"scale={target_w}:{target_h}:flags=bicubic"
            ]
            if flip_video:
                vf_filters.append("hflip")
            af_filters = ["asetpts=PTS-STARTPTS"] if info["has_audio"] else []
        else:
            rand_speed = round(random.uniform(1.03, 1.05), 3)
            rand_sat = round(random.uniform(1.02, 1.05), 2)
            rand_contrast = round(random.uniform(1.01, 1.03), 2)
            rand_gamma = round(random.uniform(0.98, 1.02), 2)
            p_shift = random.randint(3, 6)

            vf_filters = [
                f"setpts=(PTS-STARTPTS)/{rand_speed}",
                "crop=trunc(iw*0.95/2)*2:trunc(ih*0.95/2)*2:(iw-ow)/2+sin(t*1.8)*3:(ih-oh)/2+cos(t*1.4)*3",
                f"perspective=x0={p_shift}:y0=0:x1=W-{p_shift}:y1={p_shift}:x2=0:y2=H-{p_shift}:x3=W:y3=H:sense=source",
                f"scale={target_w}:{target_h}:flags=bicubic",
                f"eq=brightness=0.006:contrast={rand_contrast}:saturation={rand_sat}:gamma={rand_gamma}",
                "colorbalance=rs=0.015:gs=-0.01:bs=0.02",
                "vignette=PI/8",
                "unsharp=3:3:0.35:3:3:0.0"
            ]
            if flip_video:
                vf_filters.append("hflip")

            if info["has_audio"]:
                pitch_shift = round(random.uniform(1.006, 1.010), 4)
                af_filters = [
                    "asetpts=PTS-STARTPTS",
                    "aresample=44100",
                    f"asetrate={round(44100 * pitch_shift)}",
                    "aresample=44100",
                    f"atempo={rand_speed / pitch_shift:.8f}",
                    "equalizer=f=400:width_type=h:width=150:g=0.8",
                    "extrastereo=m=1.05"
                ]
            else:
                af_filters = []

        vf_string = ",".join(vf_filters)

        command = [
            "ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin", "-y",
            "-i", input_video_path,
            "-map", f"0:{info['v_index']}",
            "-vf", vf_string
        ]

        if info["has_audio"]:
            command.extend([
                "-map", f"0:{info['a_index']}",
                "-af", ",".join(af_filters),
                "-c:a", "aac",
                "-b:a", "192k"
            ])
        else:
            command.append("-an")

        command.extend([
            "-sn", "-dn",
            "-map_metadata", "-1",
            "-map_chapters", "-1",
            "-metadata", f"creation_time={now_str}",
            "-c:v", "libx264",
            "-preset", "faster",
            "-crf", "20",
            "-g", "50",
            "-threads", "2",
            "-pix_fmt", "yuv420p",
            "-movflags", "+faststart",
            str(output_path)
        ])

        timeout_budget = max(FFMPEG_TIMEOUT_SEC, int(info["duration"] * 3.5))
        res = subprocess.run(command, capture_output=True, text=True, timeout=timeout_budget)

        if res.returncode != 0:
            if output_path.exists():
                output_path.unlink(missing_ok=True)
            raise Exception("ویڈیو انکوڈنگ کے دوران تکنیکی مسئلہ پیش آیا۔")

        verify_output_file(str(output_path), info["has_audio"])
        return str(output_path)

    except Exception as e:
        if output_path.exists():
            output_path.unlink(missing_ok=True)
        raise e
    finally:
        with JOBS_LOCK:
            ACTIVE_JOBS.discard(job_filename)

# --- سٹریم لِٹ (Streamlit) یوزر انٹرفیس ---
st.title("🎬 Video Re-Encoder & Creative Reframer (V4)")
st.markdown(
    "> **نوٹ:** یہ ٹول ویڈیو کو دوبارہ encode کرتا ہے اور آپ کے منتخب کردہ ٹرانسفارمیشنز لاگو کرتا ہے۔ "
    "اس عمل سے تصویر (کروپ/رنگ) اور آواز میں معمولی تبدیلی آ سکتی ہے۔ اپنی اصل فائل ہمیشہ محفوظ رکھیں۔"
)

uploaded_file = st.file_uploader("اپنی ویڈیو اپلوڈ کریں (حد: 90 سیکنڈ، 150MB)", type=["mp4", "mov", "avi", "mkv"])

process_mode = st.radio(
    "پراسیسنگ موڈ منتخب کریں",
    [
        "Creative Transform (Moving Crop, Perspective, Speed & Audio Shift)",
        "Standard Clean (صرف میٹا ڈیٹا اور فارمیٹ کلین اپ)"
    ]
)

flip_check = st.checkbox("Horizontal Flip (اختیاری: ویڈیو کو دائیں سے بائیں الٹیں)")

if st.button("🚀 ویڈیو پراسیس کریں", type="primary"):
    if uploaded_file is not None:
        # عارضی ان پٹ فائل محفوظ کریں
        temp_input_path = OUTPUT_DIR / f"input_{uuid.uuid4().hex}.mp4"
        with open(temp_input_path, "wb") as f:
            f.write(uploaded_file.getbuffer())

        try:
            with st.spinner("ویڈیو پراسیس ہو رہی ہے، براہ کرم انتظار کریں..."):
                output_file_path = run_advanced_reframe(str(temp_input_path), flip_check, process_mode)
            
            st.success("ویڈیو کامیابی سے تیار ہو گئی ہے!")
            with open(output_file_path, "rb") as f:
                st.video(f.read())
            
            # ڈاؤن لوڈ بٹن
            with open(output_file_path, "rb") as f:
                st.download_button(
                    label="📥 تیار شدہ ویڈیو ڈاؤن لوڈ کریں",
                    data=f,
                    file_name="processed_video.mp4",
                    mime="video/mp4"
                )
        except Exception as e:
            st.error(f"خرابی پیش آئی: {str(e)}")
        finally:
            if temp_input_path.exists():
                temp_input_path.unlink(missing_ok=True)
    else:
        st.warning("براہ کرم پہلے کوئی ویڈیو فائل اپلوڈ کریں۔")
