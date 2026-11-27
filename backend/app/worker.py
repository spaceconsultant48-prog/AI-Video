from pathlib import Path
import asyncio
import json
import logging
import math
import os
import re
import shutil
import subprocess
import threading
import time

from app.db import SessionLocal
from app.models import Video, VideoScene, GenerationJob, VideoFile
from app.core import settings
from app.providers import ReplicateProvider

logger = logging.getLogger("space_consultant.worker")

FPS = 25
DEFAULT_VOICE = "en-IN-NeerjaNeural"
LEAD_SECONDS = 0.8      # silence before the first word: players swallow the first moments of audio
TAIL_SECONDS = 0.8      # small silent tail after the last spoken word
TTS_ATTEMPTS = 3        # Edge TTS sometimes fails once and works on retry
OVERSAMPLE = 1.5        # zoom smoothness vs CPU: 2.0 = smoother but slower
ZOOM_STRENGTH = 0.15    # 15% zoom / pan travel over one scene
EFFECTS = ("auto", "zoom_in", "zoom_out", "pan_left", "pan_right", "none")
AUTO_CYCLE = ("zoom_in", "zoom_out", "pan_right", "pan_left")
TRANSITIONS = (
    "none", "fade", "dissolve", "fadeblack", "wipeleft", "wiperight",
    "slideleft", "slideright", "circleopen", "zoomin", "radial", "random",
)
RANDOM_CYCLE = ("fade", "slideleft", "circleopen", "wipeleft", "dissolve", "zoomin", "slideright", "radial")
MALE_VOICES = {
    "en-IN-PrabhatNeural", "hi-IN-MadhurNeural",
    "en-US-GuyNeural", "en-GB-RyanNeural",
}


def output_dimensions(resolution, aspect_ratio):
    sizes = {
        "720p": {"9:16": (720, 1280), "1:1": (720, 720), "16:9": (1280, 720)},
        "1080p": {"9:16": (1080, 1920), "1:1": (1080, 1080), "16:9": (1920, 1080)},
    }
    try:
        return sizes[resolution][aspect_ratio]
    except KeyError as exc:
        raise RuntimeError(
            f"Unsupported output settings: {resolution}, {aspect_ratio}"
        ) from exc


def ensure_output_dir():
    output_dir = Path(settings.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    return output_dir


NICE_BIN = shutil.which("nice") if os.name == "posix" else None
# Keep FFmpeg to ONE thread by default: on a 2-CPU host it would otherwise take every core,
# the web server could not answer polls/health checks, and the host answered 502.
FFMPEG_THREADS = str(max(1, int(os.environ.get("FFMPEG_THREADS", "1") or 1)))


def run_ffmpeg(args, timeout=900):
    """Run FFmpeg and include its stderr in errors so jobs do not fail silently."""
    # Lowest CPU priority: the web server must keep answering requests (and health checks)
    # while FFmpeg renders, otherwise a small host restarts the app mid-render (502).
    args = list(args)
    # global filter threads right after the binary, encoder threads just before the output file
    args[1:1] = ["-filter_threads", FFMPEG_THREADS, "-filter_complex_threads", FFMPEG_THREADS]
    args[-1:-1] = ["-threads", FFMPEG_THREADS]
    if NICE_BIN:
        args = [NICE_BIN, "-n", "19", *args]
    try:
        result = subprocess.run(
            args,
            check=False,
            capture_output=True,
            text=True,
            timeout=timeout,
            encoding="utf-8",
            errors="replace",
        )
    except FileNotFoundError as exc:
        raise RuntimeError(
            f"FFmpeg executable not found: {settings.ffmpeg_bin}. "
            "Install FFmpeg and set FFMPEG_BIN to the full ffmpeg.exe path "
            "or add its bin folder to Windows PATH."
        ) from exc
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError(
            f"FFmpeg timed out after {timeout} seconds. "
            f"Command: {args[0]}"
        ) from exc

    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "No FFmpeg error output").strip()
        raise RuntimeError(f"FFmpeg failed (exit {result.returncode}): {detail[-5000:]}")
    return result


MAX_VOICE_VIDEO_SECONDS = 300   # longest uploaded video that can get an AI voice
MAX_STORED_MB = 60       # biggest video kept in the database (free Neon DB is 0.5 GB)
MAX_XFADE_INPUTS = 4    # FFmpeg opens every input at once; joining in small groups keeps memory low


def _xfade_join(clips, lengths, transition, seconds, output, pair_offset, crf):
    parts, current, elapsed = [], "[0:v]", lengths[0]
    for k in range(1, len(clips)):
        if transition == "random":
            name = RANDOM_CYCLE[(pair_offset + k - 1) % len(RANDOM_CYCLE)]
        else:
            name = transition
        label = f"[v{k}]"
        parts.append(
            f"{current}[{k}:v]xfade=transition={name}:duration={seconds:.3f}:"
            f"offset={elapsed - seconds:.3f}{label}"
        )
        current = label
        elapsed += lengths[k] - seconds

    args = [settings.ffmpeg_bin, "-y", "-hide_banner", "-loglevel", "error"]
    for clip in clips:
        args += ["-i", str(Path(clip).resolve())]
    args += [
        "-filter_complex", ";".join(parts), "-map", current,
        "-c:v", "libx264", "-preset", "ultrafast", "-crf", str(crf), "-pix_fmt", "yuv420p",
        "-r", str(FPS), "-an", "-movflags", "+faststart", str(output),
    ]
    run_ffmpeg(args, timeout=3600)
    return elapsed


def xfade_clips(clips, lengths, transition, seconds, output):
    """Join clips with a visible transition (fade, slide, wipe...) between every pair."""
    report("Joining scenes with transitions")
    output = Path(output)
    clips = [Path(c) for c in clips]
    lengths = list(lengths)
    temp_files = []
    pair_offset = 0
    try:
        # Join in small groups first, then join the groups, so memory never grows with scene count.
        while len(clips) > MAX_XFADE_INPUTS:
            next_clips, next_lengths = [], []
            for start in range(0, len(clips), MAX_XFADE_INPUTS):
                chunk = clips[start:start + MAX_XFADE_INPUTS]
                chunk_lengths = lengths[start:start + MAX_XFADE_INPUTS]
                if len(chunk) == 1:
                    next_clips.append(chunk[0])
                    next_lengths.append(chunk_lengths[0])
                    continue
                part = output.with_name(f"{output.stem}-join{len(temp_files)}.mp4")
                temp_files.append(part)
                length = _xfade_join(chunk, chunk_lengths, transition, seconds, part, pair_offset, crf=18)
                pair_offset += len(chunk) - 1
                next_clips.append(part)
                next_lengths.append(length)
            clips, lengths = next_clips, next_lengths
        _xfade_join(clips, lengths, transition, seconds, output, pair_offset, crf=23)
    finally:
        for f in temp_files:
            f.unlink(missing_ok=True)


def concat_clips(clips, output_path, video, lengths=None, transition="none", transition_seconds=0.0):
    if not clips:
        raise RuntimeError("No scene clips were created.")

    width, height = output_dimensions(video.resolution, video.aspect_ratio)
    output = Path(output_path)
    output.parent.mkdir(parents=True, exist_ok=True)

    if transition != "none" and transition_seconds > 0 and len(clips) > 1 and lengths:
        xfade_clips(clips, lengths, transition, transition_seconds, output)
        return

    list_path = output.with_name(output.stem + ".concat.txt")

    # Escape apostrophes for FFmpeg concat-demuxer file syntax.
    lines = []
    for clip in clips:
        resolved = Path(clip).resolve().as_posix().replace("'", r"'\''")
        lines.append(f"file '{resolved}'\n")
    list_path.write_text("".join(lines), encoding="utf-8")

    try:
        run_ffmpeg([
            settings.ffmpeg_bin, "-y", "-hide_banner", "-loglevel", "error",
            "-f", "concat", "-safe", "0", "-i", str(list_path),
            "-vf",
            f"scale={width}:{height}:force_original_aspect_ratio=increase,"
            f"crop={width}:{height},fps=25,format=yuv420p",
            "-c:v", "libx264", "-preset", "veryfast",
            "-pix_fmt", "yuv420p", "-r", "25", "-an",
            "-movflags", "+faststart", str(output),
        ], timeout=1800)
    finally:
        list_path.unlink(missing_ok=True)


_progress = None


def report(text):
    """Tell the UI (and the Render log) which step the render is on."""
    logger.info("[render] %s", text)
    callback = _progress
    if callback:
        try:
            callback(text)
        except Exception:
            logger.exception("Could not save render progress")


def media_duration(path):
    """Length of an audio/video file in seconds (parsed from FFmpeg, no ffprobe needed)."""
    result = subprocess.run(
        [settings.ffmpeg_bin, "-hide_banner", "-i", str(path)],
        check=False, capture_output=True, text=True, timeout=120,
        encoding="utf-8", errors="replace",
    )
    match = re.search(r"Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)", result.stderr or "")
    if not match:
        raise RuntimeError(f"Could not read duration of {Path(path).name}")
    hours, minutes, seconds = match.groups()
    return int(hours) * 3600 + int(minutes) * 60 + float(seconds)


def clean_script(text):
    """Remove things a voice should never read aloud (markdown, [stage directions], 'Scene 1:')."""
    text = re.sub(r"```.*?```", " ", text, flags=re.S)
    text = re.sub(r"\[[^\]]{0,200}\]", " ", text)
    text = re.sub(r"(?im)^\s*(scene|\u0926\u0943\u0936\u094d\u092f)\s*\d+\s*[:\-\u2013]\s*", "", text)
    text = re.sub(r"[*_#`]+", " ", text)
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def pick_voice(text, voice_id):
    """
    Hindi (Devanagari) script read by an English voice sounds wrong or gets skipped.
    If the script is mostly Devanagari, switch to the matching Hindi voice.
    Returns (voice_id, note_or_None).
    """
    voice_id = (voice_id or DEFAULT_VOICE).strip() or DEFAULT_VOICE
    letters = [c for c in text if c.isalpha()]
    if letters and not voice_id.startswith("hi-IN"):
        devanagari = sum(1 for c in letters if "\u0900" <= c <= "\u097f")
        if devanagari / len(letters) > 0.3:
            hindi = "hi-IN-MadhurNeural" if voice_id in MALE_VOICES else "hi-IN-SwaraNeural"
            return hindi, f"Script is in Hindi, so the voice was switched from {voice_id} to {hindi}."
    return voice_id, None


def read_video_config(video):
    """video.script holds JSON (voice, effects, transition) or an old plain-text script."""
    raw = (video.script or "").strip()
    if not raw:
        return {}
    try:
        config = json.loads(raw)
    except (TypeError, json.JSONDecodeError):
        config = None
    return config if isinstance(config, dict) else {"voiceover_text": raw}


def read_narration_config(video):
    """Returns (script_text, voice_id)."""
    config = read_video_config(video)
    text = str(config.get("voiceover_text") or config.get("script") or "")
    voice_id = str(config.get("voice_id") or DEFAULT_VOICE)
    return text.strip(), voice_id.strip() or DEFAULT_VOICE


def resolve_effect(name, index):
    """'auto' alternates zoom in / zoom out / pan so every scene looks different."""
    name = str(name or "auto")
    if name not in EFFECTS or name == "auto":
        return AUTO_CYCLE[index % len(AUTO_CYCLE)]
    return name


def motion_size(effect, width, height):
    """Size the still image must have before the motion filter runs."""
    if effect == "none":
        return width, height
    return int(width * OVERSAMPLE) // 2 * 2, int(height * OVERSAMPLE) // 2 * 2


def prepare_image(src, size, dest):
    """
    Shrink + crop the photo to the exact working size ONCE.
    FFmpeg's `-loop 1` re-decodes the image for every frame, so a 12-megapixel phone photo
    made each scene very slow and pinned the CPU. A small pre-sized JPEG removes that cost.
    Also applies the photo's EXIF rotation.
    """
    from PIL import Image, ImageOps
    target_w, target_h = size
    with Image.open(src) as img:
        img = ImageOps.exif_transpose(img)
        if img.mode not in ("RGB", "L"):
            background = Image.new("RGB", img.size, (0, 0, 0))
            rgba = img.convert("RGBA")
            background.paste(rgba, mask=rgba.split()[-1])
            img = background
        else:
            img = img.convert("RGB")
        scale = max(target_w / img.width, target_h / img.height)
        new_w, new_h = max(target_w, round(img.width * scale)), max(target_h, round(img.height * scale))
        img = img.resize((new_w, new_h), Image.LANCZOS)
        left, top = (new_w - target_w) // 2, (new_h - target_h) // 2
        img = img.crop((left, top, left + target_w, top + target_h))
        img.save(dest, "JPEG", quality=95, subsampling=0)
    return dest


def motion_filter(effect, width, height, duration):
    """FFmpeg filter for one still image: scale/crop, then zoom in / zoom out / pan."""
    frames = max(2, int(round(duration * FPS)))
    if effect == "none":
        return (
            f"scale={width}:{height}:force_original_aspect_ratio=increase,"
            f"crop={width}:{height},fps={FPS},format=yuv420p"
        )
    # Work on a slightly larger image so the motion is smooth instead of jittery.
    big_w, big_h = int(width * OVERSAMPLE) // 2 * 2, int(height * OVERSAMPLE) // 2 * 2
    zoom_in = f"1+{ZOOM_STRENGTH}*on/{frames}"
    zoom_out = f"{1 + ZOOM_STRENGTH}-{ZOOM_STRENGTH}*on/{frames}"
    center_x, center_y = "iw/2-(iw/zoom/2)", "ih/2-(ih/zoom/2)"
    expr = {
        "zoom_in": (zoom_in, center_x, center_y),
        "zoom_out": (zoom_out, center_x, center_y),
        "pan_right": (str(1 + ZOOM_STRENGTH), f"(iw-iw/zoom)*on/{frames}", center_y),
        "pan_left": (str(1 + ZOOM_STRENGTH), f"(iw-iw/zoom)*(1-on/{frames})", center_y),
    }[effect]
    return (
        f"scale={big_w}:{big_h}:force_original_aspect_ratio=increase,"
        f"crop={big_w}:{big_h},"
        f"zoompan=z='{expr[0]}':x='{expr[1]}':y='{expr[2]}':d=1:"
        f"s={width}x{height}:fps={FPS},format=yuv420p"
    )


def transition_plan(config, durations):
    """Returns (transition_name, seconds). seconds is 0 when transitions are off."""
    name = str(config.get("transition") or "none")
    if name not in TRANSITIONS or name == "none" or len(durations) < 2:
        return "none", 0.0
    try:
        seconds = float(config.get("transition_seconds") or 0.8)
    except (TypeError, ValueError):
        seconds = 0.8
    seconds = max(0.2, min(seconds, 1.5, min(durations) / 2))
    return name, seconds


def clip_lengths(durations, transition_seconds):
    """Each clip (except the last) is rendered `t` longer because neighbours overlap by `t`."""
    lengths = []
    for index, d in enumerate(durations):
        extra = transition_seconds if index < len(durations) - 1 else 0.0
        lengths.append(math.ceil((d + extra) * FPS) / FPS)
    return lengths


async def _tts_save(text, voice_id, audio_path):
    import edge_tts
    await edge_tts.Communicate(text, voice_id).save(str(audio_path))


def _gtts_save(text, voice_id, audio_path):
    """Backup voice (Google TTS). Used when Microsoft's Edge TTS refuses the request."""
    from gtts import gTTS
    if voice_id.startswith("hi-"):
        lang, tld = "hi", "co.in"
    elif "-IN-" in voice_id:
        lang, tld = "en", "co.in"
    elif "-GB-" in voice_id:
        lang, tld = "en", "co.uk"
    else:
        lang, tld = "en", "com"
    gTTS(text=text, lang=lang, tld=tld).save(str(audio_path))


def synthesize_speech(text, voice_id, audio_path, attempts=TTS_ATTEMPTS):
    """
    Generate the narration MP3. Edge TTS first (retrying, it fails now and then); if it keeps
    failing (common on cloud hosts), fall back to Google TTS. Returns "edge" or "gtts".
    """
    audio_path = Path(audio_path)
    last_error = None
    for attempt in range(1, attempts + 1):
        try:
            # BackgroundTasks runs this sync worker in a thread, so asyncio.run is safe.
            asyncio.run(asyncio.wait_for(_tts_save(text, voice_id, audio_path), timeout=90))
            if audio_path.exists() and audio_path.stat().st_size > 0:
                return "edge"
            raise RuntimeError("TTS provider returned an empty audio file.")
        except Exception as exc:
            last_error = exc
            audio_path.unlink(missing_ok=True)
            logger.warning("Edge TTS attempt %s/%s failed: %s", attempt, attempts, exc)
            if attempt < attempts:
                time.sleep(2 * attempt)
    try:
        _gtts_save(text, voice_id, audio_path)
        if audio_path.exists() and audio_path.stat().st_size > 0:
            logger.warning("Used backup voice (Google TTS) because Edge TTS failed: %s", last_error)
            return "gtts"
        raise RuntimeError("Backup voice returned an empty audio file.")
    except Exception as backup_error:
        audio_path.unlink(missing_ok=True)
        raise RuntimeError(f"AI voice failed. Edge TTS: {last_error} | Backup voice: {backup_error}")


def add_lead_in(audio_path):
    """Put LEAD_SECONDS of silence before the voice so the first word is not clipped on playback."""
    audio_path = Path(audio_path)
    padded = audio_path.with_name(audio_path.stem + "-lead.wav")
    try:
        run_ffmpeg([
            settings.ffmpeg_bin, "-y", "-hide_banner", "-loglevel", "error",
            "-i", str(audio_path),
            "-af", f"adelay={int(LEAD_SECONDS * 1000)}:all=1",
            "-ar", "44100", "-ac", "1", "-c:a", "pcm_s16le", str(padded),
        ], timeout=300)
        if padded.exists() and padded.stat().st_size > 0:
            audio_path.unlink(missing_ok=True)
            return padded
    except Exception:
        logger.exception("Could not add lead-in silence; using the narration as it is")
    padded.unlink(missing_ok=True)
    return audio_path


def prepare_narration(video):
    """
    Create the narration BEFORE the video is built so the video can be timed to it.
    Returns (audio_path | None, audio_seconds, note | None).
    If the voice service fails, returns no audio plus a warning so the silent
    video can still be exported.
    """
    text, voice_id = read_narration_config(video)
    text = clean_script(text)
    if not text:
        return None, 0.0, None

    voice_id, note = pick_voice(text, voice_id)
    report('Generating AI voice')
    audio_path = ensure_output_dir() / f"voice-{video.id}.mp3"
    try:
        provider = synthesize_speech(text, voice_id, audio_path)
        if provider == "gtts":
            backup = "The main AI voice was unavailable, so a backup voice was used."
            note = f"{note} {backup}" if note else backup
        audio_path = add_lead_in(audio_path)
        seconds = media_duration(audio_path)
        if seconds <= 0:
            raise RuntimeError("Generated narration has zero length.")
        logger.info("Video %s narration: %s, %.1fs", video.id, voice_id, seconds)
        return audio_path, seconds, note
    except Exception as exc:
        logger.exception("Voiceover failed for video %s; keeping silent MP4", video.id)
        audio_path.unlink(missing_ok=True)
        return None, 0.0, f"Video exported without voiceover because TTS failed: {exc}"


def fit_durations(durations, audio_seconds):
    """
    If the narration is longer than the scenes, stretch every scene proportionally
    so the whole script is spoken. If it is shorter, keep the chosen durations
    (the rest of the video is silent).
    """
    base = [float(d) for d in durations]
    total = sum(base)
    if not audio_seconds or total <= 0:
        return base
    target = audio_seconds + TAIL_SECONDS
    if target <= total:
        return base
    scale = target / total
    # Round UP to whole frames so the video is never shorter than the audio.
    return [math.ceil(d * scale * FPS) / FPS for d in base]


def has_audio_stream(path):
    result = subprocess.run(
        [settings.ffmpeg_bin, "-hide_banner", "-i", str(path)],
        check=False, capture_output=True, text=True, timeout=120, encoding="utf-8", errors="replace",
    )
    return bool(re.search(r"Stream #\S+.*Audio:", result.stderr or ""))


def add_voice_to_video(video_path, audio_path, audio_seconds, config, output):
    """
    Put an AI narration on an uploaded video.
    - narration longer than the video  -> the last frame is held so the whole script is spoken
    - narration shorter than the video -> the rest of the video stays (silent or original sound)
    - audio_mode 'replace' drops the original sound, 'mix' keeps it quietly under the voice
    """
    report("Adding AI voice to your video")
    video_seconds = media_duration(video_path)
    if video_seconds > MAX_VOICE_VIDEO_SECONDS:
        raise RuntimeError(f"Video is too long ({video_seconds:.0f}s). Maximum is {MAX_VOICE_VIDEO_SECONDS} seconds.")
    total = max(video_seconds, audio_seconds + TAIL_SECONDS)
    extra = total - video_seconds

    vf = "scale=w='trunc(if(gt(iw,ih),min(1280,iw),min(720,iw))/2)*2':h=-2,format=yuv420p"
    if extra > 0.05:
        vf += f",tpad=stop_mode=clone:stop_duration={extra:.3f}"

    mode = str(config.get("audio_mode") or "replace")
    try:
        volume = min(1.0, max(0.0, float(config.get("original_volume", 0.25))))
    except (TypeError, ValueError):
        volume = 0.25
    if mode == "mix" and has_audio_stream(video_path):
        af = (f"[0:a]volume={volume:.2f}[orig];[1:a]apad[voice];"
              "[orig][voice]amix=inputs=2:duration=longest:normalize=0[a]")
    else:
        af = "[1:a]apad[a]"

    run_ffmpeg([
        settings.ffmpeg_bin, "-y", "-hide_banner", "-loglevel", "error",
        "-i", str(video_path), "-i", str(audio_path),
        "-filter_complex", f"[0:v]{vf}[v];{af}",
        "-map", "[v]", "-map", "[a]", "-t", f"{total:.3f}",
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "23", "-pix_fmt", "yuv420p",
        "-c:a", "aac", "-b:a", "192k", "-movflags", "+faststart", str(output),
    ], timeout=max(900, int(total) * 20))


def make_slideshow(scenes, durations, video, output_path, effects=None, transition="none", transition_seconds=0.0):
    width, height = output_dimensions(video.resolution, video.aspect_ratio)
    output_dir = ensure_output_dir()
    clips = []
    temp_images = []
    effects = effects or []
    lengths = clip_lengths(durations, transition_seconds if transition != "none" else 0.0)

    try:
        for index, (scene, duration) in enumerate(zip(scenes, lengths)):
            report(f'Rendering scene {index + 1} of {len(scenes)}')
            image_path = Path(scene.image_path)
            if not image_path.exists():
                raise RuntimeError(
                    f"Scene {scene.position + 1} image is missing: {image_path}"
                )

            clip = output_dir / f"scene-{video.id}-{scene.position}.mp4"
            effect = resolve_effect(effects[index] if index < len(effects) else "auto", index)
            vf = motion_filter(effect, width, height, duration)
            prepared = output_dir / f"scene-{video.id}-{scene.position}.jpg"
            temp_images.append(prepared)
            prepare_image(image_path, motion_size(effect, width, height), prepared)
            run_ffmpeg([
                settings.ffmpeg_bin, "-y", "-hide_banner", "-loglevel", "error",
                "-loop", "1", "-i", str(prepared),
                "-t", f"{duration:.3f}", "-vf", vf,
                "-an", "-c:v", "libx264", "-preset", "ultrafast", "-crf", "20",
                "-r", str(FPS), "-pix_fmt", "yuv420p", str(clip),
            ], timeout=max(180, int(duration) * 30))

            if not clip.exists() or clip.stat().st_size == 0:
                raise RuntimeError(f"FFmpeg did not create scene {scene.position + 1}.")
            clips.append(str(clip))

        concat_clips(clips, output_path, video, lengths, transition, transition_seconds)
    finally:
        for clip in clips:
            Path(clip).unlink(missing_ok=True)
        for image in temp_images:
            image.unlink(missing_ok=True)


def make_ai_motion(scenes, durations, video, output_path, transition="none", transition_seconds=0.0):
    if not settings.replicate_api_token:
        raise RuntimeError(
            "Replicate is not configured. Set REPLICATE_API_TOKEN and "
            "REPLICATE_IMAGE_VIDEO_VERSION in backend/.env."
        )

    provider = ReplicateProvider(settings.replicate_image_video_version)
    width, height = output_dimensions(video.resolution, video.aspect_ratio)
    output_dir = ensure_output_dir()
    clips, raw_paths = [], []
    lengths = clip_lengths(durations, transition_seconds if transition != "none" else 0.0)

    try:
        for index, (scene, duration) in enumerate(zip(scenes, lengths)):
            report(f'AI motion for scene {index + 1} of {len(scenes)}')
            raw = output_dir / f"scene-ai-raw-{video.id}-{scene.position}.mp4"
            clip = output_dir / f"scene-ai-{video.id}-{scene.position}.mp4"

            provider.generate(
                scene.image_path,
                scene.prompt or "Cinematic natural motion, preserve the subject and scene",
                str(raw),
            )
            if not raw.exists() or raw.stat().st_size == 0:
                raise RuntimeError(
                    f"AI provider returned no video for scene {scene.position + 1}."
                )

            run_ffmpeg([
                settings.ffmpeg_bin, "-y", "-hide_banner", "-loglevel", "error",
                "-stream_loop", "-1", "-i", str(raw),
                "-t", f"{duration:.3f}",
                "-vf",
                f"scale={width}:{height}:force_original_aspect_ratio=increase,"
                f"crop={width}:{height},fps={FPS},format=yuv420p",
                "-an", "-c:v", "libx264", "-preset", "veryfast",
                "-pix_fmt", "yuv420p", "-r", str(FPS), str(clip),
            ], timeout=max(180, int(duration) * 30))

            if not clip.exists() or clip.stat().st_size == 0:
                raise RuntimeError(
                    f"Could not normalize AI motion scene {scene.position + 1}."
                )
            clips.append(str(clip))
            raw_paths.append(str(raw))

        concat_clips(clips, output_path, video, lengths, transition, transition_seconds)
    finally:
        for path in clips + raw_paths:
            Path(path).unlink(missing_ok=True)


def finish_video(video, silent_output, final_output, audio_path, loop_to=None):
    """
    Put the narration into the video. The video is already at least as long as the
    audio, so the voice is never cut off. Pass loop_to (seconds) for short clips
    (image-to-video) that must be looped to match the narration.
    Without audio the silent video simply becomes the final file.
    If muxing fails, the silent video is kept and a warning is returned.
    """
    silent, final = Path(silent_output), Path(final_output)
    if not audio_path:
        if silent != final:
            silent.replace(final)
        return None

    mux_path = ensure_output_dir() / f"video-{video.id}-vo.mp4"
    report('Adding voiceover to video')
    try:
        if loop_to:
            args = [
                settings.ffmpeg_bin, "-y", "-hide_banner", "-loglevel", "error",
                "-stream_loop", "-1", "-i", str(silent), "-i", str(audio_path),
                "-map", "0:v:0", "-map", "1:a:0",
                "-t", f"{loop_to:.3f}",
                "-c:v", "libx264", "-preset", "veryfast", "-pix_fmt", "yuv420p",
                "-af", "apad", "-c:a", "aac", "-b:a", "192k",
                "-movflags", "+faststart", str(mux_path),
            ]
        else:
            args = [
                settings.ffmpeg_bin, "-y", "-hide_banner", "-loglevel", "error",
                "-i", str(silent), "-i", str(audio_path),
                "-map", "0:v:0", "-map", "1:a:0",
                "-c:v", "copy", "-af", "apad",
                "-t", f"{media_duration(silent):.3f}",
                "-c:a", "aac", "-b:a", "192k",
                "-movflags", "+faststart", str(mux_path),
            ]
        run_ffmpeg(args, timeout=1800)

        if not mux_path.exists() or mux_path.stat().st_size == 0:
            raise RuntimeError("FFmpeg did not create the narrated MP4.")
        mux_path.replace(final)
        silent.unlink(missing_ok=True)
        return None
    except Exception as exc:
        logger.exception("Muxing voiceover failed for video %s; keeping silent MP4", video.id)
        if silent.exists() and silent.stat().st_size > 0:
            if silent != final:
                silent.replace(final)
            return f"Video exported without voiceover because audio mixing failed: {exc}"
        raise
    finally:
        mux_path.unlink(missing_ok=True)


_render_lock = threading.Semaphore(1)


def process(video_id):
    """Render videos one at a time; extra jobs wait (and show as pending)."""
    with _render_lock:
        _process(video_id)


def _process(video_id):
    global _progress
    """
    Process one video and always attempt to persist a terminal status.
    Any failure is recorded as failed with a useful error instead of silently
    leaving the row in 'processing'.
    """
    db = SessionLocal()
    video = None
    job = None
    audio_path = None

    try:
        video = db.get(Video, video_id)
        job = db.query(GenerationJob).filter_by(video_id=video_id).first()

        if not video:
            logger.error("Video %s not found; worker cannot start", video_id)
            return
        if not job:
            raise RuntimeError(f"Generation job record for video {video_id} was not found.")

        video.status = "processing"
        job.status = "processing"

        def _save_stage(text):
            job.provider = text[:80]
            db.commit()

        _progress = _save_stage
        report("Starting")
        # videos.error is NOT NULL in the existing SQLite schema.
        video.error = ""
        # generation_jobs.error is NOT NULL in the existing SQLite schema.
        # Use an empty string while processing instead of SQL NULL.
        job.error = ""
        db.commit()

        output_dir = ensure_output_dir()
        output = str(output_dir / f"video-{video.id}.mp4")
        silent_output = str(output_dir / f"video-{video.id}-silent.mp4")

        # Remove stale partial outputs from a previous attempt.
        for stale in (Path(output), Path(silent_output)):
            stale.unlink(missing_ok=True)

        notes = []

        if video.mode in ("multi_image_slideshow", "multi_image_ai"):
            scenes = (
                db.query(VideoScene)
                .filter_by(video_id=video.id)
                .order_by(VideoScene.position)
                .all()
            )
            if len(scenes) < 2:
                raise RuntimeError("Multi-image project needs at least two scenes.")

            # Voice first, so the scenes can be stretched to fit the whole script.
            audio_path, audio_seconds, note = prepare_narration(video)
            if note:
                notes.append(note)
            durations = fit_durations([s.duration for s in scenes], audio_seconds)
            config = read_video_config(video)
            effects = config.get("effects") if isinstance(config.get("effects"), list) else []
            transition, t_seconds = transition_plan(config, durations)

            if video.mode == "multi_image_slideshow":
                make_slideshow(scenes, durations, video, silent_output, effects, transition, t_seconds)
                job.provider = "ffmpeg-slideshow"
            else:
                make_ai_motion(scenes, durations, video, silent_output, transition, t_seconds)
                job.provider = "replicate-multi-scene"

            warning = finish_video(video, silent_output, output, audio_path)
            if warning:
                notes.append(warning)

        elif video.mode == "marketing":
            width, height = output_dimensions(video.resolution, video.aspect_ratio)
            audio_path, audio_seconds, note = prepare_narration(video)
            if note:
                notes.append(note)
            seconds = float(video.duration)
            if audio_path:
                seconds = max(seconds, audio_seconds + TAIL_SECONDS)
            effect = resolve_effect(read_video_config(video).get("effect", "zoom_in"), 0)
            vf = motion_filter(effect, width, height, seconds)
            prepared_marketing = output_dir / f"marketing-{video.id}.jpg"
            prepare_image(video.image_path, motion_size(effect, width, height), prepared_marketing)
            run_ffmpeg([
                settings.ffmpeg_bin, "-y", "-hide_banner", "-loglevel", "error",
                "-loop", "1", "-i", str(prepared_marketing),
                "-t", f"{seconds:.3f}", "-vf", vf,
                "-an", "-r", str(FPS), "-c:v", "libx264", "-pix_fmt", "yuv420p",
                silent_output,
            ], timeout=max(600, int(seconds) * 10))
            job.provider = "ffmpeg"
            warning = finish_video(video, silent_output, output, audio_path)
            if warning:
                notes.append(warning)

        elif video.mode == "video_voice":
            audio_path, audio_seconds, note = prepare_narration(video)
            if not audio_path:
                raise RuntimeError(note or "Write the narration text for the AI voice.")
            if note:
                notes.append(note)
            if not Path(video.image_path).exists():
                raise RuntimeError("The uploaded video is no longer on the server (it restarted). Please upload it again.")
            add_voice_to_video(video.image_path, audio_path, audio_seconds, read_video_config(video), output)
            job.provider = "ffmpeg-voice"

        elif video.mode == "image_to_video":
            ReplicateProvider(settings.replicate_image_video_version).generate(
                video.image_path, video.prompt, silent_output
            )
            job.provider = "replicate"
            audio_path, audio_seconds, note = prepare_narration(video)
            if note:
                notes.append(note)
            loop_to = None
            if audio_path:
                loop_to = max(media_duration(silent_output), audio_seconds + TAIL_SECONDS)
            warning = finish_video(video, silent_output, output, audio_path, loop_to=loop_to)
            if warning:
                notes.append(warning)

        else:
            raise RuntimeError(
                "Talking-avatar mode needs a configured image+audio model adapter "
                "and authorized audio input."
            )

        # Voice problems/notes are shown to the user but do not fail the video.
        if notes:
            video.error = " ".join(notes)

        output_file = Path(output)
        if not output_file.exists() or output_file.stat().st_size == 0:
            raise RuntimeError("Generation finished without creating a valid MP4 file.")

        video.output_path = output
        # Keep a copy in the database so the download still works after the host restarts.
        try:
            size = output_file.stat().st_size
            if size <= MAX_STORED_MB * 1024 * 1024:
                db.query(VideoFile).filter_by(video_id=video.id).delete()
                db.add(VideoFile(video_id=video.id, data=output_file.read_bytes(), size=size))
            else:
                logger.warning("Video %s is %.0f MB; too large to store in the database", video.id, size / 1048576)
        except Exception:
            logger.exception("Could not store video %s in the database", video.id)
        video.status = "completed"
        job.status = "completed"
        job.provider = {
            "multi_image_slideshow": "ffmpeg-slideshow", "multi_image_ai": "replicate-multi-scene",
            "marketing": "ffmpeg", "image_to_video": "replicate", "video_voice": "ffmpeg-voice",
        }.get(video.mode, job.provider)
        db.commit()
        logger.info("Video %s completed: %s", video_id, output)

    except Exception as exc:
        logger.exception("Video %s processing failed", video_id)
        try:
            db.rollback()
            # Reload rows after rollback in case SQLAlchemy expired them.
            video = db.get(Video, video_id)
            job = db.query(GenerationJob).filter_by(video_id=video_id).first()
            message = str(exc).strip() or exc.__class__.__name__
            if video:
                video.status = "failed"
                video.error = message[:3000]
            if job:
                job.status = "failed"
                job.error = message[:3000]
            db.commit()
        except Exception:
            logger.exception("Could not persist failed status for video %s", video_id)
            db.rollback()

    finally:
        _progress = None
        Path(settings.output_dir, f"marketing-{video_id}.jpg").unlink(missing_ok=True)
        if video is not None and video.mode == "video_voice" and video.image_path:
            Path(video.image_path).unlink(missing_ok=True)   # source upload is not needed after rendering
        if audio_path:
            Path(audio_path).unlink(missing_ok=True)
        db.close()
