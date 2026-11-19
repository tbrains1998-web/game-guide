"""Read in-game text out loud: screenshot -> text (Claude vision or Windows OCR) -> natural voice.

Voices: Microsoft neural voices through edge-tts (free, no key, no GPU). Falls back to Kokoro offline.
"""

import asyncio
import json
import re
import subprocess
import threading
from pathlib import Path

import numpy as np

VOICES = [  # (id, label) - a short list of the most natural English voices
    ("en-US-AndrewMultilingualNeural", "Andrew (US, warm)"),
    ("en-US-AvaMultilingualNeural", "Ava (US)"),
    ("en-US-BrianMultilingualNeural", "Brian (US, casual)"),
    ("en-US-EmmaMultilingualNeural", "Emma (US)"),
    ("en-GB-RyanNeural", "Ryan (UK)"),
    ("en-GB-SoniaNeural", "Sonia (UK)"),
    ("en-IE-ConnorNeural", "Connor (Irish)"),
    ("en-US-GuyNeural", "Guy (US, narrator)"),
]


# ------------------------------------------------------------------ text from the screen
def ocr_windows(image_path: str) -> str:
    """Windows' built-in OCR (instant, CPU). Good on clean fonts, weaker on handwriting.

    Runs in a separate process: the winrt package ships its own older MSVCP140.dll, and once that is
    loaded into the app, faster-whisper (ctranslate2) crashes on it (access violation) when F10 loads it."""
    import sys
    exe = Path(sys.executable).with_name("python.exe")
    r = subprocess.run([str(exe), str(Path(__file__).resolve()), "--ocr", str(image_path)],
                       capture_output=True, text=True, encoding="utf-8", timeout=30,
                       creationflags=subprocess.CREATE_NO_WINDOW)
    if r.returncode != 0:
        raise RuntimeError((r.stderr or "OCR process failed").strip().splitlines()[-1])
    return json.loads(r.stdout)["text"]


def _ocr_in_this_process(image_path: str) -> str:
    from PIL import Image
    from winrt.windows.graphics.imaging import BitmapPixelFormat, SoftwareBitmap
    from winrt.windows.media.ocr import OcrEngine
    from winrt.windows.storage.streams import DataWriter

    img = Image.open(image_path).convert("RGBA")
    # OCR works best at roughly 1500-2500 px wide.
    if img.width < 1500:
        img = img.resize((img.width * 2, img.height * 2))
    b, g, r, a = img.split()[2], img.split()[1], img.split()[0], img.split()[3]
    bgra = Image.merge("RGBA", (b, g, r, a)).tobytes()
    w = DataWriter()
    w.write_bytes(bgra)
    bmp = SoftwareBitmap.create_copy_from_buffer(w.detach_buffer(), BitmapPixelFormat.BGRA8, img.width, img.height)
    engine = OcrEngine.try_create_from_user_profile_languages()

    async def run():
        return await engine.recognize_async(bmp)
    res = asyncio.run(run())
    lines = [l.text for l in res.lines]
    # Drop typical HUD/controls noise: very short lines and button prompts.
    keep = [l for l in lines if len(l) > 2 and not re.match(r"^\W?[A-Z]{1,3}\W?\s+(Back|Close|Select|Zoom|Turn|Exit|Next|Rotate)", l)]
    return "\n".join(keep).strip()


def looks_readable(text: str | None) -> bool:
    """Is the OCR result real text (vs. garbage from a handwritten / stylised font)?"""
    if not text or len(text) < 12:
        return False
    words = re.findall(r"\S+", text)
    good = [w for w in words if re.fullmatch(r"[\"'(]?[A-Za-z][a-z']*[A-Za-z]?[.,;:!?\"')]*|\d+[.,]?|[AaI][.,]?", w)
            and (re.search(r"[aeiouyAEIOUY]", w) or len(w) <= 3 or w[0].isdigit())]
    return len(words) >= 3 and len(good) / len(words) >= 0.75


READ_PROMPT = (
    "The image at {path} is a screenshot from the video game {game}. The player wants the in-game text read "
    "out loud to them. Use Read to view it, then output ONLY the readable story/in-game text - notes, letters, "
    "documents, diaries, signs, posters, subtitles, dialogue, item or file descriptions - exactly as written, "
    "in natural reading order, fixing only obvious line-break hyphenation. Leave out HUD, button prompts, "
    "controls, menus, map labels, and anything that isn't meant to be read. No comments, no quotes around it, "
    "no intro. If there is no such text, output exactly: NONE")


def text_via_claude(exe: str, image_path: str, game: str, model: str = "haiku", timeout: int = 90) -> str | None:
    """Claude reads the screenshot (handles handwriting / stylised fonts, skips UI). No GPU."""
    import brains                     # here, not at the top: brains is only needed for this Claude run
    cmd = [exe, "-p", READ_PROMPT.format(path=image_path, game=game or "a video game"),
           "--output-format", "json", "--model", model, "--allowedTools", "Read",
           "--disallowedTools", "Bash,Edit,Write,WebSearch,WebFetch,NotebookEdit,Agent", "--strict-mcp-config",
           "--settings", brains.no_plugins_settings()]
    r = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", timeout=timeout,
                       cwd=str(Path(image_path).parent), creationflags=subprocess.CREATE_NO_WINDOW)
    d = json.loads(r.stdout)
    if d.get("is_error"):
        raise RuntimeError(d.get("result"))
    text = (d.get("result") or "").strip()
    return None if text.upper().strip(". ") == "NONE" else text


# ------------------------------------------------------------------ speaking
def chunk_text(text: str, max_len: int = 220) -> list[str]:
    """Sentence groups of ~max_len chars so the first one starts playing quickly."""
    text = re.sub(r"[ \t]+", " ", text)
    sents = re.split(r"(?<=[.!?…])\s+|\n{2,}", text)
    out, cur = [], ""
    for s in sents:
        s = s.replace("\n", " ").strip()
        if not s:
            continue
        if cur and len(cur) + len(s) > max_len:
            out.append(cur)
            cur = ""
        cur = (cur + " " + s).strip()
    if cur:
        out.append(cur)
    return out


def kokoro_paths(models_dir=None) -> tuple[Path, Path]:
    """(kokoro-v1.0.onnx, voices-v1.0.bin) in models_dir, or in models\\ next to this file when it is not set."""
    m = Path(models_dir) if models_dir else Path(__file__).resolve().parent / "models"
    return m / "kokoro-v1.0.onnx", m / "voices-v1.0.bin"


class Speaker:
    """Speaks text with an edge-tts neural voice; synthesises the next chunk while the current one plays.
    stop() interrupts immediately."""

    def __init__(self, log=print, models_dir=None):
        self.log = log
        self.models_dir = models_dir     # Kokoro model folder (None/"" = models\ next to this file)
        self.stop_flag = threading.Event()
        self.lock = threading.Lock()
        self.speaking = False
        self.gen = 0                     # every say()/stop() bumps it; older say() calls then go quiet
        self._gen_lock = threading.Lock()
        self._kokoro = None

    def stop(self):
        with self._gen_lock:
            self.gen += 1
        self.stop_flag.set()
        try:
            import sounddevice as sd
            sd.stop()
        except Exception:
            pass

    def _edge_audio(self, text: str, voice: str, rate: str) -> np.ndarray:
        import edge_tts
        import miniaudio

        async def synth():
            buf = b""
            async for ch in edge_tts.Communicate(text, voice, rate=rate).stream():
                if ch["type"] == "audio":
                    buf += ch["data"]
            return buf
        mp3 = asyncio.run(synth())
        d = miniaudio.decode(mp3, output_format=miniaudio.SampleFormat.FLOAT32, nchannels=1, sample_rate=24000)
        return np.frombuffer(d.samples, dtype=np.float32)

    def _kokoro_audio(self, text: str) -> np.ndarray:
        if self._kokoro is None:
            from kokoro_onnx import Kokoro
            onnx, voices = kokoro_paths(self.models_dir)
            if not (onnx.is_file() and voices.is_file()):
                raise FileNotFoundError(f"offline voice model not found: put kokoro-v1.0.onnx and voices-v1.0.bin "
                                        f"in {onnx.parent}")
            self._kokoro = Kokoro(str(onnx), str(voices))
        samples, sr = self._kokoro.create(text, voice="af_heart", speed=1.05, lang="en-us")
        if sr != 24000:
            samples = np.interp(np.arange(0, len(samples), sr / 24000), np.arange(len(samples)), samples)
        return samples.astype(np.float32)

    def say(self, text: str, voice: str, rate: str = "+0%"):
        """Blocking. Call from a worker thread. A newer say() or stop() replaces this one - calls
        never queue up behind each other (that read whole answers after a few F7 presses)."""
        import sounddevice as sd
        from concurrent.futures import ThreadPoolExecutor
        with self._gen_lock:
            self.gen += 1
            my = self.gen
        self.stop_flag.set()                 # cut off whatever is playing now
        try:
            sd.stop()
        except Exception:
            pass
        with self.lock:
            if my != self.gen:               # replaced while waiting for the previous one to finish
                return
            self.stop_flag.clear()
            self.speaking = True
            try:
                chunks = chunk_text(text)
                if not chunks:
                    return
                use_edge = True

                def synth(c):
                    nonlocal use_edge
                    if use_edge:
                        try:
                            return self._edge_audio(c, voice, rate)
                        except Exception as e:
                            self.log(f"edge-tts failed ({e}); using offline Kokoro voice")
                            use_edge = False
                    return self._kokoro_audio(c)

                with ThreadPoolExecutor(1) as ex:
                    nxt = ex.submit(synth, chunks[0])
                    for i in range(len(chunks)):
                        audio = nxt.result()
                        if i + 1 < len(chunks):
                            nxt = ex.submit(synth, chunks[i + 1])
                        if self.stop_flag.is_set() or my != self.gen:
                            break
                        sd.play(audio, 24000)
                        sd.wait()
                        if self.stop_flag.is_set() or my != self.gen:
                            break
            finally:
                self.speaking = False


if __name__ == "__main__":
    import sys
    if len(sys.argv) == 3 and sys.argv[1] == "--ocr":
        print(json.dumps({"text": _ocr_in_this_process(sys.argv[2])}))
