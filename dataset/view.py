#!/usr/bin/env python3
"""
Interactive dataset viewer for SyzFix syzbot dataset.

Usage:
    python view.py build-index                 # build fast-lookup index (run once)
    python view.py list                        # list all collected bugs  (fast)
    python view.py list --has-reproducer       # only bugs with C reproducer
    python view.py list --subsystem net        # filter by keyword in title
    python view.py list --has-evolution        # only bugs with v1→v2+ patches
    python view.py search <keyword>            # search titles + crash summaries (fast)
    python view.py search <keyword> --deep     # search full crash reports (slow)
    python view.py show <bug_id>               # full details for one bug
    python view.py crash <bug_id>             # just the crash report + C reproducer
    python view.py patch <bug_id>             # final patch diff
    python view.py discuss <bug_id>           # discussion thread
    python view.py diff <bug_id>              # side-by-side v1 vs final patch
    python view.py random                     # show a random interesting bug
    python view.py list --cross-layer         # only cross-layer bugs (466)
    python view.py list --cross-layer --verify-stack  # show on/off-stack status
    python view.py list --true-cross-layer    # fix NOT on crash stack (130, hardest)
    python view.py crosslayer <bug_id>        # cross-layer analysis for one bug
    python view.py stats                      # dataset & cross-layer statistics

Note: 'list' and 'search' use a lightweight index (~5MB) for speed.
      Run 'build-index' after collecting new bugs to keep it current.
"""

from __future__ import annotations

import json
import random
import sys
from pathlib import Path

import click

PROCESSED_DIR = Path(__file__).parent / "data" / "processed"
INDEX_FILE = Path(__file__).parent / "data" / "index.jsonl"
RESULTS_DIR = Path(__file__).parent.parent / "analysis" / "results"

# Fields extracted from each full bug file into the lightweight index.
# crash_summary stores the first 300 chars of the crash report for search.
_INDEX_FIELDS = (
    "bug_id", "title", "fix_time",
    "n_patch_versions", "has_patch", "has_discussion",
    "has_c_reproducer", "crash_title", "crash_summary",
)


# ─── helpers ──────────────────────────────────────────────────────────────────

def load_bug(bug_id: str) -> dict | None:
    p = PROCESSED_DIR / f"{bug_id}.json"
    if not p.exists():
        click.echo(f"[error] Bug not found: {bug_id}", err=True)
        return None
    return json.loads(p.read_text())


def _make_index_record(b: dict) -> dict:
    """Extract lightweight metadata from a full bug dict."""
    crashes = b.get("crashes", [])
    c0 = crashes[0] if crashes else {}
    return {
        "bug_id": b.get("bug_id", ""),
        "title": b.get("title", ""),
        "fix_time": b.get("fix_time", ""),
        "n_patch_versions": len(b.get("patch_versions", [])),
        "has_patch": any(fc.get("patch_diff") for fc in b.get("fix_commits", [])),
        "has_discussion": any(
            d.get("messages") for d in b.get("discussions", [])
            if not d.get("is_syzbot_report")
        ),
        "has_c_reproducer": any(
            c.get("c_reproducer") and c.get("kernel_commit") and c.get("kernel_config_link")
            for c in crashes
        ),
        "crash_title": c0.get("title", ""),
        "crash_summary": c0.get("crash_report", "")[:300],
    }


def build_index(show_progress: bool = True) -> int:
    """Scan all processed bug files and write a lightweight index.

    Returns the number of bugs indexed.
    """
    files = sorted(PROCESSED_DIR.glob("*.json"))
    if show_progress:
        click.echo(f"Building index from {len(files)} bug files...", err=True)

    INDEX_FILE.parent.mkdir(parents=True, exist_ok=True)
    count = 0
    with open(INDEX_FILE, "w") as out:
        for i, p in enumerate(files):
            try:
                b = json.loads(p.read_text())
                out.write(json.dumps(_make_index_record(b)) + "\n")
                count += 1
            except Exception:
                continue
            if show_progress and (i + 1) % 500 == 0:
                click.echo(f"  {i + 1}/{len(files)}...", err=True)

    if show_progress:
        click.echo(f"Index written to {INDEX_FILE} ({count} bugs)", err=True)
    return count


def _load_index() -> list[dict] | None:
    """Load the index if it exists. Returns None if missing."""
    if not INDEX_FILE.exists():
        return None
    records = []
    with open(INDEX_FILE) as f:
        for line in f:
            line = line.strip()
            if line:
                try:
                    records.append(json.loads(line))
                except Exception:
                    continue
    return records


def _ensure_index() -> list[dict]:
    """Return index records, auto-building the index if it doesn't exist yet."""
    records = _load_index()
    if records is None:
        click.echo(
            "Index not found — building it now (one-time, ~10s). "
            "Run 'python view.py build-index' to rebuild manually.",
            err=True,
        )
        build_index(show_progress=True)
        records = _load_index() or []
    return records


def all_bugs() -> list[dict]:
    """Load all full bug dicts (slow — only used by show/crash/patch/discuss/diff)."""
    bugs = []
    for p in sorted(PROCESSED_DIR.glob("*.json")):
        try:
            bugs.append(json.loads(p.read_text()))
        except Exception:
            continue
    return bugs


def sep(char="─", width=72):
    click.echo(char * width)


def header(title: str):
    sep("═")
    click.echo(f"  {title}")
    sep("═")


def section(title: str):
    click.echo()
    sep()
    click.echo(f"  {title}")
    sep()


def truncate(text: str, max_chars: int = 2000) -> str:
    if len(text) <= max_chars:
        return text
    return text[:max_chars] + f"\n... [{len(text) - max_chars} more chars, use --full to see all]"


# ─── Cross-layer helpers ─────────────────────────────────────────────────────

def _load_cross_layer_results() -> dict | None:
    """Load cross-layer analyzer results. Returns None if not available."""
    result_file = RESULTS_DIR / "cross-layer_analysis" / "result.json"
    if not result_file.exists():
        return None
    try:
        return json.loads(result_file.read_text())
    except Exception:
        return None


def _cross_layer_bug_ids(domain: str = "", stack_overlap: str = "") -> set[str] | None:
    """Return set of cross-layer bug IDs, optionally filtered by domain/stack_overlap.

    stack_overlap: "fix_on_stack" or "fix_off_stack" to filter, empty for all.
    Returns None if results are not available (caller should print error).
    """
    data = _load_cross_layer_results()
    if data is None:
        return None
    ids = set()
    for d in data.get("details", []):
        if not d.get("is_cross_layer"):
            continue
        if domain and d.get("domain", "") != domain:
            continue
        if stack_overlap and d.get("stack_overlap", "") != stack_overlap:
            continue
        ids.add(d["bug_id"])
    return ids


def _cross_layer_overlap_map() -> dict[str, str] | None:
    """Return {bug_id: stack_overlap} for all cross-layer bugs."""
    data = _load_cross_layer_results()
    if data is None:
        return None
    return {
        d["bug_id"]: d.get("stack_overlap", "unknown")
        for d in data.get("details", [])
        if d.get("is_cross_layer")
    }


def _cross_layer_detail(bug_id: str) -> dict | None:
    """Get cross-layer detail for a specific bug from saved results."""
    data = _load_cross_layer_results()
    if data is None:
        return None
    for d in data.get("details", []):
        if d.get("bug_id") == bug_id:
            return d
    return None


# ─── CLI ──────────────────────────────────────────────────────────────────────

@click.group()
def cli():
    """SyzFix dataset viewer."""
    pass


@cli.command("list")
@click.option("--subsystem", "-s", default="", help="Filter by keyword in bug title")
@click.option("--has-patch", is_flag=True, help="Only bugs with a final patch diff")
@click.option("--has-evolution", is_flag=True, help="Only bugs with v1→v2+ patch iterations")
@click.option("--has-discussion", is_flag=True, help="Only bugs with reviewer emails")
@click.option("--has-reproducer", "-r", is_flag=True,
              help="Only bugs with a C reproducer (needed for crash reproduction)")
@click.option("--limit", "-n", default=30, help="Max entries to show (default 30)")
@click.option("--rebuild-index", is_flag=True, help="Rebuild the index before listing")
@click.option("--cross-layer", is_flag=True,
              help="Only cross-layer bugs (requires: python -m analysis.run_all --analyzer crosslayer)")
@click.option("--cross-layer-domain", default="",
              help="Filter cross-layer bugs by domain (filesystem, networking, block, device, ...)")
@click.option("--verify-stack", is_flag=True,
              help="Show stack overlap status for cross-layer bugs")
@click.option("--true-cross-layer", is_flag=True,
              help="Only cross-layer bugs where fix is NOT on crash stack (hardest cases)")
def cmd_list(subsystem, has_patch, has_evolution, has_discussion, has_reproducer, limit,
             rebuild_index, cross_layer, cross_layer_domain, verify_stack, true_cross_layer):
    """List collected bugs with one-line summaries (fast, uses index)."""
    if rebuild_index:
        build_index()

    bugs = _ensure_index()

    # Apply filters (all fields are precomputed in the index)
    if subsystem:
        bugs = [b for b in bugs if subsystem.lower() in b.get("title", "").lower()]
    if has_patch:
        bugs = [b for b in bugs if b.get("has_patch")]
    if has_evolution:
        bugs = [b for b in bugs if b.get("n_patch_versions", 0) > 1]
    if has_discussion:
        bugs = [b for b in bugs if b.get("has_discussion")]
    if has_reproducer:
        bugs = [b for b in bugs if b.get("has_c_reproducer")]

    if cross_layer or cross_layer_domain or true_cross_layer:
        stack_filter = "fix_off_stack" if true_cross_layer else ""
        cl_ids = _cross_layer_bug_ids(domain=cross_layer_domain, stack_overlap=stack_filter)
        if cl_ids is None:
            click.echo(
                "[error] Cross-layer results not found. Run first:\n"
                "  python -m analysis.run_all --analyzer crosslayer",
                err=True,
            )
            return
        bugs = [b for b in bugs if b.get("bug_id") in cl_ids]

    # Load overlap map if verify-stack is requested
    overlap_map = None
    if verify_stack or true_cross_layer:
        overlap_map = _cross_layer_overlap_map()

    click.echo(f"\nFound {len(bugs)} bugs (showing first {min(limit, len(bugs))}):\n")

    if verify_stack and overlap_map:
        click.echo(f"{'BUG ID':<24} {'V':<3} {'P':<3} {'R':<3} {'D':<3} {'STACK':<10} {'TITLE'}")
        click.echo(f"{'─'*24} {'─'*3} {'─'*3} {'─'*3} {'─'*3} {'─'*10} {'─'*40}")
    else:
        click.echo(f"{'BUG ID':<24} {'V':<3} {'P':<3} {'R':<3} {'D':<3} {'TITLE'}")
        click.echo(f"{'─'*24} {'─'*3} {'─'*3} {'─'*3} {'─'*3} {'─'*45}")

    for b in bugs[:limit]:
        bug_id = b.get("bug_id", "")[:22]
        title  = b.get("title", "")[:52] if not (verify_stack and overlap_map) else b.get("title", "")[:42]
        v_str  = str(b.get("n_patch_versions") or "·")
        has_p  = "✓" if b.get("has_patch") else "·"
        has_r  = "✓" if b.get("has_c_reproducer") else "·"
        has_d  = "✓" if b.get("has_discussion") else "·"
        if verify_stack and overlap_map:
            full_id = b.get("bug_id", "")
            ol = overlap_map.get(full_id, "n/a")
            ol_str = "OFF-STACK" if ol == "fix_off_stack" else "on-stack" if ol == "fix_on_stack" else ol
            click.echo(f"{bug_id:<24} {v_str:<3} {has_p:<3} {has_r:<3} {has_d:<3} {ol_str:<10} {title}")
        else:
            click.echo(f"{bug_id:<24} {v_str:<3} {has_p:<3} {has_r:<3} {has_d:<3} {title}")

    click.echo(f"\nV=patch versions, P=has patch diff, R=has C reproducer, D=has discussion")
    if verify_stack:
        click.echo(f"STACK: OFF-STACK=fix not on crash stack (true cross-layer), on-stack=fix reachable from stack")


@cli.command("show")
@click.argument("bug_id")
@click.option("--full", is_flag=True, help="Show full text without truncation")
def cmd_show(bug_id, full):
    """Show complete lifecycle for one bug."""
    b = load_bug(bug_id)
    if not b:
        return

    max_chars = 999999 if full else 2000

    header(f"Bug: {b.get('title', '')}")
    click.echo(f"  ID:          {b.get('bug_id')}")
    click.echo(f"  Status:      {b.get('status', '')[:80]}")
    click.echo(f"  First crash: {b.get('first_crash', '')}")
    click.echo(f"  Fix time:    {b.get('fix_time', '')}")

    # Fix commits
    section("FIX COMMITS")
    for fc in b.get("fix_commits", []):
        click.echo(f"  Hash:    {fc.get('hash', '(none)')}")
        click.echo(f"  Title:   {fc.get('title', '')}")
        click.echo(f"  Author:  {fc.get('author_name', '')} <{fc.get('author', '')}>")
        click.echo(f"  Date:    {fc.get('date', '')}")
        click.echo(f"  Patch:   {'yes (' + str(len(fc.get('patch_diff',''))) + ' chars)' if fc.get('patch_diff') else 'MISSING'}")
        click.echo()

    # Crash summary
    section("CRASH REPORT")
    crashes = b.get("crashes", [])
    if crashes:
        c = crashes[0]
        click.echo(truncate(c.get("crash_report", "(none)"), max_chars))
    else:
        click.echo("  (no crash data)")

    # Reproducers
    section("REPRODUCERS")
    c = crashes[0] if crashes else {}
    click.echo(f"  C reproducer:   {'yes (' + str(len(c.get('c_reproducer',''))) + ' chars)' if c.get('c_reproducer') else 'none'}")
    click.echo(f"  syz reproducer: {'yes (' + str(len(c.get('syz_reproducer',''))) + ' chars)' if c.get('syz_reproducer') else 'none'}")

    # Patch evolution
    section(f"PATCH EVOLUTION ({len(b.get('patch_versions', []))} versions)")
    for pv in b.get("patch_versions", []):
        click.echo(f"\n  ── v{pv['version']}: {pv.get('subject', '')[:65]}")
        click.echo(f"     Diff: {'yes (' + str(len(pv.get('diff',''))) + ' chars)' if pv.get('diff') else 'none'}")
        click.echo(f"     Emails in thread: {len(pv.get('discussion', []))}")

    # All discussions
    section(f"DISCUSSIONS ({len(b.get('discussions', []))} threads)")
    for d in b.get("discussions", []):
        kind = "[syzbot]" if d.get("is_syzbot_report") else f"[v{d.get('patch_version')}]" if d.get("patch_version") else "[?]"
        msgs = len(d.get("messages", []))
        click.echo(f"  {kind:<10} {msgs:>3} emails  {d.get('subject', d.get('url',''))[:55]}")

    click.echo()


@cli.command("crash")
@click.argument("bug_id")
@click.option("--full", is_flag=True, help="Show full crash report")
def cmd_crash(bug_id, full):
    """Print the kernel crash report for a bug."""
    b = load_bug(bug_id)
    if not b:
        return

    crashes = b.get("crashes", [])
    if not crashes or not crashes[0].get("crash_report"):
        click.echo("No crash report available.")
        return

    header(f"Crash Report: {b.get('title', '')}")
    text = crashes[0]["crash_report"]
    click.echo(text if full else truncate(text, 3000))

    # Also show C reproducer if available
    if crashes[0].get("c_reproducer"):
        section("C REPRODUCER")
        click.echo(crashes[0]["c_reproducer"] if full else truncate(crashes[0]["c_reproducer"], 1500))


@cli.command("patch")
@click.argument("bug_id")
@click.option("--version", "-v", default=0, type=int,
              help="Patch version to show (0 = final merged patch)")
@click.option("--full", is_flag=True)
def cmd_patch(bug_id, version, full):
    """Print a patch diff (final or a specific version)."""
    b = load_bug(bug_id)
    if not b:
        return

    if version == 0:
        # Final merged patch
        fcs = b.get("fix_commits", [])
        if not fcs or not fcs[0].get("patch_diff"):
            click.echo("No final patch diff available.")
            return
        fc = fcs[0]
        header(f"Final Patch: {fc.get('title', '')}")
        click.echo(f"Commit: {fc.get('hash', '')}")
        click.echo(f"Author: {fc.get('author_name', '')} <{fc.get('author', '')}>")
        click.echo(f"Date:   {fc.get('date', '')}")
        click.echo()
        text = fc["patch_diff"]
        click.echo(text if full else truncate(text, 4000))
    else:
        # Specific version
        pvs = {pv["version"]: pv for pv in b.get("patch_versions", [])}
        if version not in pvs:
            available = sorted(pvs.keys())
            click.echo(f"Version v{version} not found. Available: {available}")
            return
        pv = pvs[version]
        header(f"Patch v{version}: {pv.get('subject', '')}")
        if pv.get("diff"):
            click.echo(pv["diff"] if full else truncate(pv["diff"], 4000))
        else:
            click.echo("(no inline diff captured for this version)")


@cli.command("discuss")
@click.argument("bug_id")
@click.option("--version", "-v", default=None, type=int,
              help="Show discussion for a specific patch version (default: all)")
@click.option("--full", is_flag=True, help="Show full email bodies")
def cmd_discuss(bug_id, version, full):
    """Print the email discussion thread(s) for a bug."""
    b = load_bug(bug_id)
    if not b:
        return

    header(f"Discussion: {b.get('title', '')}")

    patch_versions = b.get("patch_versions", [])
    if version is not None:
        patch_versions = [pv for pv in patch_versions if pv["version"] == version]
        if not patch_versions:
            click.echo(f"No discussion found for v{version}.")
            return

    if not patch_versions:
        click.echo("No patch discussion data captured.")
        return

    for pv in sorted(patch_versions, key=lambda x: x["version"]):
        section(f"Patch v{pv['version']}: {pv.get('subject', '')[:60]}")
        emails = pv.get("discussion", [])
        if not emails:
            click.echo("  (no emails)")
            continue

        for i, msg in enumerate(emails):
            click.echo(f"\n  ┌─ Email #{i+1} of {len(emails)}")
            click.echo(f"  │  From:    {msg.get('from_addr', '')}")
            click.echo(f"  │  Date:    {msg.get('date', '')}")
            click.echo(f"  │  Subject: {msg.get('subject', '')}")
            click.echo(f"  └─")
            body = msg.get("body", "")
            if not full:
                # Show first 600 chars
                lines = body.split("\n")
                preview = "\n".join(lines[:20])
                if len(lines) > 20:
                    preview += f"\n  ... [{len(lines)-20} more lines]"
                click.echo("  " + preview.replace("\n", "\n  "))
            else:
                click.echo("  " + body.replace("\n", "\n  "))


@cli.command("diff")
@click.argument("bug_id")
def cmd_diff(bug_id):
    """Show how the patch changed from v1 to the final version."""
    b = load_bug(bug_id)
    if not b:
        return

    pvs = b.get("patch_versions", [])
    final_diff = ""
    fcs = b.get("fix_commits", [])
    if fcs:
        final_diff = fcs[0].get("patch_diff", "")

    if not pvs:
        click.echo("No patch version history available.")
        return

    header(f"Patch Evolution: {b.get('title', '')}")

    v1 = next((pv for pv in pvs if pv["version"] == 1), None)
    if v1 and v1.get("diff"):
        section(f"v1 diff — {v1.get('subject', '')[:60]}")
        click.echo(truncate(v1["diff"], 3000))

    for pv in sorted(pvs, key=lambda x: x["version"])[1:]:
        if pv.get("diff"):
            section(f"v{pv['version']} diff — {pv.get('subject','')[:60]}")
            click.echo(truncate(pv["diff"], 3000))

    if final_diff:
        fc = fcs[0]
        section(f"FINAL (merged) — {fc.get('title','')[:60]}")
        click.echo(f"Commit: {fc.get('hash','')}")
        click.echo(truncate(final_diff, 3000))


@cli.command("search")
@click.argument("keyword")
@click.option("--limit", "-n", default=20)
@click.option("--has-reproducer", "-r", is_flag=True,
              help="Only show bugs with a C reproducer")
@click.option("--deep", is_flag=True,
              help="Search full crash reports (slow — loads all files)")
def cmd_search(keyword, limit, has_reproducer, deep):
    """Search bug titles and crash reports for a keyword (fast, uses index)."""
    kw = keyword.lower()
    click.echo(f"\nSearching for: '{keyword}' ...\n")

    if deep:
        # Slow path: load full files to search complete crash reports
        results = []
        for p in sorted(PROCESSED_DIR.glob("*.json")):
            try:
                b = json.loads(p.read_text())
            except Exception:
                continue
            crashes = b.get("crashes", [])
            crash_text = crashes[0].get("crash_report", "").lower() if crashes else ""
            if (kw in b.get("title", "").lower()
                    or kw in crash_text
                    or any(kw in fc.get("title", "").lower() for fc in b.get("fix_commits", []))):
                if not has_reproducer or any(
                    c.get("c_reproducer") and c.get("kernel_commit") and c.get("kernel_config_link")
                    for c in crashes
                ):
                    results.append({"bug_id": b["bug_id"], "title": b.get("title", ""),
                                    "has_c_reproducer": any(c.get("c_reproducer") for c in crashes)})
                    if len(results) >= limit:
                        break
    else:
        # Fast path: search the index (title + 300-char crash summary)
        records = _ensure_index()
        results = []
        for b in records:
            if (kw in b.get("title", "").lower()
                    or kw in b.get("crash_title", "").lower()
                    or kw in b.get("crash_summary", "").lower()):
                if not has_reproducer or b.get("has_c_reproducer"):
                    results.append(b)
                    if len(results) >= limit:
                        break

    if not results:
        click.echo("No results found.")
        return

    click.echo(f"{'BUG ID':<24} {'R':<3} {'TITLE'}")
    click.echo(f"{'─'*24} {'─'*3} {'─'*50}")
    for b in results:
        has_r = "✓" if b.get("has_c_reproducer") else "·"
        click.echo(f"{b.get('bug_id',''):<24} {has_r:<3} {b.get('title','')[:55]}")
    click.echo(f"\n{len(results)} result(s). Use 'python view.py show <bug_id>' for details.")
    if not deep:
        click.echo("(searched titles + crash summaries; use --deep to search full crash reports)")


@cli.command("build-index")
def cmd_build_index():
    """Build (or rebuild) the fast-lookup index from all processed bug files.

    The index is a lightweight JSONL file (~5MB) that caches per-bug metadata
    so 'list' and 'search' don't need to load all 11GB of processed data.
    Run this once after collecting new bugs to keep the index current.
    """
    import time
    t0 = time.time()
    count = build_index(show_progress=True)
    elapsed = time.time() - t0
    click.echo(f"Done: {count} bugs indexed in {elapsed:.1f}s "
               f"({INDEX_FILE.stat().st_size // 1024}KB)")


@cli.command("crosslayer")
@click.argument("bug_id")
@click.option("--full", is_flag=True, help="Show full crash report and patch")
def cmd_crosslayer(bug_id, full):
    """Show cross-layer analysis for a specific bug.

    Runs live classification of the crash stack trace and patch diff
    against the kernel layer taxonomy.
    """
    b = load_bug(bug_id)
    if not b:
        return

    # Add analysis/ parent to path so we can import the analyzer
    analysis_parent = Path(__file__).parent.parent
    if str(analysis_parent) not in sys.path:
        sys.path.insert(0, str(analysis_parent))

    from analysis.filters import parse_stack_trace
    from analysis.analyzers.cross_layer import compute_cross_layer
    from analysis.analyzers.kernel_layers import classify_file_layer, get_layer_label

    crashes = b.get("crashes", [])
    crash_report = crashes[0].get("crash_report", "") if crashes else ""
    patch_diff = ""
    for fc in b.get("fix_commits", []):
        if fc.get("patch_diff"):
            patch_diff = fc["patch_diff"]
            break

    if not crash_report:
        click.echo("[error] No crash report available for this bug.")
        return
    if not patch_diff:
        click.echo("[error] No patch diff available for this bug.")
        return

    header(f"Cross-Layer Analysis: {b.get('title', '')}")
    click.echo(f"  Bug ID: {bug_id}")

    # Show stack trace classification
    section("CRASH STACK TRACE (layer classification)")
    frames = parse_stack_trace(crash_report)
    if not frames:
        click.echo("  (no stack trace found)")
    else:
        for i, f in enumerate(frames[:15]):
            cls = classify_file_layer(f.file) if f.file else None
            if cls:
                domain, layer_name, level = cls
                label = get_layer_label(domain, layer_name, level)
                tag = f"  [{domain}: {label}]"
            else:
                tag = "  [unclassified]"
            inline = " [inline]" if f.is_inline else ""
            click.echo(f"  #{i:<2} {f.function}{inline}")
            click.echo(f"      {f.file}:{f.line}{tag}")
        if len(frames) > 15:
            click.echo(f"  ... and {len(frames) - 15} more frames")

    # Show patch file classification
    section("PATCH FILES (layer classification)")
    import re
    fix_files = re.findall(r'diff --git a/(\S+)', patch_diff)
    for fp in fix_files:
        cls = classify_file_layer(fp)
        if cls:
            domain, layer_name, level = cls
            label = get_layer_label(domain, layer_name, level)
            tag = f"[{domain}: {label}]"
        else:
            tag = "[unclassified]"
        click.echo(f"  {fp}  {tag}")

    # Run full cross-layer analysis
    section("CROSS-LAYER DETERMINATION")
    result = compute_cross_layer(crash_report, patch_diff)
    if result is None:
        click.echo("  Could not determine (missing data)")
    elif result["is_cross_layer"]:
        click.echo(f"  *** CROSS-LAYER BUG ***")
        click.echo(f"  Domain:      {result['domain']}")
        click.echo(f"  Crash layer: {result['crash_layer']}")
        click.echo(f"  Fix layer:   {result['fix_layer']}")
        click.echo(f"  Direction:   {result['direction']}")

        # Stack overlap analysis
        overlap = result.get("stack_overlap", "unknown")
        if overlap == "fix_off_stack":
            click.echo(f"  Stack:       ⚡ FIX NOT ON CRASH STACK (true cross-layer)")
        elif overlap == "fix_on_stack":
            click.echo(f"  Stack:       ↳ Fix file IS on crash stack (stack-reachable)")
        if result.get("fix_on_stack_files"):
            click.echo(f"  On-stack:    {', '.join(result['fix_on_stack_files'])}")
        if result.get("fix_off_stack_files"):
            click.echo(f"  Off-stack:   {', '.join(result['fix_off_stack_files'])}")

        if len(result.get("all_findings", [])) > 1:
            click.echo(f"\n  Additional cross-layer relationships:")
            for finding in result["all_findings"][1:]:
                click.echo(
                    f"    {finding['domain']}: "
                    f"{finding['crash_layer']} → {finding['fix_layer']} "
                    f"({finding['direction']})"
                )
    else:
        click.echo(f"  Not cross-layer ({result.get('reason', 'same layer')})")
        if result.get("shared_domains"):
            click.echo(f"  Shared domains: {', '.join(result['shared_domains'])}")

    # Optionally show saved analyzer result if available
    saved = _cross_layer_detail(bug_id)
    if saved and saved.get("is_cross_layer"):
        click.echo(f"\n  (Saved result matches: {saved['domain']} / {saved['direction']})")

    click.echo()


@cli.command("random")
@click.option("--has-evolution", is_flag=True, default=True,
              help="Pick a bug with patch version history (default: on)")
def cmd_random(has_evolution):
    """Show a random bug (defaults to one with patch evolution)."""
    bugs = all_bugs()
    if has_evolution:
        pool = [b for b in bugs
                if len(b.get("patch_versions", [])) > 1
                and any(fc.get("patch_diff") for fc in b.get("fix_commits", []))]
    else:
        pool = [b for b in bugs if b.get("fix_commits")]

    if not pool:
        click.echo("No matching bugs in dataset.")
        return

    b = random.choice(pool)
    click.echo(f"\nRandom pick: {b['bug_id']}\n")
    # Delegate to show command
    from click.testing import CliRunner
    runner = CliRunner()
    result = runner.invoke(cmd_show, [b["bug_id"]])
    click.echo(result.output)


@cli.command("stats")
def cmd_stats():
    """Show dataset statistics and cross-layer/stack-overlap breakdown.

    Reads from saved cross-layer analysis results. Run the analyzer first:
      python -m analysis.run_all --analyzer crosslayer
    """
    header("SyzFix Dataset Statistics")

    # Basic dataset stats from index
    records = _ensure_index()
    total = len(records)
    with_patch = sum(1 for b in records if b.get("has_patch"))
    with_repro = sum(1 for b in records if b.get("has_c_reproducer"))
    with_disc = sum(1 for b in records if b.get("has_discussion"))
    with_evo = sum(1 for b in records if b.get("n_patch_versions", 0) > 1)

    click.echo(f"  Total bugs:              {total}")
    click.echo(f"  With patch diff:         {with_patch}")
    click.echo(f"  With C reproducer:       {with_repro}")
    click.echo(f"  With discussion:         {with_disc}")
    click.echo(f"  With patch evolution:    {with_evo}")

    # Cross-layer results
    data = _load_cross_layer_results()
    if data is None:
        click.echo("\n  [cross-layer results not found — run: python -m analysis.run_all --analyzer crosslayer]")
        return

    summary = data.get("summary", {})
    details = data.get("details", [])

    section("Cross-Layer Analysis")
    analyzed = summary.get("Analyzed (have stack trace + patch)", "?")
    click.echo(f"  Analyzed (stack trace + patch): {analyzed}")
    click.echo(f"  Skipped (missing data):         {summary.get('Skipped (missing data)', '?')}")

    section("Fix Location vs. Crash Location")

    cross = [d for d in details if d.get("is_cross_layer")]
    not_cross = [d for d in details if not d.get("is_cross_layer")]
    n_analyzed = len(details)

    # Stack overlap across cross-layer bugs
    cl_on = sum(1 for d in cross if d.get("stack_overlap") == "fix_on_stack")
    cl_off = sum(1 for d in cross if d.get("stack_overlap") == "fix_off_stack")

    click.echo(f"")
    click.echo(f"  Cross-layer bugs (different architectural layer):")
    click.echo(f"    Total:                        {len(cross):>5}  ({len(cross)/max(n_analyzed,1)*100:.1f}% of analyzed)")
    click.echo(f"    Fix ON crash stack:            {cl_on:>5}  (stack-reachable — fix file visible in stack trace)")
    click.echo(f"    Fix OFF crash stack:           {cl_off:>5}  (true cross-layer — fix file NOT in stack trace)")
    click.echo(f"")
    click.echo(f"  Same-layer bugs:                {summary.get('Same-layer bugs', '?')}")
    click.echo(f"  No shared domain (cross-subsys): {summary.get('No shared domain (cross-subsystem)', '?')}")

    section("Cross-Layer by Domain")
    from collections import Counter
    domain_counts = Counter(d.get("domain") for d in cross)
    for domain, count in domain_counts.most_common():
        pct = count / max(len(cross), 1) * 100
        click.echo(f"  {domain:<15} {count:>4}  ({pct:.1f}%)")

    section("Cross-Layer by Direction")
    dir_counts = Counter(d.get("direction") for d in cross)
    for direction, count in dir_counts.most_common():
        label = "crash upper → fix lower" if direction == "fix_in_lower_layer" else "crash lower → fix upper"
        click.echo(f"  {label:<30} {count:>4}  ({count/max(len(cross),1)*100:.1f}%)")

    section("Key Insight for LLM-based Bug Fixing")
    click.echo(f"  Of {len(cross)} cross-layer bugs:")
    click.echo(f"    {cl_on:>4} ({cl_on/max(len(cross),1)*100:.1f}%)  fix file is ON the crash stack trace")
    click.echo(f"          → Stack trace can guide an LLM to the fix location")
    click.echo(f"    {cl_off:>4} ({cl_off/max(len(cross),1)*100:.1f}%)  fix file is OFF the crash stack trace")
    click.echo(f"          → Requires architectural reasoning to locate the fix")
    click.echo(f"")
    click.echo(f"  Use 'list --true-cross-layer' to see the {cl_off} hardest cases.")
    click.echo(f"  Use 'list --cross-layer --verify-stack' to see all with on/off labels.")
    click.echo()


if __name__ == "__main__":
    cli()

