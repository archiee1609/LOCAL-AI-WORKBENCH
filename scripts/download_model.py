import os
import sys
import time
from pathlib import Path
import httpx

SD_TURBO_URL = "https://huggingface.co/Blib-la/sd-turbo-fp16/resolve/main/sd-turbo-fp16-emaoonly.safetensors"
ROOT_DIR = Path(__file__).resolve().parent.parent
TARGET_DIR = ROOT_DIR / "ComfyUI" / "models" / "checkpoints"
TARGET_DIR.mkdir(parents=True, exist_ok=True)
DEST_FILE = TARGET_DIR / "sd_turbo.safetensors"
TEMP_FILE = TARGET_DIR / "sd_turbo.safetensors.tmp"

def download_model():
    print(f"Target file: {DEST_FILE}")
    if DEST_FILE.exists() and DEST_FILE.stat().st_size > 1024 * 1024 * 1000:
        print(f"Model already downloaded: {DEST_FILE.stat().st_size / (1024*1024):.1f} MB")
        return

    existing_bytes = TEMP_FILE.stat().st_size if TEMP_FILE.exists() else 0
    headers = {"Range": f"bytes={existing_bytes}-"} if existing_bytes > 0 else {}

    print(f"Connecting to {SD_TURBO_URL} (resuming from {existing_bytes / (1024*1024):.1f} MB)...")
    with httpx.Client(timeout=3600.0, follow_redirects=True) as client:
        with client.stream("GET", SD_TURBO_URL, headers=headers) as response:
            if response.status_code not in (200, 206):
                print(f"Error: HTTP {response.status_code}")
                sys.exit(1)

            total_bytes = existing_bytes + int(response.headers.get("content-length", 0))
            downloaded = existing_bytes
            last_report = time.time()

            mode = "ab" if existing_bytes > 0 else "wb"
            with open(TEMP_FILE, mode) as f:
                for chunk in response.iter_bytes(chunk_size=1024 * 1024 * 2): # 2MB chunks
                    f.write(chunk)
                    downloaded += len(chunk)
                    now = time.time()
                    if now - last_report > 5.0 or downloaded >= total_bytes:
                        pct = (downloaded / total_bytes) * 100 if total_bytes > 0 else 0
                        mb_dl = downloaded / (1024 * 1024)
                        mb_tot = total_bytes / (1024 * 1024)
                        print(f"Progress: {pct:.1f}% ({mb_dl:.1f} MB / {mb_tot:.1f} MB)")
                        last_report = now

    TEMP_FILE.rename(DEST_FILE)
    print(f"Successfully downloaded and installed: {DEST_FILE}")

if __name__ == "__main__":
    download_model()
