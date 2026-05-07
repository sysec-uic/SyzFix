#!/usr/bin/env python3
"""Visualize a bug's call stack and patch files on the kernel-layer hierarchy.

Generates a single self-contained HTML page per bug with:
  • the full kernel-layer taxonomy (13 domains × up to 3 levels), introspected
    live from `analysis/analyzers/kernel_layers.py` so the picture stays in
    sync with the source-of-truth definitions
  • the crash call stack, each frame coloured by its (domain, level)
  • the ground-truth patched files, each coloured by its (domain, level),
    with on-stack vs off-stack visually distinguished

Examples:

    # One bug → one HTML file
    python -m analysis.viz_layer_bug --bug-id 5b64180f8d9e39d3f061

    # Multiple bugs → directory + index page
    python -m analysis.viz_layer_bug \\
        --bug-id 5b64180f8d9e39d3f061,037e18398ba8c655a652,0039110f932d438130f9 \\
        --out viz/

    # Random sample
    python -m analysis.viz_layer_bug --sample 10 --out viz/
"""

from __future__ import annotations

import argparse
import html
import json
import random
import re
import sys
from dataclasses import asdict
from pathlib import Path
from typing import Iterable

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from analysis.analyzers.kernel_layers import (
    DOMAINS, classify_file_layer,
)
from analysis.analyzers.cross_layer import (
    classify_under_mode, compute_cross_layer,
)

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_INPUT = (
    PROJECT_ROOT / "analysis" / "results" / "cross-layer_analysis" / "result.json"
)
DEFAULT_PROCESSED_DIR = (
    PROJECT_ROOT / "dataset" / "data" / "processed"
)


# ─── Colour scheme ──────────────────────────────────────────────────────────


def _domain_hue(domain_name: str) -> int:
    """Stable hue [0..360) per domain — order matches DOMAINS list."""
    names = [d.name for d in DOMAINS]
    if domain_name in names:
        idx = names.index(domain_name)
    else:
        idx = sum(ord(c) for c in domain_name) % len(names)
    # Spread hues evenly around the wheel.
    return int(idx * (360 / max(len(names), 1)))


def _level_palette(level: int) -> tuple[int, int]:
    """Return (saturation%, lightness%) for a level. L0 darkest, L2 lightest."""
    # L0 = abstract/core (saturated, dark)  → high contrast
    # L1 = framework/bus (medium)
    # L2 = specific impl (light, less saturated)
    if level == 0:
        return (75, 38)
    if level == 1:
        return (60, 52)
    return (50, 68)  # level 2 or unknown


def _color_for(domain: str | None, level: int | None) -> str:
    if not domain or level is None:
        return "hsl(0, 0%, 80%)"  # gray for unclassified
    h = _domain_hue(domain)
    s, l = _level_palette(level)
    return f"hsl({h}, {s}%, {l}%)"


def _text_color_for(level: int | None) -> str:
    if level is None or level >= 2:
        return "#222"
    return "#fff"


# ─── Taxonomy serialization ─────────────────────────────────────────────────


def _layer_examples(layer) -> list[str]:
    """Return all path prefixes and regex patterns belonging to this layer.

    Patterns are prefixed with `~ ` so the visualizer can distinguish
    explicit prefixes from catch-all regexes at a glance.
    """
    out: list[str] = []
    out.extend(layer.path_prefixes)
    for pat in layer.path_patterns:
        out.append(f"~ {pat.pattern}")
    return out


def serialize_taxonomy() -> list[dict]:
    """Build a JSON-friendly view of DOMAINS for the taxonomy panel."""
    out: list[dict] = []
    for d in DOMAINS:
        layers_out: list[dict] = []
        for layer in sorted(d.layers, key=lambda l: l.level):
            paths = _layer_examples(layer)
            layers_out.append({
                "name": layer.name,
                "level": layer.level,
                "color": _color_for(d.name, layer.level),
                "text_color": _text_color_for(layer.level),
                "examples": paths,
                "n_paths": len(paths),
            })
        out.append({
            "name": d.name,
            "hue": _domain_hue(d.name),
            "n_layers": len(layers_out),
            "layers": layers_out,
        })
    return out


# ─── Bug data extraction ────────────────────────────────────────────────────


def load_patch_diffs(
    bug_id: str, processed_dir: Path = DEFAULT_PROCESSED_DIR,
) -> dict[str, str]:
    """Load the bug's per-file patch diff from processed/<bug_id>.json.

    Concatenates all `fix_commits[*].patch_diff` (most bugs have one),
    splits on `diff --git a/<path>` headers, and returns a map
    {file_path: diff_chunk}. Returns empty dict if the file is missing
    or has no patch.
    """
    path = processed_dir / f"{bug_id}.json"
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return {}

    pieces: list[str] = []
    for fc in data.get("fix_commits") or []:
        diff = fc.get("patch_diff") or ""
        if diff:
            pieces.append(diff)
    if not pieces:
        return {}
    full = "\n".join(pieces)

    # Split on `diff --git a/<file>` lines. The first segment before the
    # first such header is the commit message / mailbox metadata; drop it.
    out: dict[str, str] = {}
    chunks = re.split(r'(?m)^(?=diff --git a/)', full)
    for chunk in chunks:
        m = re.match(r'diff --git a/(\S+)', chunk)
        if not m:
            continue
        file_path = m.group(1)
        # Last write wins if a path appears twice (rename + edit).
        out[file_path] = chunk.rstrip()
    return out


SYZBOT_BASE = "https://syzkaller.appspot.com"


def _abs_syzbot(link: str | None) -> str:
    """Resolve a syzbot link.

    Syzbot returns absolute URLs unchanged and root-relative paths
    (e.g. ``/text?tag=CrashReport&x=...``) prefixed with the public
    syzkaller host. Empty or non-string inputs yield ``""``.
    """
    if not link or not isinstance(link, str):
        return ""
    if link.startswith("http://") or link.startswith("https://"):
        return link
    if link.startswith("/"):
        return SYZBOT_BASE + link
    return ""


def load_external_links(
    bug_id: str, processed_dir: Path = DEFAULT_PROCESSED_DIR,
) -> dict:
    """Collect outbound links for a bug for manual verification.

    Reads ``processed/<bug_id>.json`` and returns:
      - ``syzbot_bug``: syzbot bug page URL (always set if bug_id given)
      - ``crash_report``, ``syz_reproducer``, ``c_reproducer``,
        ``kernel_config``: absolute URLs or "" if absent
      - ``fix_commits``: list of {hash_short, link, title} for each fix commit
    """
    out: dict = {
        "syzbot_bug": (
            f"{SYZBOT_BASE}/bug?extid={bug_id}" if bug_id else ""
        ),
        "crash_report": "",
        "syz_reproducer": "",
        "c_reproducer": "",
        "kernel_config": "",
        "fix_commits": [],
    }
    if not bug_id:
        return out
    path = processed_dir / f"{bug_id}.json"
    if not path.exists():
        return out
    try:
        data = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return out

    # Pick the first crash that actually has a report; fall back to crashes[0].
    crashes = data.get("crashes") or []
    chosen = None
    for c in crashes:
        if c.get("crash_report_link"):
            chosen = c
            break
    if chosen is None and crashes:
        chosen = crashes[0]
    if chosen:
        out["crash_report"] = _abs_syzbot(chosen.get("crash_report_link"))
        out["syz_reproducer"] = _abs_syzbot(chosen.get("syz_reproducer_link"))
        out["c_reproducer"] = _abs_syzbot(chosen.get("c_reproducer_link"))
        out["kernel_config"] = _abs_syzbot(chosen.get("kernel_config_link"))

    for fc in data.get("fix_commits") or []:
        link = fc.get("link") or ""
        if not link:
            continue
        h = fc.get("hash") or ""
        out["fix_commits"].append({
            "hash_short": h[:12] if h else "commit",
            "link": link,
            "title": fc.get("title") or "",
        })
    return out


def _classify_for_viz(file: str) -> dict:
    """Classify a fix file path. Returns viz-friendly dict."""
    cls = classify_file_layer(file)
    if cls is None:
        return {
            "file": file,
            "domain": None,
            "layer_name": None,
            "layer_level": None,
            "color": _color_for(None, None),
            "text_color": "#222",
        }
    domain, layer_name, level = cls
    return {
        "file": file,
        "domain": domain,
        "layer_name": layer_name,
        "layer_level": level,
        "color": _color_for(domain, level),
        "text_color": _text_color_for(level),
    }


def build_bug_view(
    record: dict,
    *,
    processed_dir: Path = DEFAULT_PROCESSED_DIR,
) -> dict:
    """Produce a viz-ready dict from a cross-layer analyzer detail record."""
    crash_frames = []
    for f in record.get("crash_layers_top_n", []):
        crash_frames.append({
            "frame_index": f.get("frame_index"),
            "file": f.get("file"),
            "function": f.get("function") or "",
            "line": f.get("line") or 0,
            "domain": f.get("domain"),
            "layer_name": f.get("layer_name"),
            "layer_level": f.get("layer_level"),
            "is_inline": f.get("is_inline", False),
            "color": _color_for(f.get("domain"), f.get("layer_level")),
            "text_color": _text_color_for(f.get("layer_level")),
        })

    diffs_by_file = load_patch_diffs(record.get("bug_id", ""), processed_dir)

    def _enrich(p: str, on_stack: bool) -> dict:
        info = _classify_for_viz(p)
        info["on_stack"] = on_stack
        info["patch_diff"] = diffs_by_file.get(p, "")
        return info

    on_stack = [_enrich(p, True) for p in record.get("fix_on_stack_files", [])]
    off_stack = [_enrich(p, False) for p in record.get("fix_off_stack_files", [])]
    fix_files = on_stack + off_stack

    headline = {
        "relation": record.get("relation", ""),
        "domain": record.get("domain") or record.get("crash_domain") or "",
        "fix_domain": record.get("fix_domain") or "",
        "crash_layer": record.get("crash_layer", ""),
        "fix_layer": record.get("fix_layer", ""),
        "direction": record.get("direction", ""),
        "stack_overlap": record.get("stack_overlap", ""),
    }

    # Backfill `fix_internal_layers` for analyzer outputs generated before
    # the field was added — recompute on-the-fly from the processed JSON.
    fix_internal_layers = record.get("fix_internal_layers")
    if not fix_internal_layers:
        bug_id = record.get("bug_id", "")
        path = processed_dir / f"{bug_id}.json"
        if bug_id and path.exists():
            try:
                data = json.loads(path.read_text())
                crash_text = (
                    (data.get("crashes") or [{}])[0].get("crash_report") or ""
                )
                diff_text = "\n".join(
                    (fc.get("patch_diff") or "")
                    for fc in (data.get("fix_commits") or [])
                )
                recomputed = compute_cross_layer(crash_text, diff_text)
                if recomputed:
                    fix_internal_layers = (
                        recomputed.get("fix_internal_layers") or []
                    )
            except (OSError, json.JSONDecodeError):
                fix_internal_layers = []
    fix_internal_layers = fix_internal_layers or []

    # Mode-aware verdicts under the four canonical operational definitions.
    # Order matches docs/cross_layer.md: the default appears first.
    mode_specs = [
        ("combined", 1, "default"),
        ("layer", 1, "layer-only"),
        ("layer", "all", "layer relax=all"),
        ("stack", 1, "stack-only"),
    ]
    mode_verdicts = []
    for strict, window, label in mode_specs:
        v = classify_under_mode(record, strict=strict, relax_window=window)
        mode_verdicts.append({
            "strict": strict,
            "relax_window": window,
            "label_text": label,
            "label": v["label"],
            "reason": v["reason"],
            "mode": v["mode"],
        })

    return {
        "bug_id": record.get("bug_id"),
        "title": record.get("title", ""),
        "headline": headline,
        "crash_frames": crash_frames,
        "fix_files": fix_files,
        "fix_internal_layers": fix_internal_layers,
        "mode_verdicts": mode_verdicts,
        "links": load_external_links(record.get("bug_id", ""), processed_dir),
        "raw": record,  # kept for the data-dump panel
    }


# ─── HTML rendering ─────────────────────────────────────────────────────────


_CSS = """
  :root { --fg:#222; --muted:#777; --line:#ddd; --bg:#fafafa; }
  * { box-sizing: border-box; }
  body { font: 14px/1.45 -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
         margin: 0; padding: 24px; color: var(--fg); background: var(--bg); }
  h1 { font-size: 18px; margin: 0 0 6px; }
  h2 { font-size: 14px; text-transform: uppercase; letter-spacing: .04em;
       color: var(--muted); margin: 20px 0 8px; border-bottom: 1px solid var(--line); padding-bottom: 4px;}
  .meta { color: var(--muted); font-size: 12px; }
  .meta b { color: var(--fg); font-weight: 600; }
  .grid { display: grid; grid-template-columns: 1fr 1fr; gap: 24px; margin-top: 12px; }
  @media (max-width: 1100px) { .grid { grid-template-columns: 1fr; } }
  .panel { background: #fff; border: 1px solid var(--line); border-radius: 8px; padding: 14px; }
  .domain { margin-bottom: 14px; }
  .domain-name { font-weight: 600; font-size: 13px; margin-bottom: 4px; }
  .layer-row { display: flex; align-items: flex-start; gap: 8px; margin-bottom: 6px; }
  .layer-pill { padding: 4px 10px; border-radius: 4px; font-size: 11px;
                white-space: nowrap; min-width: 150px; font-weight: 500;
                flex-shrink: 0; }
  .layer-paths { font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
                 font-size: 11px; color: var(--muted); padding: 4px 0; flex: 1;
                 line-height: 1.7; word-break: break-word; }
  .layer-paths .pp { display: inline-block; padding: 1px 6px; margin: 1px 1px;
                     background: #f0f0f0; border-radius: 3px; }
  .layer-paths .pp.regex { background: #f3edff; color: #5b3a99; }
  .frame, .fixfile { display: flex; align-items: center; gap: 8px; padding: 4px 0;
                     border-bottom: 1px dashed #eee; }
  .frame:last-child, .fixfile:last-child { border-bottom: none; }
  .chip { padding: 3px 9px; border-radius: 4px; font-size: 11px; font-weight: 500;
          white-space: nowrap; min-width: 130px; text-align: left; }
  .chip-num { color: var(--muted); font-family: ui-monospace, monospace; font-size: 11px;
              width: 22px; text-align: right; }
  .path { font-family: ui-monospace, monospace; font-size: 12px; flex: 1;
          overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .badge { font-size: 10px; padding: 1px 6px; border-radius: 3px;
           background: #eef; color: #335; margin-left: 4px; }
  .badge-inline { background: #fde; color: #743; }
  .badge-onstack { background: #cef; color: #036; }
  .badge-offstack { background: #fed; color: #722; }
  .relation-cross_layer  { background: #ffeac0; color: #6b4500; }
  .relation-cross_domain { background: #ffd6d0; color: #771511; }
  .relation-same_layer   { background: #d8ecd8; color: #15461a; }
  .pillbar { display: inline-block; padding: 2px 8px; border-radius: 12px;
             font-size: 11px; font-weight: 600; }
  details summary { cursor: pointer; color: var(--muted); font-size: 12px; }
  details pre { font-size: 11px; background: #f4f4f4; padding: 8px; border-radius: 6px;
                overflow: auto; }
  .frame-main { display: flex; flex-direction: column; flex: 1; min-width: 0; }
  .func { font-family: ui-monospace, SFMono-Regular, Menlo, monospace; font-size: 12.5px;
          font-weight: 600; color: var(--fg);
          overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .file-line { font-family: ui-monospace, SFMono-Regular, Menlo, monospace; font-size: 11px;
               color: var(--muted);
               overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .fix-row { display: flex; flex-direction: column; padding: 6px 0;
             border-bottom: 1px dashed #eee; }
  .fix-row:last-child { border-bottom: none; }
  .fix-head { display: flex; align-items: center; gap: 8px; }
  details.diff { margin-top: 6px; }
  details.diff[open] { background: #fcfcfc; padding: 6px 8px; border-radius: 4px;
                      border: 1px solid #eee; }
  pre.diff-body { font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
                  font-size: 11.5px; line-height: 1.45; margin: 6px 0 0;
                  padding: 8px; background: #fafafa; border-radius: 4px;
                  overflow-x: auto; max-height: 360px; }
  .diff-add  { background: #e6ffed; color: #1a7f37; display: block; }
  .diff-del  { background: #ffeef0; color: #b3201e; display: block; }
  .diff-hunk { background: #f1f8ff; color: #032f62; display: block; font-weight: 600; }
  .diff-meta { color: var(--muted); display: block; }
  .diff-ctx  { display: block; }
  .linkbar { margin-top: 8px; display: flex; flex-wrap: wrap; gap: 6px;
             align-items: center; }
  .linkbar .lk-label { font-size: 11px; color: var(--muted);
                       text-transform: uppercase; letter-spacing: .04em;
                       margin-right: 2px; }
  .linkbar a.lk { display: inline-flex; align-items: center; gap: 4px;
                  padding: 3px 9px; border-radius: 12px; font-size: 11px;
                  font-weight: 500; text-decoration: none;
                  background: #eef3ff; color: #1d3a8a;
                  border: 1px solid #d8e0f5; }
  .linkbar a.lk:hover { background: #dbe5ff; }
  .linkbar a.lk.lk-syzbot { background: #fff1d8; color: #6b4500;
                            border-color: #f3dfb1; }
  .linkbar a.lk.lk-commit { background: #e6ffed; color: #0a5c1f;
                            border-color: #c4e9cf; }
  .linkbar a.lk.lk-commit code { font-family: ui-monospace, monospace;
                                  font-size: 10.5px; }
  .modebar { margin-top: 6px; display: flex; flex-wrap: wrap; gap: 6px;
             align-items: center; }
  .modebar .mb-label { font-size: 11px; color: var(--muted);
                       text-transform: uppercase; letter-spacing: .04em; }
  .modebar .mode { padding: 2px 8px; border-radius: 10px; font-size: 11px;
                   font-weight: 500; border: 1px solid #ddd; cursor: help; }
  .modebar .mode.pos { background: #fff1d8; color: #6b4500;
                       border-color: #f3dfb1; }
  .modebar .mode.neg { background: #eef0f4; color: #4a4a4a; }
  .modebar .mode b { font-family: ui-monospace, monospace; font-size: 10.5px;
                     font-weight: 600; }
  .span-bar { margin-top: 6px; font-size: 12px; color: var(--muted); }
  .span-bar .span-chip { display: inline-block; padding: 2px 8px;
                          border-radius: 10px; font-size: 11px; font-weight: 500;
                          margin-left: 4px; border: 1px solid #ddd; }
  .span-bar .span-warn { background: #fff1d8; color: #6b4500;
                         border-color: #f3dfb1; }
"""


def _esc(s) -> str:
    if s is None:
        return ""
    return html.escape(str(s))


def _render_taxonomy(taxonomy: list[dict]) -> str:
    parts = []
    for d in taxonomy:
        rows = []
        for layer in d["layers"]:
            chips = []
            for p in layer["examples"]:
                if p.startswith("~ "):
                    chips.append(
                        f'<span class="pp regex" title="catch-all regex">'
                        f'{_esc(p)}</span>'
                    )
                else:
                    chips.append(f'<span class="pp">{_esc(p)}</span>')
            count_meta = (
                f' <span class="meta" style="font-size:10px">'
                f'({layer["n_paths"]})</span>'
            )
            rows.append(
                f'<div class="layer-row">'
                f'  <span class="layer-pill" '
                f'        style="background:{layer["color"]};color:{layer["text_color"]}">'
                f'    L{layer["level"]} · {_esc(layer["name"])}{count_meta}'
                f'  </span>'
                f'  <span class="layer-paths">{"".join(chips)}</span>'
                f'</div>'
            )
        parts.append(
            f'<div class="domain">'
            f'  <div class="domain-name">{_esc(d["name"])} '
            f'  <span class="meta">({d["n_layers"]} layer{"" if d["n_layers"]==1 else "s"})</span></div>'
            f'  {"".join(rows)}'
            f'</div>'
        )
    return "\n".join(parts)


def _render_frame(frame: dict) -> str:
    name = (frame["layer_name"] or "unclassified")
    label = (
        f'L{frame["layer_level"]} · {_esc(frame["domain"])}'
        if frame["layer_level"] is not None else "unclassified"
    )
    inline = (
        '<span class="badge badge-inline">inline</span>' if frame["is_inline"] else ""
    )
    chip = (
        f'<span class="chip" '
        f'      style="background:{frame["color"]};color:{frame["text_color"]}" '
        f'      title="{_esc(frame["domain"])} / {_esc(name)}">{label}</span>'
    )
    func = frame.get("function") or ""
    line = frame.get("line") or 0
    file_path = frame.get("file") or ""
    func_html = (
        f'<span class="func">{_esc(func)}()</span>' if func
        else f'<span class="func">{_esc(file_path)}</span>'
    )
    line_suffix = f":{line}" if line else ""
    file_html = (
        f'<span class="file-line">{_esc(file_path)}{line_suffix}</span>'
        if func else ""
    )
    return (
        f'<div class="frame">'
        f'  <span class="chip-num">#{frame["frame_index"]}</span>'
        f'  {chip}'
        f'  <span class="frame-main">{func_html}{file_html}</span>'
        f'  {inline}'
        f'</div>'
    )


def _render_diff_body(diff_text: str) -> str:
    """Render a single file's diff with line-level coloring.

    Strips git diff/index/+++/--- header noise (only useful when present
    for context) and shows hunks with classic +/- highlighting. Each
    line gets a CSS class to color additions, deletions, hunk headers,
    and context.
    """
    if not diff_text:
        return '<div class="meta">(diff text not available)</div>'

    lines: list[str] = []
    for raw in diff_text.splitlines():
        if (
            raw.startswith("diff --git")
            or raw.startswith("index ")
            or raw.startswith("new file mode")
            or raw.startswith("deleted file mode")
            or raw.startswith("similarity index")
            or raw.startswith("rename from")
            or raw.startswith("rename to")
        ):
            cls = "diff-meta"
        elif raw.startswith(("+++", "---")):
            cls = "diff-meta"
        elif raw.startswith("@@"):
            cls = "diff-hunk"
        elif raw.startswith("+"):
            cls = "diff-add"
        elif raw.startswith("-"):
            cls = "diff-del"
        else:
            cls = "diff-ctx"
        text = _esc(raw) or "&nbsp;"
        lines.append(f'<span class="{cls}">{text}</span>')
    return f'<pre class="diff-body">{"".join(lines)}</pre>'


def _render_fix(fix: dict) -> str:
    label = (
        f'L{fix["layer_level"]} · {_esc(fix["domain"])}'
        if fix["layer_level"] is not None else "unclassified"
    )
    chip = (
        f'<span class="chip" '
        f'      style="background:{fix["color"]};color:{fix["text_color"]}" '
        f'      title="{_esc(fix["domain"])} / {_esc(fix["layer_name"])}">{label}</span>'
    )
    badge = (
        '<span class="badge badge-onstack">on stack</span>' if fix["on_stack"]
        else '<span class="badge badge-offstack">off stack</span>'
    )

    diff_text = fix.get("patch_diff") or ""
    if diff_text:
        # Count + and - lines for the summary
        adds = sum(
            1 for ln in diff_text.splitlines()
            if ln.startswith("+") and not ln.startswith("+++")
        )
        dels = sum(
            1 for ln in diff_text.splitlines()
            if ln.startswith("-") and not ln.startswith("---")
        )
        diff_summary = (
            f'<span class="meta" style="font-size:11px">'
            f'(<span style="color:#1a7f37">+{adds}</span> '
            f'<span style="color:#b3201e">-{dels}</span>)</span>'
        )
        diff_block = (
            f'<details class="diff">'
            f'  <summary>show diff {diff_summary}</summary>'
            f'  {_render_diff_body(diff_text)}'
            f'</details>'
        )
    else:
        diff_block = (
            '<div class="meta" style="font-size:11px">'
            '(diff not available — run from project root with '
            'dataset/data/processed/ populated)</div>'
        )

    return (
        f'<div class="fix-row">'
        f'  <div class="fix-head">'
        f'    {chip}'
        f'    <span class="path">{_esc(fix["file"])}</span>'
        f'    {badge}'
        f'  </div>'
        f'  {diff_block}'
        f'</div>'
    )


def _render_link_bar(links: dict) -> str:
    """Render outbound verification chips for a bug. Empty if no links."""
    if not links:
        return ""
    chips: list[str] = []

    def _chip(href: str, label: str, klass: str = "lk", title: str = "") -> str:
        title_attr = f' title="{_esc(title)}"' if title else ""
        return (
            f'<a class="{klass}" href="{_esc(href)}" '
            f'target="_blank" rel="noopener noreferrer"{title_attr}>'
            f'{_esc(label)}</a>'
        )

    if links.get("syzbot_bug"):
        chips.append(_chip(
            links["syzbot_bug"], "syzbot", "lk lk-syzbot",
            "open the syzbot bug page",
        ))
    if links.get("crash_report"):
        chips.append(_chip(
            links["crash_report"], "crash report", "lk",
            "open the KASAN/crash report on syzkaller.appspot.com",
        ))
    if links.get("syz_reproducer"):
        chips.append(_chip(
            links["syz_reproducer"], "syz repro", "lk",
            "open the syzkaller reproducer",
        ))
    if links.get("c_reproducer"):
        chips.append(_chip(
            links["c_reproducer"], "C repro", "lk",
            "open the C reproducer",
        ))
    if links.get("kernel_config"):
        chips.append(_chip(
            links["kernel_config"], "config", "lk",
            "open the kernel .config",
        ))
    for fc in links.get("fix_commits") or []:
        if not fc.get("link"):
            continue
        label = f'commit {fc["hash_short"]}'
        chips.append(_chip(
            fc["link"], label, "lk lk-commit",
            fc.get("title") or "open the upstream fix commit",
        ))
    if not chips:
        return ""
    return (
        '<div class="linkbar">'
        '<span class="lk-label">verify:</span>'
        + "".join(chips) +
        '</div>'
    )


def _render_mode_bar(verdicts: list[dict]) -> str:
    """Render mode-aware verdicts (combined+1, layer+1, layer+all, stack)."""
    if not verdicts:
        return ""
    chips = []
    for v in verdicts:
        klass = "mode pos" if v["label"] else "mode neg"
        sign = "+" if v["label"] else "−"
        title = (
            f'mode={v["mode"]}\nlabel='
            f'{"positive" if v["label"] else "negative"}\n'
            f'reason={v["reason"]}'
        )
        chips.append(
            f'<span class="{klass}" title="{_esc(title)}">{sign}&nbsp;'
            f'<b>{_esc(v["label_text"])}</b></span>'
        )
    return (
        '<div class="modebar">'
        '<span class="mb-label">modes:</span>'
        + "".join(chips) +
        '</div>'
    )


def _render_span_bar(internal: list[dict]) -> str:
    """Render a one-line summary of fix_internal_layers."""
    if not internal:
        return ""
    pieces = []
    distinct_keys = set()
    for il in internal:
        distinct_keys.add((il["domain"], il["layer_level"]))
        pieces.append(
            f'<span class="span-chip">'
            f'{_esc(il["domain"])}·L{il["layer_level"]}·'
            f'{_esc(il["layer_name"])} '
            f'<small>({il["lines_changed"]}L, {len(il["files"])}f)</small>'
            f'</span>'
        )
    multi = len(distinct_keys) > 1
    label = "fix spans" + (" <b>multiple layers</b>" if multi else "")
    if multi:
        # Highlight the chips when the patch itself crosses layer boundaries.
        pieces = [p.replace('span-chip', 'span-chip span-warn') for p in pieces]
    return f'<div class="span-bar">{label}:{"".join(pieces)}</div>'


def render_html(view: dict, taxonomy: list[dict]) -> str:
    h = view["headline"]
    relation = h.get("relation") or "unknown"
    rel_pill = (
        f'<span class="pillbar relation-{relation}">{_esc(relation)}</span>'
    )
    direction = h.get("direction")
    direction_html = (
        f' · direction <b>{_esc(direction)}</b>' if direction else ""
    )
    cross_dom_html = ""
    if relation == "cross_domain":
        cross_dom_html = (
            f' · crash domain <b>{_esc(h.get("domain"))}</b>'
            f' → fix domain <b>{_esc(h.get("fix_domain"))}</b>'
        )
    elif h.get("domain"):
        cross_dom_html = f' · domain <b>{_esc(h["domain"])}</b>'
    layer_html = ""
    if h.get("crash_layer") and h.get("fix_layer"):
        layer_html = (
            f' · crash <b>{_esc(h["crash_layer"])}</b>'
            f' → fix <b>{_esc(h["fix_layer"])}</b>'
        )
    stack = h.get("stack_overlap")
    stack_html = f' · stack <b>{_esc(stack)}</b>' if stack else ""

    crash_html = "\n".join(_render_frame(f) for f in view["crash_frames"]) \
                 or '<div class="meta">(no classified frames)</div>'
    fix_html = "\n".join(_render_fix(f) for f in view["fix_files"]) \
               or '<div class="meta">(no fix files)</div>'

    raw_json = _esc(json.dumps(view["raw"], indent=2, default=str))

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>SyzFix · {_esc(view['bug_id'])}</title>
<style>{_CSS}</style>
</head>
<body>
<h1>{_esc(view['title'] or '(no title)')}</h1>
<div class="meta">
  bug <b>{_esc(view['bug_id'])}</b> · {rel_pill}{cross_dom_html}{direction_html}{stack_html}{layer_html}
</div>
{_render_link_bar(view.get('links') or {})}
{_render_mode_bar(view.get('mode_verdicts') or [])}
{_render_span_bar(view.get('fix_internal_layers') or [])}

<div class="grid">

  <div>
    <h2>Call stack (top frames)</h2>
    <div class="panel">{crash_html}</div>

    <h2>Patched files (ground truth)</h2>
    <div class="panel">{fix_html}</div>
  </div>

  <div>
    <h2>Kernel-layer hierarchy</h2>
    <div class="panel">{_render_taxonomy(taxonomy)}</div>
  </div>

</div>

<details style="margin-top:24px">
<summary>raw analyzer record</summary>
<pre>{raw_json}</pre>
</details>

</body>
</html>
"""


# ─── Index page (when generating multiple bugs) ─────────────────────────────


def render_index(views: list[dict], hrefs: list[str]) -> str:
    rows = []
    for v, href in zip(views, hrefs):
        h = v["headline"]
        rel = h.get("relation") or "unknown"
        links = v.get("links") or {}
        ext_cells = []
        if links.get("syzbot_bug"):
            ext_cells.append(
                f'<a href="{_esc(links["syzbot_bug"])}" '
                f'target="_blank" rel="noopener noreferrer" '
                f'title="open the syzbot bug page">syzbot</a>'
            )
        fix_commits = links.get("fix_commits") or []
        if fix_commits and fix_commits[0].get("link"):
            fc = fix_commits[0]
            ext_cells.append(
                f'<a href="{_esc(fc["link"])}" '
                f'target="_blank" rel="noopener noreferrer" '
                f'title="{_esc(fc.get("title") or "")}">'
                f'commit&nbsp;<code>{_esc(fc["hash_short"])}</code></a>'
            )
        ext_html = " · ".join(ext_cells) if ext_cells else ""
        rows.append(
            f'<tr>'
            f'  <td><a href="{_esc(href)}">{_esc(v["bug_id"])}</a></td>'
            f'  <td><span class="pillbar relation-{rel}">{_esc(rel)}</span></td>'
            f'  <td>{_esc(h.get("domain") or h.get("fix_domain") or "")}</td>'
            f'  <td>{_esc(h.get("crash_layer") or "")}</td>'
            f'  <td>{_esc(h.get("fix_layer") or "")}</td>'
            f'  <td>{_esc(h.get("direction") or "")}</td>'
            f'  <td class="path">{_esc(v.get("title", ""))}</td>'
            f'  <td class="ext">{ext_html}</td>'
            f'</tr>'
        )
    return f"""<!DOCTYPE html>
<html><head><meta charset="utf-8"><title>SyzFix · viz index</title>
<style>{_CSS}
table {{ border-collapse: collapse; width: 100%; background: #fff; }}
td, th {{ border-bottom: 1px solid var(--line); padding: 6px 8px; text-align: left; font-size: 13px; }}
th {{ font-size: 12px; text-transform: uppercase; color: var(--muted); }}
td.ext {{ white-space: nowrap; font-size: 12px; }}
td.ext a {{ color: #1d3a8a; text-decoration: none; }}
td.ext a:hover {{ text-decoration: underline; }}
td.ext code {{ font-family: ui-monospace, monospace; font-size: 11px; color: #0a5c1f; }}
</style></head>
<body>
<h1>SyzFix layer visualizer · {len(views)} bugs</h1>
<div class="meta">click a bug_id to open its layered view</div>
<table>
<tr><th>bug_id</th><th>relation</th><th>domain</th><th>crash layer</th><th>fix layer</th><th>direction</th><th>title</th><th>verify</th></tr>
{"".join(rows)}
</table>
</body></html>
"""


# ─── CLI ────────────────────────────────────────────────────────────────────


def _load_records(input_path: Path) -> dict[str, dict]:
    if not input_path.exists():
        sys.exit(
            f"[viz_layer_bug] Missing {input_path}. "
            f"Run `python -m analysis.run_all --analyzer crosslayer` first."
        )
    data = json.loads(input_path.read_text())
    return {r["bug_id"]: r for r in data.get("details", []) if r.get("bug_id")}


def _resolve_ids(
    records: dict[str, dict], bug_ids: list[str], sample: int, prefix_match: bool,
) -> list[str]:
    chosen: list[str] = []
    if bug_ids:
        for raw in bug_ids:
            if raw in records:
                chosen.append(raw)
                continue
            if prefix_match:
                hits = [k for k in records if k.startswith(raw)]
                if len(hits) == 1:
                    chosen.append(hits[0])
                    continue
                if len(hits) > 1:
                    print(f"[viz] ambiguous bug-id prefix {raw!r}: {hits[:5]}…")
                    continue
            print(f"[viz] no record for bug_id {raw!r}", file=sys.stderr)
    if sample > 0:
        random.seed(0)
        chosen.extend(random.sample(list(records.keys()),
                                    min(sample, len(records))))
    # Dedup preserving order
    seen: set[str] = set()
    return [b for b in chosen if not (b in seen or seen.add(b))]


def main() -> None:
    ap = argparse.ArgumentParser(
        description="Visualize a bug's call stack and patch on the kernel-layer hierarchy",
    )
    ap.add_argument(
        "--bug-id", default="",
        help="Comma-separated bug IDs (or unique prefixes when --prefix-match).",
    )
    ap.add_argument(
        "--sample", type=int, default=0,
        help="Pick N random bugs from the dataset (deterministic, seed=0).",
    )
    ap.add_argument(
        "--input", default=str(DEFAULT_INPUT), type=Path,
        help="Path to cross-layer analyzer result.json",
    )
    ap.add_argument(
        "--out", default=None,
        help="Output HTML path (single bug) or directory (multiple bugs). "
             "Default: ./viz_<bug_id>.html or ./viz/.",
    )
    ap.add_argument(
        "--prefix-match", action="store_true",
        help="Allow --bug-id to be a unique prefix of a real bug_id.",
    )
    args = ap.parse_args()

    if not args.bug_id and not args.sample:
        ap.error("provide --bug-id or --sample")

    records = _load_records(args.input)
    ids_input = [s.strip() for s in args.bug_id.split(",") if s.strip()]
    ids = _resolve_ids(records, ids_input, args.sample, args.prefix_match)
    if not ids:
        sys.exit("[viz_layer_bug] no bugs to render")

    taxonomy = serialize_taxonomy()
    views = [build_bug_view(records[bid]) for bid in ids]

    # Resolve output target.
    if len(views) == 1:
        out_path = Path(args.out) if args.out else (
            Path(f"viz_{views[0]['bug_id']}.html")
        )
        if out_path.is_dir():
            out_path = out_path / f"{views[0]['bug_id']}.html"
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(render_html(views[0], taxonomy))
        print(f"[viz_layer_bug] wrote {out_path}")
        return

    out_dir = Path(args.out) if args.out else Path("viz")
    out_dir.mkdir(parents=True, exist_ok=True)
    hrefs: list[str] = []
    for v in views:
        path = out_dir / f"{v['bug_id']}.html"
        path.write_text(render_html(v, taxonomy))
        hrefs.append(path.name)
    index = out_dir / "index.html"
    index.write_text(render_index(views, hrefs))
    print(f"[viz_layer_bug] wrote {len(views)} bug pages + {index}")


if __name__ == "__main__":
    main()
