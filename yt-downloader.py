
#!/usr/bin/env python3
from __future__ import annotations
import argparse
import logging
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Optional
from pytube import YouTube, Stream
from pytube.exceptions import RegexMatchError, VideoUnavailable, MembersOnly, LiveStreamError

logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
logger = logging.getLogger("yt_downloader")

YOUTUBE_URL_RE = re.compile(r"(https?://)?(www\.)?(youtube\.com/watch\?v=|youtu\.be/|youtube\.com/shorts/)(?P<id>[-_0-9A-Za-z]+)")

def is_youtube_url(url: str) -> bool:
    return bool(YOUTUBE_URL_RE.search(url))

def fs_safe_filename(name: str) -> str:
    safe = re.sub(r'[\\/*?:"<>|]', "_", name).strip()
    safe = re.sub(r"\s+", " ", safe)
    return safe

def unique_path(path: Path) -> Path:
    if not path.exists():
        return path
    parent = path.parent
    stem = path.stem
    suffix = path.suffix
    i = 1
    while True:
        candidate = parent / f"{stem} ({i}){suffix}"
        if not candidate.exists():
            return candidate
        i += 1

def mk_progress_callbacks(title: str):
    def on_progress(stream: Stream, chunk: bytes, bytes_remaining: int):
        total = getattr(stream, "filesize", None) or getattr(stream, "filesize_approx", None)
        if total:
            downloaded = total - bytes_remaining
            pct = downloaded / total * 100
            tag = getattr(stream, "resolution", None) or getattr(stream, "abr", None) or stream.mime_type
            print(f"\r{title} | {tag} | {pct:6.2f}% ({downloaded:,}/{total:,} bytes)", end="", flush=True)
        else:
            print(f"\r{title} | downloading... ({bytes_remaining} bytes remaining)", end="", flush=True)
    def on_complete(stream: Stream, file_path: str):
        print()
        logger.info("Completed: %s -> %s", title, file_path)
    return on_progress, on_complete

def download_video(url: str, output_dir: str = ".", quality: str = "best", list_streams: bool = False, itag: Optional[int] = None, merge_adaptive: bool = True) -> Optional[Path]:
    if not is_youtube_url(url):
        raise ValueError("Provided URL does not look like a YouTube URL.")
    output_dir_p = Path(output_dir).expanduser().resolve()
    output_dir_p.mkdir(parents=True, exist_ok=True)
    try:
        yt = YouTube(url)
    except RegexMatchError:
        raise ValueError("Invalid YouTube URL.")
    except VideoUnavailable:
        raise RuntimeError("Video is unavailable.")
    except Exception as e:
        raise RuntimeError(f"Failed to initialize YouTube object: {e}")
    title = fs_safe_filename(yt.title or "youtube_video")
    on_progress, on_complete = mk_progress_callbacks(title)
    try:
        yt.register_on_progress_callback(on_progress)
        yt.register_on_complete_callback(on_complete)
    except Exception:
        yt = YouTube(url, on_progress_callback=on_progress, on_complete_callback=on_complete)
    logger.info("Video title: %s", yt.title)
    logger.info("Author: %s, Length: %s seconds", yt.author, yt.length)
    streams = yt.streams
    if list_streams:
        logger.info("Available streams (itag, mime_type, res/abr, progressive):")
        for s in streams.order_by("resolution").desc():
            prog = "yes" if s.is_progressive else "no"
            res = getattr(s, "resolution", None) or getattr(s, "abr", None) or ""
            logger.info("  itag=%s  %s  %s  prog=%s", s.itag, s.mime_type, res, prog)
        return None
    if itag is not None:
        stream = streams.get_by_itag(itag)
        if stream is None:
            raise ValueError(f"No stream with itag={itag} found.")
        if stream.is_progressive:
            out_name = unique_path(output_dir_p / (title + "." + stream.subtype))
            path = stream.download(output_path=str(output_dir_p), filename=out_name.name)
            return Path(path)
        else:
            audio = streams.filter(only_audio=True).order_by("abr").desc().first()
            if not audio:
                raise RuntimeError("Couldn't find audio stream to pair with the selected video itag.")
            return _download_and_merge_adaptive(stream, audio, output_dir_p, title, merge_adaptive)
    if quality.lower() in ("audio", "audio-only"):
        audio = streams.filter(only_audio=True).order_by("abr").desc().first()
        if not audio:
            raise RuntimeError("No audio stream available.")
        out_name = unique_path(output_dir_p / (title + "." + audio.subtype))
        logger.info("Downloading audio-only: %s", audio.abr)
        path = audio.download(output_path=str(output_dir_p), filename=out_name.name)
        return Path(path)
    if re.match(r"^\d+p$", quality):
        desired_res = quality
        progressive = streams.filter(progressive=True, file_extension="mp4", res=desired_res).first()
        progressive = progressive or streams.filter(progressive=True, resolution=desired_res).first()
        if progressive:
            out_name = unique_path(output_dir_p / (title + "." + progressive.subtype))
            logger.info("Found progressive %s -> downloading", desired_res)
            path = progressive.download(output_path=str(output_dir_p), filename=out_name.name)
            return Path(path)
        video_adaptive = streams.filter(adaptive=True, only_video=True, resolution=desired_res, file_extension="mp4").first()
        if video_adaptive is None:
            video_adaptive = streams.filter(adaptive=True, only_video=True).order_by("resolution").desc().first()
        audio_adaptive = streams.filter(only_audio=True).order_by("abr").desc().first()
        if not video_adaptive or not audio_adaptive:
            raise RuntimeError("Could not find suitable video/audio streams for requested resolution.")
        return _download_and_merge_adaptive(video_adaptive, audio_adaptive, output_dir_p, title, merge_adaptive)
    best_prog = streams.filter(progressive=True, file_extension="mp4").order_by("resolution").desc().first()
    if best_prog:
        out_name = unique_path(output_dir_p / (title + "." + best_prog.subtype))
        logger.info("Downloading best progressive: %s", getattr(best_prog, "resolution", "unknown"))
        path = best_prog.download(output_path=str(output_dir_p), filename=out_name.name)
        return Path(path)
    best_video = streams.filter(adaptive=True, only_video=True, file_extension="mp4").order_by("resolution").desc().first()
    best_audio = streams.filter(only_audio=True, file_extension="mp4").order_by("abr").desc().first()
    if not best_video:
        best_video = streams.filter(adaptive=True, only_video=True).order_by("resolution").desc().first()
    if not best_audio:
        best_audio = streams.filter(only_audio=True).order_by("abr").desc().first()
    if not best_video or not best_audio:
        raise RuntimeError("No suitable streams found to download.")
    return _download_and_merge_adaptive(best_video, best_audio, output_dir_p, title, merge_adaptive)

def _download_and_merge_adaptive(video_stream: Stream, audio_stream: Stream, output_dir: Path, title: str, merge_with_ffmpeg: bool) -> Path:
    logger.info("Adaptive download selected: video %s | audio %s", getattr(video_stream, "resolution", "V"), getattr(audio_stream, "abr", "A"))
    with tempfile.TemporaryDirectory() as tmpdir:
        tmpdir_p = Path(tmpdir)
        video_tmp = tmpdir_p / f"video.{video_stream.subtype}"
        audio_tmp = tmpdir_p / f"audio.{audio_stream.subtype}"
        logger.info("Downloading video to temporary file...")
        video_stream.download(output_path=str(tmpdir_p), filename=video_tmp.name)
        logger.info("Downloading audio to temporary file...")
        audio_stream.download(output_path=str(tmpdir_p), filename=audio_tmp.name)
        final_ext = ".mp4"
        out_path = unique_path(output_dir / (title + final_ext))
        ffmpeg_path = shutil.which("ffmpeg")
        if merge_with_ffmpeg and ffmpeg_path:
            logger.info("Merging with ffmpeg...")
            cmd_copy = [ffmpeg_path, "-y", "-i", str(video_tmp), "-i", str(audio_tmp), "-c", "copy", str(out_path)]
            res = subprocess.run(cmd_copy, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            if res.returncode != 0:
                logger.warning("ffmpeg copy failed, trying re-encode audio to AAC (slower)...")
                cmd_reencode = [ffmpeg_path, "-y", "-i", str(video_tmp), "-i", str(audio_tmp), "-c:v", "copy", "-c:a", "aac", "-b:a", "192k", str(out_path)]
                res2 = subprocess.run(cmd_reencode, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
                if res2.returncode != 0:
                    logger.error("ffmpeg failed to merge streams. ffmpeg stderr:\n%s", res2.stderr.decode(errors="ignore"))
                    fallback = unique_path(output_dir / video_tmp.name)
                    shutil.move(str(video_tmp), str(fallback))
                    logger.warning("Saved video (no audio) to: %s", fallback)
                    return fallback
            logger.info("Merged file saved to: %s", out_path)
            return out_path
        else:
            video_out = unique_path(output_dir / f"{title}_video.{video_tmp.suffix.lstrip('.') or video_tmp.suffix}")
            audio_out = unique_path(output_dir / f"{title}_audio.{audio_tmp.suffix.lstrip('.') or audio_tmp.suffix}")
            shutil.move(str(video_tmp), str(video_out))
            shutil.move(str(audio_tmp), str(audio_out))
            logger.warning("ffmpeg not found or merging disabled. Saved separate files:\n  %s\n  %s", video_out, audio_out)
            return Path(video_out)

def parse_args():
    p = argparse.ArgumentParser(description="YouTube downloader (pytube).")
    p.add_argument("url", help="YouTube video URL")
    p.add_argument("-o", "--output", default=".", help="Output directory")
    p.add_argument("-q", "--quality", default="best", help="Quality: best, audio, or e.g. 720p")
    p.add_argument("--list", action="store_true", help="List available streams and exit")
    p.add_argument("--itag", type=int, help="Specific stream itag to download")
    p.add_argument("--no-merge", action="store_true", help="Do not merge adaptive streams with ffmpeg")
    return p.parse_args()

def main():
    args = parse_args()
    try:
        result = download_video(url=args.url, output_dir=args.output, quality=args.quality, list_streams=args.list, itag=args.itag, merge_adaptive=not args.no_merge)
        if result:
            logger.info("Final file: %s", result)
    except KeyboardInterrupt:
        logger.info("Aborted by user.")
    except ValueError as ve:
        logger.error("Input error: %s", ve)
        sys.exit(2)
    except (VideoUnavailable, MembersOnly, LiveStreamError) as e:
        logger.error("Video error: %s", e)
        sys.exit(3)
    except Exception as exc:
        logger.exception("Download failed: %s", exc)
        sys.exit(4)

if __name__ == "__main__":
    main()
