#!/usr/bin/env python3
"""
Download processed.jsonl.gz from HF and restore data/processed/*.json.

Usage:
    python -m dataset.restore_processed --repo yourname/syzfix-dataset
    python -m dataset.restore_processed --repo yourname/syzfix-dataset --out /custom/path
"""
import argparse, gzip, json, sys
from pathlib import Path

# Default output is always next to this script, regardless of working directory
_DEFAULT_OUT = Path(__file__).parent / "data" / "processed"

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo", required=True)
    parser.add_argument("--out", default=str(_DEFAULT_OUT),
                        help=f"Output directory (default: {_DEFAULT_OUT})")
    args = parser.parse_args()

    try:
        from huggingface_hub import hf_hub_download
    except ImportError:
        sys.exit("pip install huggingface_hub")

    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)

    print(f"Downloading processed.jsonl.gz from {args.repo} ...")
    gz_path = hf_hub_download(
        repo_id=args.repo,
        filename="processed/processed.jsonl.gz",
        repo_type="dataset",
    )

    print(f"Unpacking into {out_dir} ...")
    with gzip.open(gz_path, "rt", encoding="utf-8") as gz:
        for i, line in enumerate(gz, 1):
            obj = json.loads(line)
            bug_id = obj.get("bug_id", f"unknown_{i:06d}")
            (out_dir / f"{bug_id}.json").write_text(
                json.dumps(obj, indent=2, ensure_ascii=False)
            )
            if i % 500 == 0:
                print(f"  {i} records restored ...")

    print(f"Done. {i} records written to {out_dir}/")

if __name__ == "__main__":
    main()
