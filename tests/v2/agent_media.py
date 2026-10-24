"""Loads agent/media_analysis.py and makes small real media files with ffmpeg, so the analysis is tested against what ffprobe really says."""
import shutil
import subprocess
import sys
from pathlib import Path

PATH = Path(__file__).resolve().parents[2] / "agent" / "media_analysis.py"
sys.path.insert(0, str(PATH.parent))                    # the agent imports it from its own folder: use the very same module object
import media_analysis as analysis                       # noqa: E402

HAVE_FFMPEG = bool(shutil.which("ffmpeg") and shutil.which("ffprobe"))


def make_media(path, *, audio=(("eng", "aac"),), subtitles=(), video="libx264", seconds=2, size="160x120", container=None, titles=None):
    """A real, tiny media file. audio is [(language, codec)], subtitles [language]; a language of None sets no language tag."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    command = ["ffmpeg", "-nostdin", "-hide_banner", "-v", "error", "-y", "-f", "lavfi", "-i", f"testsrc=duration={seconds}:size={size}:rate=10"]
    for index, _ in enumerate(audio):
        command += ["-f", "lavfi", "-i", f"sine=frequency={440 + 110 * index}:duration={seconds}"]
    for index, language in enumerate(subtitles):
        srt = path.parent / f".{path.stem}.{index}.srt"
        srt.write_text("1\n00:00:00,000 --> 00:00:01,000\nHello\n")
        command += ["-i", str(srt)]
    command += ["-map", "0:v"] + sum((["-map", f"{i + 1}:a"] for i in range(len(audio))), []) + sum((["-map", f"{len(audio) + 1 + i}:s"] for i in range(len(subtitles))), [])
    command += ["-c:v", video, "-pix_fmt", "yuv420p"]
    if video == "libx264":
        command += ["-preset", "ultrafast"]
    for index, (language, codec) in enumerate(audio):
        command += [f"-c:a:{index}", codec]
        if language:
            command += [f"-metadata:s:a:{index}", f"language={language}"]
        if titles and titles.get(("a", index)):
            command += [f"-metadata:s:a:{index}", f"title={titles[('a', index)]}"]
    ext = path.suffix.lower()
    if subtitles:
        command += ["-c:s", "srt" if ext in (".mkv", ".webm") else "mov_text"]
    for index, language in enumerate(subtitles):
        if language:
            command += [f"-metadata:s:s:{index}", f"language={language}"]
    if container:
        command += ["-f", container]
    command.append(str(path))
    done = subprocess.run(command, capture_output=True, text=True)
    if done.returncode != 0:
        raise RuntimeError(f"ffmpeg could not make {path.name}: {done.stderr}")
    for leftover in path.parent.glob(f".{path.stem}.*.srt"):
        leftover.unlink()
    return path
