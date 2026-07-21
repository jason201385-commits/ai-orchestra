#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
statusline_quota.py — a Claude Code statusline script that also exports live quota.

Claude Code feeds a JSON blob on stdin each time it refreshes the statusline.
For subscribers, rate_limits.five_hour / seven_day carry used_percentage and
resets_at. This script (1) prints a compact statusline and (2) writes the quota
to <home>/data/claude_quota.json so usage_report.py can surface a real,
live 5h/7d number instead of guessing.

Enable it in ~/.claude/settings.json (adjust the path to your clone):
  "statusLine": { "type": "command",
    "command": "python /path/to/ai-orchestra/scripts/statusline_quota.py" }
"""
import io
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

sys.path.insert(0, str(Path(__file__).resolve().parent))
try:
    from config import DATA_DIR
    OUT = DATA_DIR / "claude_quota.json"
except Exception:
    OUT = Path(__file__).resolve().parent.parent / "data" / "claude_quota.json"

try:
    data = json.load(sys.stdin)
except Exception:
    print("claude")
    sys.exit(0)

model = (data.get("model") or {}).get("display_name", "?")
ctx = data.get("context_window") or {}
ctx_used = ctx.get("used_percentage")
rl = data.get("rate_limits") or {}
fh = (rl.get("five_hour") or {}).get("used_percentage")
sd = (rl.get("seven_day") or {}).get("used_percentage")

# 導出給儀表板
try:
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps({
        "fetched_at": datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds"),
        "model": model,
        "context_used_pct": ctx_used,
        "five_hour_used_pct": fh,
        "seven_day_used_pct": sd,
        "five_hour_resets_at": (rl.get("five_hour") or {}).get("resets_at"),
        "seven_day_resets_at": (rl.get("seven_day") or {}).get("resets_at"),
        "cost_usd": (data.get("cost") or {}).get("total_cost_usd"),
    }, ensure_ascii=False), encoding="utf-8")
except Exception:
    pass

parts = [model]
if ctx_used is not None:
    parts.append(f"ctx {round(ctx_used)}%")
if fh is not None:
    parts.append(f"5h {round(fh)}%")
if sd is not None:
    parts.append(f"7d {round(sd)}%")
print(" | ".join(parts))
