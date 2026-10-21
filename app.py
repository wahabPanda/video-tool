# Video Re-Encoder & Creative Reframer (V4) — Final (Streamlit Edition)
# تمام 5 فکسز اور 14 پوائنٹس من و عن شامل ہیں
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

st.set_page_config(page_title="Video Re-Encoder & Editor V4", page_icon="🎬", layout="wide")

# 9. پراپر لاگنگ سسٹم (ڈویلپر کے لیے مکمل تفصیل، یوزر سے خفیہ)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s"
)
logger = logging.getLogger("VideoReframerV4")

# 8. مخصوص آؤٹ پٹ ڈائریکٹری اور فائل منیجمنٹ
OUTPUT_DIR = Path("processed_outputs")
OUTPUT_DIR.mkdir(exist_ok=True)
ACTIVE_JOBS = set()
JOBS_LOCK = threading.Lock()

# حدود (Server Resource Limits)
MAX_DURATION_SEC = 90.0
MAX_FILE_SIZE_MB = 150.0
MAX_DIMENSION = 3840  # 4K limit
FFPROBE_TIMEOUT_SEC = 15
FFMPEG_TIMEOUT_SEC = 150
CLEANUP_MAX_AGE_SEC = 1800  # 30 منٹ

# کسٹم ایرر کلاس (بالکل gr.Error کی طرح کام کرنے کے لیے)
class AppError(Exception):
    pass

# 5. سٹارٹ اپ پر ایگزیکیوٹیبل چیک
def verify_binaries():
    if not shutil.which("ffmpeg") or not shutil.which("ffprobe"):
        logger.critical("FFmpeg or FFprobe binary is missing on this server!")
        st.error("سرور پر FFmpeg یا FFprobe انسٹال نہیں ہے۔ (براہ کرم GitHub میں packages.txt فائل بنا کر اس میں ffmpeg لکھیں)")
        st.stop()

verify_binaries()

# 8. حقیقی بیک گراؤنڈ کلین اپ مکینزم (جو ہر 5 منٹ بعد خودکار چلتا ہے)
@st.cache_resource
def start_background_cleaner():
    def cleanup_old_files():
        now = time.time()
        for file_path in OUTPUT_DIR.glob("job_*.mp4"):
            try:
                with JOBS_LOCK:
                    if file_path.name in ACTIVE_JOBS:
                        continue
                if file_path.is_file() and (now - file_path.stat().st_mtime) > CLEANUP_MAX_AGE_SEC:
                    file_path.unlink()
                    logger.info(f"Cleaned up expired file: {file_path.name}")
            except Exception as e:
                logger.warning(f"Failed to delete old file {file_path.name}: {e}")

    def cleaner_loop():
        while True:
            time.sleep(300)
            cleanup_old_files()

    t = threading.Thread(target=cleaner_loop, daemon=True)
    t.start()
    return cleanup_old_files

cleanup_old_files = start_background_cleaner()

# 3 & 4. ایک ہی بار میں مکمل JSON Probe اور سخت Validation
def inspect_media_file(file_path: str) -> dict:
    if not os.path.exists(file_path):
        raise AppError("اپلوڈ کردہ فائل سرور پر نہیں ملی۔")

    file_size_mb = os.path.getsize(file_path) / (1024 * 1024)
    if file_size_mb <= 0:
        raise AppError("فائل خالی ہے (0 بائٹس)۔")
    if file_size_mb > MAX_FILE_SIZE_MB:
        raise AppError(f"فائل کا سائز {file_size_mb:.1f}MB ہے۔ زیادہ سے زیادہ حد {int(MAX_FILE_SIZE_MB)}MB ہے۔")

    cmd = [
        "ffprobe", "-v", "error",
        "-show_format", "-show_streams",
        "-of", "json", file_path
    ]

    try:
        res = subprocess.run(cmd, capture_output=True, text=True, timeout=FFPROBE_TIMEOUT_SEC)
    except subprocess.TimeoutExpired:
        logger.error("ffprobe timed out.")
        raise AppError("ویڈیو فائل چیک کرنے میں بہت زیادہ وقت لگ رہا ہے۔ فائل خراب ہو سکتی ہے۔")
    except FileNotFoundError:
        raise AppError("سرور پر ffprobe دستیاب نہیں ہے۔")

    if res.returncode != 0:
        logger.error(f"ffprobe failed (code {res.returncode}): {res.stderr}")
        raise AppError("ویڈیو فائل پڑھنے میں ناکامی۔ براہ کرم درست MP4/MOV فائل اپلوڈ کریں۔")

    try:
        data = json.loads(res.stdout)
    except json.JSONDecodeError as e:
        logger.error(f"Invalid JSON from ffprobe: {e}")
        raise AppError("ویڈیو کی اندرونی معلومات پڑھی نہیں جا سکیں۔")

    streams = data.get("streams", [])
    fmt = data.get("format", {})

    # حقیقی ویڈیو اسٹریم تلاش کریں (Attached cover art کو نظر انداز کریں)
    video_stream = None
    for s in streams:
        if s.get("codec_type") == "video":
            disposition = s.get("disposition", {})
            if disposition.get("attached_pic", 0) == 0:
                video_stream = s
                break

    if not video_stream:
        raise AppError("اس فائل میں کوئی حقیقی ویڈیو اسٹریم موجود نہیں ہے۔")

    width = int(video_stream.get("width", 0))
    height = int(video_stream.get("height", 0))
    if width <= 0 or height <= 0:
        raise AppError("ویڈیو کی چوڑائی یا لمبائی (Resolution) درست نہیں ہے۔")
    if width > MAX_DIMENSION or height > MAX_DIMENSION:
        raise AppError(f"ویڈیو ریزولوشن ({width}x{height}) حد سے زیادہ ہے۔ زیادہ سے زیادہ 4K سپورٹڈ ہے۔")

    # Duration نکالیں اور NaN/Infinity/Zero چیک کریں
    raw_duration = fmt.get("duration") or video_stream.get("duration")
    try:
        duration = float(raw_duration)
    except (TypeError, ValueError):
        raise AppError("ویڈیو کا دورانیہ (Duration) معلوم نہیں ہو سکا۔")

    if not math.isfinite(duration) or duration <= 0.1:
        raise AppError("ویڈیو کا دورانیہ غیر معمولی یا صفر ہے۔")
    if duration > MAX_DURATION_SEC:
        raise AppError(f"یہ ویڈیو {duration:.1f} سیکنڈ کی ہے۔ زیادہ سے زیادہ حد {int(MAX_DURATION_SEC)} سیکنڈ ہے۔")

    # آڈیو اسٹریم کی جانچ
    audio_stream = None
    for s in streams:
        if s.get("codec_type") == "audio":
            audio_stream = s
            break

    has_audio = audio_stream is not None

    # فکس #5: sample_rate key حذف کر دی گئی
    return {
        "duration": duration,
        "width": width,
        "height": height,
        "v_index": video_stream.get("index", 0),
        "has_audio": has_audio,
        "a_index": audio_stream.get("index") if has_audio else None
    }

# آؤٹ پٹ فائل کی تصدیق (صرف سائز نہیں بلکہ اسٹریم چیک)
def verify_output_file(out_path: str, expected_audio: bool):
    if not os.path.exists(out_path) or os.path.getsize(out_path) == 0:
        raise AppError("پراسیسنگ کے بعد آؤٹ پٹ فائل تیار نہیں ہو سکی۔")

    cmd = ["ffprobe", "-v", "error", "-show_streams", "-of", "json", out_path]
    # فکس #4: ffprobe کا TimeoutExpired بھی پکڑیں
    try:
        res = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
    except subprocess.TimeoutExpired:
        raise AppError("تیار شدہ فائل کی تصدیق میں زیادہ وقت لگ رہا ہے۔")
    if res.returncode != 0:
        raise AppError("تیار شدہ ویڈیو فائل درست فارمیٹ میں نہیں ہے۔")

    streams = json.loads(res.stdout).get("streams", [])
    types = [s.get("codec_type") for s in streams]
    if "video" not in types:
        raise AppError("آؤٹ پٹ فائل میں ویڈیو ٹریک غائب ہے۔")
    if expected_audio and "audio" not in types:
        raise AppError("آؤٹ پٹ فائل میں آڈیو ٹریک محفوظ نہیں ہو سکا۔")

# مین پراسیسنگ انجن
def run_advanced_reframe(input_video, flip_video, process_mode):
    if input_video is None:
        raise AppError("براہ کرم پہلے ویڈیو فائل اپلوڈ کریں۔")

    cleanup_old_files()
    info = inspect_media_file(input_video)

    # 11. اصل ریزولوشن (جفت نمبر یقینی بناتے ہوئے)
    target_w = (info["width"] // 2) * 2
    target_h = (info["height"] // 2) * 2

    job_filename = f"job_{uuid.uuid4().hex}.mp4"
    output_path = OUTPUT_DIR / job_filename

    with JOBS_LOCK:
        ACTIVE_JOBS.add(job_filename)

    try:
        now_str = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

        # 10. دونوں موڈز کا حقیقی اور واضح فرق
        if process_mode == "Standard Clean (صرف میٹا ڈیٹا اور فارمیٹ کلین اپ)":
            vf_filters = [
                "setpts=PTS-STARTPTS",
                f"scale={target_w}:{target_h}:flags=bicubic"
            ]
            if flip_video:
                vf_filters.append("hflip")

            af_filters = ["asetpts=PTS-STARTPTS"] if info["has_audio"] else []

        else:
            # Creative / Deep Transform Mode
            rand_speed = round(random.uniform(1.03, 1.05), 3)
            rand_sat = round(random.uniform(1.02, 1.05), 2)
            rand_contrast = round(random.uniform(1.01, 1.03), 2)
            rand_gamma = round(random.uniform(0.98, 1.02), 2)
            p_shift = random.randint(3, 6)

            vf_filters = [
                # 7. درست ٹائم اسٹیمپ نارملائزیشن
                f"setpts=(PTS-STARTPTS)/{rand_speed}",
                # 11. Moving crop / pan + اصل ریزولوشن پر واپس اسکیلنگ
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
                # 1. ڈویلپر کے بتائے ہوئے عین مطابق 44.1kHz اور 48kHz دونوں کے لیے پرفیکٹ سنک فارمولا
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

        # 5, 6 & 9. محفوظ کمانڈ، واضح Stream Mapping اور لاگنگ فلٹرز
        command = [
            "ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin", "-y",
            "-i", input_video,
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
            "-sn", "-dn",  # 6. غیر ضروری سب ٹائٹل یا ڈیٹا اسٹریمز شامل نہ کریں
            "-map_metadata", "-1",
            "-map_chapters", "-1",
            "-metadata", f"creation_time={now_str}",
            "-c:v", "libx264",
            "-preset", "faster",
            "-crf", "20",
            "-g", "50",
            "-threads", "2",  # 13. سرور کے CPU تھریڈز کو محدود رکھنا
            "-pix_fmt", "yuv420p",
            "-movflags", "+faststart",
            str(output_path)
        ])

        # فکس #3: دورانیے کے مطابق ڈائنامک بجٹ
        timeout_budget = max(FFMPEG_TIMEOUT_SEC, int(info["duration"] * 3.5))
        try:
            res = subprocess.run(command, capture_output=True, text=True, timeout=timeout_budget)
        except subprocess.TimeoutExpired:
            logger.error(f"FFmpeg encoding timeout on job {job_filename}")
            if output_path.exists():
                output_path.unlink(missing_ok=True)
            raise AppError("سرور پر ویڈیو پراسیسنگ کا وقت ختم ہو گیا (Timeout)۔ براہ کرم چھوٹی ویڈیو اپلوڈ کریں۔")

        if res.returncode != 0:
            logger.error(f"FFmpeg error (code {res.returncode}): {res.stderr}")
            if output_path.exists():
                output_path.unlink(missing_ok=True)
            raise AppError("ویڈیو انکوڈنگ کے دوران تکنیکی مسئلہ پیش آیا۔")

        # 14. آؤٹ پٹ فائل کی مکمل جانچ
        verify_output_file(str(output_path), info["has_audio"])
        return str(output_path)

    except Exception as e:
        if output_path.exists():
            output_path.unlink(missing_ok=True)
        if isinstance(e, AppError):
            raise e
        logger.exception("Unexpected error during video processing:")
        raise AppError("ایک غیر متوقع خرابی پیش آئی۔ براہ کرم دوبارہ کوشش کریں۔")
    finally:
        with JOBS_LOCK:
            ACTIVE_JOBS.discard(job_filename)

# 12. حقیقت پسندانہ اور شفاف UI ڈسکرپشن
st.markdown("## 🎬 Video Re-Encoder & Creative Reframer (V4)")
st.markdown(
    "> **نوٹ:** یہ ٹول ویڈیو کو دوبارہ encode کرتا ہے اور آپ کے منتخب کردہ ٹرانسفارمیشنز لاگو کرتا ہے۔ "
    "اس عمل سے تصویر (کروپ/رنگ) اور آواز میں معمولی تبدیلی آ سکتی ہے۔ اپنی اصل فائل ہمیشہ محفوظ رکھیں۔"
)

col1, col2 = st.columns(2)

with col1:
    uploaded_file = st.file_uploader("اپنی ویڈیو اپلوڈ کریں (حد: 90 سیکنڈ، 150MB)", type=["mp4", "mov", "mkv", "avi"])

    process_mode = st.radio(
        "پراسیسنگ موڈ منتخب کریں",
        options=[
            "Standard Clean (صرف میٹا ڈیٹا اور فارمیٹ کلین اپ)",
            "Creative Transform (Moving Crop, Perspective, Speed & Audio Shift)"
        ],
        index=1
    )

    flip_check = st.checkbox(
        "Horizontal Flip (اختیاری: ویڈیو کو دائیں سے بائیں الٹیں — نوٹ: اس سے لکھائی اور لوگو الٹ جائیں گے)",
        value=False
    )

    submit_btn = st.button("🚀 ویڈیو پراسیس کریں", type="primary", use_container_width=True)

with col2:
    st.markdown("**تیار شدہ ویڈیو (خودکار صفائی: ہر 30 منٹ بعد پرانی فائلیں حذف ہو جاتی ہیں)**")
    if submit_btn:
        if uploaded_file is None:
            st.error("براہ کرم پہلے ویڈیو فائل اپلوڈ کریں۔")
        else:
            temp_input = OUTPUT_DIR / f"temp_in_{uuid.uuid4().hex}.mp4"
            try:
                with open(temp_input, "wb") as f:
                    f.write(uploaded_file.getbuffer())

                with st.spinner("ویڈیو پراسیس ہو رہی ہے... براہ کرم انتظار کریں۔"):
                    final_video_path = run_advanced_reframe(str(temp_input), flip_check, process_mode)

                st.success("✅ ویڈیو کامیابی سے تیار ہو گئی ہے!")
                st.video(final_video_path)

                with open(final_video_path, "rb") as file_bytes:
                    st.download_button(
                        label="📥 تیار شدہ ویڈیو ڈاؤن لوڈ کریں",
                        data=file_bytes,
                        file_name=f"reframed_{Path(final_video_path).name}",
                        mime="video/mp4",
                        use_container_width=True
                    )
            except AppError as ae:
                st.error(str(ae))
            except Exception:
                st.error("ایک غیر متوقع خرابی پیش آئی۔ براہ کرم دوبارہ کوشش کریں۔")
            finally:
                if temp_input.exists():
                    temp_input.unlink(missing_ok=True)
