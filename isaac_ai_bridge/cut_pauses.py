"""
Cut the frozen "Claude is thinking" stretches out of an OBS recording.

agent.py calls this automatically after each recording. To run it by hand:
    python cut_pauses.py "C:/Users/you/Videos/2026-10-22 11-05-32.mkv"
It finds the frames where the picture is frozen, keeps only the frozen stretches
that match Claude's thinking pauses logged in <video>.pauses.json, cuts them, and
writes <video>_cut.mp4 next to it. The original file is never changed.
(--timeline cuts by the logged times alone; --detect uses the picture alone.)

Continuous background music: cutting chops the game's music into jumps. Turn the
game's MUSIC volume to 0 in Isaac's options (keep sound effects on), and pass a
music file; it is looped and mixed under the effects for the whole video:
    python cut_pauses.py "video.mkv" --music "C:/music/bgm.mp3"
(agent.py does this automatically when BGM_PATH is set.) Only use music you have
the right to publish.

No .pauses.json (older recording, or recorded by hand)? Detect frozen stretches
from the picture itself instead:
    python cut_pauses.py "video.mkv" --detect
This finds stretches where the image does not change for at least 0.7 s. Isaac's
gameplay is almost never perfectly still, so it removes the thinking pauses; check
the result once.

Needs ffmpeg:   winget install Gyan.FFmpeg   (then open a NEW PowerShell window)

Why this exists: OBS can only pause when its recording output supports it. In
Settings -> Output (Simple mode) "Recording Quality: Same as stream" cannot pause;
pick "High Quality, Medium File Size" (or use Advanced mode) and OBS pauses for
real, leaving nothing to cut.
"""

import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path


def _find(tool):
    """Locate ffmpeg/ffprobe: PATH first, then where winget and common installs put them."""
    found = shutil.which(tool)
    if found:
        return found
    import os
    exe = tool + ".exe"
    local = Path(os.environ.get("LOCALAPPDATA", ""))
    candidates = [local / "Microsoft" / "WinGet" / "Links" / exe]
    for root in (local / "Microsoft" / "WinGet" / "Packages",
                 Path(os.environ.get("ProgramFiles", "C:/Program Files")),
                 Path("C:/ffmpeg")):
        if root.exists():
            candidates += sorted(root.glob(f"**/bin/{exe}"))
    for c in candidates:
        if c.is_file():
            return str(c)
    return None


def _probe(ffprobe, video):
    out = subprocess.run(
        [ffprobe, "-v", "error", "-show_entries", "format=duration:stream=codec_type",
         "-of", "json", str(video)],
        capture_output=True, text=True, check=True).stdout
    info = json.loads(out)
    duration = float(info["format"]["duration"])
    has_audio = any(s.get("codec_type") == "audio" for s in info.get("streams", []))
    return duration, has_audio


def _fps(ffprobe, video):
    out = subprocess.run([ffprobe, "-v", "error", "-select_streams", "v:0", "-show_entries",
                          "stream=avg_frame_rate", "-of", "csv=p=0", str(video)],
                         capture_output=True, text=True).stdout.strip()
    try:
        num, den = out.split("/")
        return float(num) / float(den) if float(den) else 60.0
    except ValueError:
        return 60.0


def keep_segments(gaps_ms, duration):
    """Turn frozen stretches (ms) into the list of [start, end] seconds to keep."""
    keep, t = [], 0.0
    for a, b in sorted(gaps_ms):
        a, b = max(0.0, a / 1000), min(duration, b / 1000)
        if a > t + 0.01:
            keep.append((t, a))
        t = max(t, b)
    if duration > t + 0.01:
        keep.append((t, duration))
    return keep


def _encoders(ffmpeg):
    """Encoders to try in order: NVIDIA NVENC (fast on RTX cards), then CPU x264."""
    out = subprocess.run([ffmpeg, "-hide_banner", "-encoders"], capture_output=True, text=True).stdout
    cpu = ["-c:v", "libx264", "-preset", "veryfast", "-crf", "18"]
    if "h264_nvenc" in out:
        return [["-c:v", "h264_nvenc", "-preset", "p5", "-cq", "19"], cpu]
    return [cpu]


def detect_frozen(video, min_seconds=0.4, noise=0.003):
    """Find stretches where the picture does not change, via ffmpeg's freezedetect."""
    import re
    ffmpeg = _find("ffmpeg")
    if not ffmpeg:
        raise RuntimeError("ffmpeg not found. Install it:  winget install Gyan.FFmpeg  "
                           "then open a new PowerShell window")
    print(f"[detect] Scanning {Path(video).name} for frozen stretches ...")
    r = subprocess.run([ffmpeg, "-hide_banner", "-nostats", "-hwaccel", "auto", "-i", str(video),
                        "-map", "0:v:0",
                        "-vf", f"freezedetect=n={noise}:d={min_seconds}", "-f", "null", "-"],
                       capture_output=True, text=True)
    starts = [float(x) for x in re.findall(r"freeze_start: ([\d.]+)", r.stderr)]
    ends = [float(x) for x in re.findall(r"freeze_end: ([\d.]+)", r.stderr)]
    if len(ends) < len(starts):                       # frozen until the very end
        ends.append(_probe(_find("ffprobe"), video)[0])
    # freezedetect starts a freeze AT the last changing frame (the one that then
    # repeats). That frame is real gameplay, so keep it: start one frame later.
    frame = 1.0 / _fps(_find("ffprobe"), video)
    gaps = [[(a + frame) * 1000, b * 1000] for a, b in zip(starts, ends) if b - a > frame]
    print(f"[detect] Found {len(gaps)} frozen stretches, "
          f"{sum(b - a for a, b in gaps) / 1000:.1f}s in total.")
    return gaps


def refine(detected_ms, logged_ms, min_share=0.5):
    """Keep only picture-frozen stretches that mostly fall inside a logged thinking
    pause. The picture gives frame-accurate edges; the log rules out ordinary still
    gameplay (e.g. Isaac standing in a cleared room). Logged times are ~0.1 s off,
    which the 50 % overlap rule tolerates."""
    logged = sorted(logged_ms)
    out = []
    for a, b in detected_ms:
        overlap = sum(max(0, min(b, lb) - max(a, la)) for la, lb in logged)
        if b > a and overlap / (b - a) >= min_share:
            out.append([a, b])
    return out


CHUNK_SECONDS = 60          # source time handled per ffmpeg run
CHUNK_TERMS = 40            # kept segments per run (keeps ffmpeg's expression small)


def _chunks(keep):
    """Group kept segments so each ffmpeg run gets a short window and a short expression."""
    groups, cur = [], []
    for seg in keep:
        if cur and (seg[1] - cur[0][0] > CHUNK_SECONDS or len(cur) >= CHUNK_TERMS):
            groups.append(cur)
            cur = []
        cur.append(seg)
    if cur:
        groups.append(cur)
    return groups


def _run(cmd):
    r = subprocess.run(cmd, capture_output=True, text=True)
    return r.returncode == 0, r.stderr


def cut(video, gaps_ms=None, music=None, music_volume=0.35):
    video = Path(video)
    ffmpeg, ffprobe = _find("ffmpeg"), _find("ffprobe")
    if not ffmpeg or not ffprobe:
        raise RuntimeError("ffmpeg not found. Install it:  winget install Gyan.FFmpeg  "
                           "then open a new PowerShell window")
    if gaps_ms is None:
        gaps_ms = json.loads(video.with_suffix(".pauses.json").read_text(encoding="utf-8"))

    duration, has_audio = _probe(ffprobe, video)
    keep = keep_segments(gaps_ms, duration)
    removed = duration - sum(b - a for a, b in keep)
    if removed < 0.5 and not music:
        print("[cut] Nothing worth cutting; OBS paused correctly.")
        return None
    if not keep:
        raise RuntimeError("nothing left to keep after cutting")

    groups = _chunks(keep)
    print(f"[cut] Removing {removed:.1f}s of frozen footage from {duration:.1f}s "
          f"({len(keep)} pieces kept, processed in {len(groups)} parts)"
          + (f", adding music {Path(music).name}" if music else "") + " ...")

    out = video.with_name(video.stem + "_cut.mp4")
    work = Path(tempfile.mkdtemp(prefix="cut_", dir=video.parent))
    encoders = _encoders(ffmpeg)
    try:
        # Pass 1: cut each window. Video in the final codec, audio as lossless PCM so
        # joining the parts later leaves no clicks or gaps at the seams.
        parts = []
        for i, group in enumerate(groups, 1):
            t0, t1 = group[0][0], group[-1][1]
            expr = "+".join(f"between(t,{a - t0:.3f},{b - t0:.3f})" for a, b in group)
            graph = f"[0:v]select='{expr}',setpts=N/FRAME_RATE/TB[v]"
            maps = ["-map", "[v]"]
            if has_audio:
                graph += f";[0:a]aselect='{expr}',asetpts=N/SR/TB[a]"
                maps += ["-map", "[a]"]
            part = work / f"part{i:04d}.mkv"
            base = [ffmpeg, "-hide_banner", "-loglevel", "error", "-y",
                    "-ss", f"{t0:.3f}", "-t", f"{t1 - t0:.3f}", "-i", str(video),
                    "-filter_complex", graph] + maps
            audio = ["-c:a", "pcm_s16le", "-ar", "48000"] if has_audio else []
            while True:
                ok, err = _run(base + encoders[0] + audio + [str(part)])
                if ok or len(encoders) == 1:
                    break
                encoders = encoders[1:]        # GPU encoder failed: fall back to CPU
            if not ok:
                print(err[-2000:])
                raise RuntimeError(f"ffmpeg failed on part {i} (see the messages above)")
            parts.append(part)
            print(f"\r[cut] part {i}/{len(groups)}", end="", flush=True)
        print()

        # Pass 2: join the parts (video copied, not re-encoded) and encode audio once,
        # mixing in looped background music if asked.
        listing = work / "parts.txt"
        listing.write_text("".join(f"file '{p.name}'\n" for p in parts), encoding="utf-8")
        cmd = [ffmpeg, "-hide_banner", "-loglevel", "error", "-y",
               "-f", "concat", "-safe", "0", "-i", str(listing)]
        if music:
            cmd += ["-stream_loop", "-1", "-i", str(music)]
            if has_audio:
                graph = (f"[1:a]volume={music_volume}[m];"
                         "[0:a][m]amix=inputs=2:duration=first:dropout_transition=0:normalize=0[a]")
            else:
                graph = f"[1:a]volume={music_volume}[a]"
            cmd += ["-filter_complex", graph, "-map", "0:v", "-map", "[a]", "-shortest"]
        else:
            cmd += ["-map", "0:v"] + (["-map", "0:a"] if has_audio else [])
        cmd += ["-c:v", "copy"]
        if has_audio or music:
            cmd += ["-c:a", "aac", "-b:a", "192k"]
        cmd += ["-movflags", "+faststart", str(out)]
        ok, err = _run(cmd)
        if not ok:
            print(err[-2000:])
            raise RuntimeError("ffmpeg failed while joining (see the messages above)")
    finally:
        shutil.rmtree(work, ignore_errors=True)
    print(f"[cut] Done: {out}")
    return out


def smart_cut(video, logged_ms=None, music=None, music_volume=0.35):
    """Default: detect frozen frames in the picture, keep those that match the log."""
    detected = detect_frozen(video)
    if logged_ms:
        gaps = refine(detected, logged_ms)
        print(f"[cut] {len(gaps)} of {len(detected)} still stretches match Claude's thinking "
              "pauses; only those are cut.")
    else:
        gaps = detected
    return cut(video, gaps, music=music, music_volume=music_volume)


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser(description="Cut frozen stretches out of an OBS recording.")
    ap.add_argument("video")
    ap.add_argument("--music", help="background music file, looped under the whole video")
    ap.add_argument("--music-volume", type=float, default=0.35)
    ap.add_argument("--detect", action="store_true",
                    help="use only the picture (ignore <video>.pauses.json)")
    ap.add_argument("--timeline", action="store_true",
                    help="use only the logged pause times (old behaviour, less exact)")
    a = ap.parse_args()
    log_file = Path(a.video).with_suffix(".pauses.json")
    logged = json.loads(log_file.read_text(encoding="utf-8")) if log_file.exists() else None
    if a.timeline:
        if not logged:
            raise SystemExit("No .pauses.json next to the video.")
        cut(a.video, logged, music=a.music, music_volume=a.music_volume)
    else:
        if logged is None and not a.detect:
            print("[cut] No .pauses.json next to the video; using the picture only.")
        smart_cut(a.video, None if a.detect else logged, music=a.music, music_volume=a.music_volume)
