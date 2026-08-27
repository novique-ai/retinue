#!/usr/bin/env python3
"""Track B test sidecar: OpenAI-compatible STT + TTS on a LAN GPU host.

Run NEXT TO Glimmer — do not touch llama-server :8080.

    RETINUE_VOICE_WHISPER=/path/to/whisper-cli \\
    RETINUE_VOICE_WHISPER_MODEL=/path/to/ggml-large-v3-turbo.bin \\
    RETINUE_VOICE_PIPER=/path/to/piper \\
    RETINUE_VOICE_PIPER_MODEL=/path/to/en_US-lessac-medium.onnx \\
    python3 voice_sidecar.py --host 0.0.0.0 --port 8104

STT:  POST /v1/audio/transcriptions  (multipart file=)
TTS:  POST /v1/audio/speech          ({input, voice})
Health: GET /health

TTS falls back to espeak-ng when piper is not configured.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import tempfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

DEFAULT_HOST = os.getenv("RETINUE_VOICE_SIDECAR_HOST", "127.0.0.1")
DEFAULT_PORT = int(os.getenv("RETINUE_VOICE_SIDECAR_PORT", "8104"))


def _env_path(name: str) -> str:
    return (os.getenv(name) or "").strip()


def whisper_bin() -> str:
    return _env_path("RETINUE_VOICE_WHISPER") or shutil.which("whisper-cli") or ""


def whisper_model() -> str:
    return _env_path("RETINUE_VOICE_WHISPER_MODEL")


def piper_bin() -> str:
    return _env_path("RETINUE_VOICE_PIPER") or shutil.which("piper") or ""


def piper_model() -> str:
    return _env_path("RETINUE_VOICE_PIPER_MODEL")


def espeak_bin() -> str:
    return shutil.which("espeak-ng") or shutil.which("espeak") or ""


def status() -> dict:
    wbin, wmodel = whisper_bin(), whisper_model()
    pbin, pmodel = piper_bin(), piper_model()
    tts = "piper" if pbin and pmodel else ("espeak" if espeak_bin() else "missing")
    return {
        "ok": bool(wbin and wmodel and tts != "missing"),
        "stt": {"bin": wbin, "model": wmodel, "ready": bool(wbin and wmodel)},
        "tts": {"engine": tts, "piper": pbin, "piper_model": pmodel, "espeak": espeak_bin()},
    }


def transcribe(path: str) -> str:
    bin_path, model = whisper_bin(), whisper_model()
    if not bin_path or not model:
        raise RuntimeError("whisper-cli or model not configured")
    outdir = tempfile.mkdtemp(prefix="retinue-whisper-")
    try:
        cmd = [
            bin_path,
            "-m",
            model,
            "-f",
            path,
            "-otxt",
            "-of",
            os.path.join(outdir, "out"),
            "-np",
            "-nt",
        ]
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            stdin=subprocess.DEVNULL,
            timeout=180,
        )
        txt = Path(outdir) / "out.txt"
        if txt.is_file():
            return txt.read_text(encoding="utf-8", errors="replace").strip()
        # Some builds print the transcript to stdout instead.
        if proc.returncode == 0 and proc.stdout.strip():
            return proc.stdout.strip()
        raise RuntimeError(
            f"whisper failed rc={proc.returncode}: {(proc.stderr or proc.stdout)[:400]}"
        )
    finally:
        shutil.rmtree(outdir, ignore_errors=True)


def synthesize(text: str, voice: str = "") -> bytes:
    pbin, pmodel = piper_bin(), piper_model()
    if pbin and pmodel:
        return _piper(text, pbin, pmodel)
    es = espeak_bin()
    if es:
        return _espeak(text, es, voice)
    raise RuntimeError("no TTS engine (piper or espeak-ng)")


def _piper(text: str, bin_path: str, model: str) -> bytes:
    with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as fh:
        out = fh.name
    try:
        proc = subprocess.run(
            [bin_path, "--model", model, "--output_file", out],
            input=text.encode("utf-8"),
            capture_output=True,
            timeout=60,
        )
        if proc.returncode != 0 or not os.path.isfile(out):
            raise RuntimeError(f"piper failed: {proc.stderr[:300]!r}")
        return Path(out).read_bytes()
    finally:
        try:
            os.unlink(out)
        except OSError:
            pass


def _espeak(text: str, bin_path: str, voice: str) -> bytes:
    with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as fh:
        out = fh.name
    try:
        cmd = [bin_path, "-w", out, "-s", "165"]
        if voice:
            cmd.extend(["-v", "en"])
        cmd.append(text)
        proc = subprocess.run(
            cmd,
            capture_output=True,
            stdin=subprocess.DEVNULL,
            timeout=30,
        )
        if proc.returncode != 0 or not os.path.isfile(out):
            raise RuntimeError(f"espeak failed: {proc.stderr[:300]!r}")
        return Path(out).read_bytes()
    finally:
        try:
            os.unlink(out)
        except OSError:
            pass


def _parse_multipart(handler: BaseHTTPRequestHandler) -> dict:
    """Minimal multipart parser for a single ``file`` field."""
    ctype = handler.headers.get("Content-Type") or ""
    length = int(handler.headers.get("Content-Length") or "0")
    body = handler.rfile.read(length) if length else b""
    if "multipart/form-data" not in ctype:
        return {"file": body, "filename": "speech.wav"}
    boundary = b""
    for part in ctype.split(";"):
        part = part.strip()
        if part.lower().startswith("boundary="):
            boundary = part.split("=", 1)[1].strip().strip('"').encode()
    if not boundary:
        return {"file": body, "filename": "speech.wav"}
    marker = b"--" + boundary
    filename = "speech.wav"
    file_bytes = b""
    for chunk in body.split(marker):
        chunk = chunk.lstrip(b"\r\n")
        if not chunk or chunk.startswith(b"--"):
            continue
        head, _, data = chunk.partition(b"\r\n\r\n")
        data = data.rstrip(b"\r\n")
        header = head.decode("utf-8", errors="replace")
        if "filename=" in header:
            for token in header.replace("\r", "\n").split("\n"):
                if "filename=" in token:
                    filename = token.split("filename=", 1)[1].strip().strip('"') or filename
            file_bytes = data
            break
    return {"file": file_bytes, "filename": filename}


class _Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        print("sidecar:", fmt % args)

    def _json(self, status: int, payload: dict) -> None:
        raw = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def _bytes(self, status: int, payload: bytes, ctype: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def do_GET(self):
        path = urlparse(self.path).path
        if path in ("/health", "/v1/health"):
            return self._json(200, status())
        if path in ("/v1/models", "/models"):
            return self._json(200, {"data": [{"id": "whisper-1"}, {"id": "tts-1"}]})
        return self._json(404, {"error": "not found"})

    def do_POST(self):
        path = urlparse(self.path).path
        if path in ("/v1/audio/transcriptions", "/audio/transcriptions"):
            parsed = _parse_multipart(self)
            data = parsed.get("file") or b""
            name = parsed.get("filename") or "speech.wav"
            if not data:
                return self._json(400, {"error": "missing file"})
            suffix = Path(name).suffix or ".wav"
            fd, tmp = tempfile.mkstemp(prefix="retinue-in-", suffix=suffix)
            try:
                os.write(fd, data)
                os.close(fd)
                fd = -1
                text = transcribe(tmp)
            except Exception as e:
                return self._json(502, {"error": str(e)})
            finally:
                if fd >= 0:
                    os.close(fd)
                try:
                    os.unlink(tmp)
                except OSError:
                    pass
            return self._json(200, {"text": text})
        if path in ("/v1/audio/speech", "/audio/speech"):
            length = int(self.headers.get("Content-Length") or "0")
            try:
                body = json.loads(self.rfile.read(length).decode("utf-8")) if length else {}
            except ValueError:
                return self._json(400, {"error": "invalid json"})
            text = str(body.get("input") or body.get("text") or "")
            voice = str(body.get("voice") or "")
            if not text.strip():
                return self._json(400, {"error": "empty input"})
            try:
                audio = synthesize(text, voice)
            except Exception as e:
                return self._json(502, {"error": str(e)})
            ctype = "audio/wav" if audio[:4] == b"RIFF" else "audio/mpeg"
            return self._bytes(200, audio, ctype)
        return self._json(404, {"error": "not found"})


def main() -> int:
    parser = argparse.ArgumentParser(description="Retinue Track B voice sidecar")
    parser.add_argument("--host", default=DEFAULT_HOST)
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    args = parser.parse_args()
    print("voice sidecar status:", json.dumps(status()))
    httpd = ThreadingHTTPServer((args.host, args.port), _Handler)
    print(f"listening on {args.host}:{args.port}")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
