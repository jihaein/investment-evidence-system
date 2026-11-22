"""Build the repository's six-CSV preprocessing release for data-team review."""
import argparse
import os
import sys
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[2]
if not __package__:
    sys.path.insert(0, str(ROOT))

from seoseohi_preprocess import preprocess


def main():
    load_dotenv(ROOT / ".env")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--raw-dir", type=Path)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--strict", action="store_true")
    args = parser.parse_args()
    preprocess(args.raw_dir or Path(os.environ["RAWDATA_DIR"]), args.out, strict=args.strict)
    print(f"Preprocessing release created for review: {args.out}")


if __name__ == "__main__":
    main()
