"""Keep the Render worker available for staging seed upload before enabling jobs."""
import os
import sys
import time
from pathlib import Path


def main():
    seed = Path(os.environ["RAWDATA_DIR"]).parent / ".staging-seed-ready"
    print(f"Waiting for staging seed marker {seed}", flush=True)
    while not seed.is_file():
        time.sleep(5)
    print("Starting live KRX and DART staging jobs", flush=True)
    args = [sys.executable, "-m", "data_pipeline.scheduled_refresh", "--staging"]
    os.execv(sys.executable, args)


if __name__ == "__main__":
    main()
