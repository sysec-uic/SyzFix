"""
Extract cherry-pick backport mappings from a bare linux-stable.git clone.

For each stable branch (linux-4.14.y, linux-5.4.y, etc.), parses git log
to find commits containing "(cherry picked from commit <hash>)" and builds
a mapping from upstream commit hashes to their stable backport records.

Usage:
    python -m dataset.scraper.stable_cherrypick [--repo PATH] [--output PATH]

Requires a bare clone of linux-stable.git:
    git clone --bare git://git.kernel.org/pub/scm/linux/kernel/git/stable/linux-stable.git \
        dataset/data/raw/linux-stable.git
"""

import argparse
import json
import re
import subprocess
import sys
import time
from collections import defaultdict
from datetime import datetime
from pathlib import Path

from ..config import RAW_DIR, PROCESSED_DIR

DEFAULT_REPO_PATH = RAW_DIR / "linux-stable.git"
DEFAULT_OUTPUT_PATH = PROCESSED_DIR / "cherrypick_map.json"

CHERRY_PICK_RE = re.compile(r'\(cherry picked from commit ([0-9a-f]{40})\)')
# [ Upstream commit HASH ] — primary pattern used by stable maintainers
UPSTREAM_COMMIT_RE = re.compile(r'\[\s*[Uu]pstream\s+commit\s+([0-9a-f]{40})\s*\]')

# Delimiter unlikely to appear in commit messages
COMMIT_SEP = "---COMMIT_SEP_f7a3b2c1---"


def get_stable_branches(repo_path: Path) -> list[str]:
    """List all linux-*.y branches available in the bare repo.

    Reads packed-refs directly as a fallback since for-each-ref can
    sometimes miss packed refs in bare mirror clones.
    """
    # Try for-each-ref first
    result = subprocess.run(
        [
            "git", "-C", str(repo_path), "for-each-ref",
            "--format=%(refname)", "refs/heads/",
        ],
        capture_output=True, text=True, check=True,
    )
    branches = []
    for line in result.stdout.strip().split("\n"):
        line = line.strip()
        if re.match(r'refs/heads/linux-\d+\.\d+\.y$', line):
            branches.append(line)

    # Fallback: parse packed-refs if for-each-ref missed them
    if not branches:
        packed_refs = repo_path / "packed-refs"
        if packed_refs.exists():
            with open(packed_refs) as f:
                for line in f:
                    line = line.strip()
                    if line.startswith("#") or line.startswith("^"):
                        continue
                    parts = line.split()
                    if len(parts) == 2:
                        ref = parts[1]
                        if re.match(r'refs/heads/linux-\d+\.\d+\.y$', ref):
                            branches.append(ref)

    return sorted(branches)


def extract_cherrypicks_from_branch(
    repo_path: Path, branch: str
) -> list[dict]:
    """Extract cherry-pick records from a single stable branch.

    Returns list of {upstream_hash, stable_hash, date, branch}.
    """
    # Resolve ref to hash first (bare repos with packed-refs can have
    # issues with symbolic ref resolution in git log)
    rev = subprocess.run(
        ["git", "-C", str(repo_path), "rev-parse", branch],
        capture_output=True, text=True, check=True,
    ).stdout.strip()

    # Search for both annotation patterns (OR logic — default for
    # multiple --grep flags without --all-match):
    #   "(cherry picked from commit HASH)"  — used by git cherry-pick -x
    #   "[ Upstream commit HASH ]"          — used by stable maintainers
    result = subprocess.run(
        [
            "git", "-C", str(repo_path), "log",
            "--grep=cherry picked from commit",
            "--grep=Upstream commit",
            f"--format={COMMIT_SEP}%n%H%n%aI%n%b",
            rev,
        ],
        capture_output=True, text=True, check=True,
        timeout=600,  # 10 min per branch (more commits now)
    )

    records = []
    seen = set()  # avoid duplicates if both patterns match
    chunks = result.stdout.split(COMMIT_SEP)

    for chunk in chunks:
        chunk = chunk.strip()
        if not chunk:
            continue

        lines = chunk.split("\n", 2)
        if len(lines) < 2:
            continue

        stable_hash = lines[0].strip()
        date_str = lines[1].strip()
        body = lines[2] if len(lines) > 2 else ""

        # Try both patterns
        upstream_hashes = set()
        for m in CHERRY_PICK_RE.finditer(body):
            upstream_hashes.add(m.group(1))
        for m in UPSTREAM_COMMIT_RE.finditer(body):
            upstream_hashes.add(m.group(1))

        for upstream_hash in upstream_hashes:
            key = (upstream_hash, branch)
            if key not in seen:
                seen.add(key)
                records.append({
                    "upstream_hash": upstream_hash,
                    "stable_hash": stable_hash,
                    "date": date_str,
                    "branch": branch,
                })

    return records


def extract_upstream_dates(
    repo_path: Path, needed_hashes: set[str]
) -> dict[str, str]:
    """Extract commit dates for upstream hashes from the master branch.

    Only retains hashes present in needed_hashes for memory efficiency.
    """
    # Try various ref names for the mainline branch
    for ref in [
        "refs/heads/master", "refs/heads/main",
        "refs/remotes/origin/master", "refs/remotes/origin/main",
        "master", "main",
    ]:
        check = subprocess.run(
            ["git", "-C", str(repo_path), "rev-parse", "--verify", ref],
            capture_output=True, text=True,
        )
        if check.returncode == 0:
            master_ref = ref
            break
    else:
        print("  WARNING: No master/main branch found, skipping upstream dates")
        return {}

    print(f"  Extracting upstream dates from {master_ref}...")
    result = subprocess.run(
        [
            "git", "-C", str(repo_path), "log",
            "--format=%H %aI",
            master_ref,
        ],
        capture_output=True, text=True, check=True,
        timeout=600,
    )

    dates = {}
    for line in result.stdout.strip().split("\n"):
        if not line:
            continue
        parts = line.split(" ", 1)
        if len(parts) == 2 and parts[0] in needed_hashes:
            dates[parts[0]] = parts[1]

    return dates


def build_mapping(repo_path: Path) -> dict:
    """Build the full upstream→backports mapping from all stable branches."""
    branches = get_stable_branches(repo_path)
    if not branches:
        print("ERROR: No stable branches found. Is this a linux-stable.git bare clone?")
        sys.exit(1)

    print(f"Found {len(branches)} stable branches:")
    for b in branches:
        print(f"  {b}")

    upstream_to_backports: dict[str, list[dict]] = defaultdict(list)
    total = 0

    for branch in branches:
        t0 = time.time()
        print(f"\nProcessing {branch}...", end="", flush=True)
        try:
            records = extract_cherrypicks_from_branch(repo_path, branch)
        except subprocess.TimeoutExpired:
            print(f" TIMEOUT (skipped)")
            continue
        except subprocess.CalledProcessError as e:
            print(f" ERROR: {e} (skipped)")
            continue

        for r in records:
            upstream_to_backports[r["upstream_hash"]].append({
                "branch": r["branch"],
                "stable_hash": r["stable_hash"],
                "date": r["date"],
            })
        total += len(records)
        elapsed = time.time() - t0
        print(f" {len(records)} cherry-picks ({elapsed:.1f}s)")

    # Extract upstream commit dates for all referenced hashes
    needed_hashes = set(upstream_to_backports.keys())
    upstream_dates = extract_upstream_dates(repo_path, needed_hashes)

    mapping = {
        "metadata": {
            "extracted_at": datetime.now().isoformat(),
            "branches": branches,
            "total_cherrypicks": total,
            "unique_upstream_commits": len(upstream_to_backports),
            "upstream_dates_found": len(upstream_dates),
        },
        "upstream_to_backports": dict(upstream_to_backports),
        "upstream_dates": upstream_dates,
    }

    return mapping


def main():
    parser = argparse.ArgumentParser(
        description="Extract cherry-pick backport mappings from linux-stable.git"
    )
    parser.add_argument(
        "--repo", type=Path, default=DEFAULT_REPO_PATH,
        help=f"Path to bare linux-stable.git clone (default: {DEFAULT_REPO_PATH})"
    )
    parser.add_argument(
        "--output", type=Path, default=DEFAULT_OUTPUT_PATH,
        help=f"Output JSON path (default: {DEFAULT_OUTPUT_PATH})"
    )
    args = parser.parse_args()

    if not args.repo.exists():
        print(f"ERROR: Repo not found at {args.repo}")
        print("Clone it first:")
        print(f"  git clone --bare git://git.kernel.org/pub/scm/linux/kernel/git/stable/linux-stable.git {args.repo}")
        sys.exit(1)

    print(f"Extracting cherry-pick mappings from {args.repo}")
    mapping = build_mapping(args.repo)

    meta = mapping["metadata"]
    print(f"\n{'='*60}")
    print(f"Total cherry-picks: {meta['total_cherrypicks']}")
    print(f"Unique upstream commits: {meta['unique_upstream_commits']}")
    print(f"Upstream dates found: {meta['upstream_dates_found']}")

    args.output.parent.mkdir(parents=True, exist_ok=True)
    print(f"\nWriting to {args.output}...")
    with open(args.output, "w") as f:
        json.dump(mapping, f)

    size_mb = args.output.stat().st_size / (1024 * 1024)
    print(f"Done ({size_mb:.1f} MB)")


if __name__ == "__main__":
    main()
