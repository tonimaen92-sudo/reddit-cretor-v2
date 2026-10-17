# UTF-8
"""Offline eyes benchmark: 20 frozen frames, exact-xy Reply accuracy.

Frames live in misc/bench_frames/ with ground truth below (pinned: live
runs overwrite dumps/, so the bench never reads dumps/). No phone touched.
Scoring: predicted reply_xy within 80px of ANY true Reply center = hit.
Cost/shot from live OpenRouter pricing + measured token usage.

Models (resolved 24 Sep 2026 via GET /api/v1/models):
  gemini-flash -> google/gemini-2.5-flash
  qwen-vl      -> qwen/qwen2.5-vl-72b-instruct
  ui-tars      -> bytedance/ui-tars-1.5-7b
  gpt-mini     -> openai/gpt-4o-mini (baseline)

Run: python misc/eyes_bench.py [--models gemini-flash,qwen-vl] [--frames 20]
Out: logs/eyes_bench.json + console table.
"""

from __future__ import annotations

import base64
import json
import os
import re
import sys
import time
from friend_poster.core.reply_state import prep_frame  # noqa: E402
from friend_poster.core.paths import LEGACY_MISC_DIR, PROJECT_ROOT

ROOT = str(PROJECT_ROOT)
FRAMES_DIR = os.path.join(LEGACY_MISC_DIR, "bench_frames")
OUT_PATH = os.path.join(ROOT, "logs", "eyes_bench.json")
OPENROUTER = "https://openrouter.ai/api/v1/chat/completions"

MODELS = {
    "gemini-flash": "google/gemini-2.5-flash",
    "qwen-vl": "qwen/qwen2.5-vl-72b-instruct",
    "ui-tars": "bytedance/ui-tars-1.5-7b",
    "gpt-mini": "openai/gpt-4o-mini",
}

# stem -> list of true Reply centers (750x1334 device pixels)
TRUTH: dict[str, list[tuple[int, int]]] = {
    "2003_comments_0": [(475, 1022)],
    "2003_top_check2": [(474, 672)],
    "2100_top_comment": [(476, 653), (475, 902), (476, 1149)],
    "2101_top_again": [(476, 653), (475, 902), (476, 1149)],
    "2102_top": [(476, 653), (475, 902), (476, 1149)],
    "2103_top_final": [(476, 653), (475, 902), (476, 1149)],
    "2106_top_comment_final": [(476, 653), (475, 902), (476, 1149)],
    "2110_first_comment": [(476, 898), (476, 1145)],
    "2111_first": [(476, 653), (475, 902), (476, 1149)],
    "2113_first": [(476, 627), (475, 947)],
    "2114_top": [(476, 627), (475, 947)],
    "2401_scan_7": [(481, 60)],
    "2661_after_reply": [(475, 265), (709, 789)],
    "2663_typed": [(475, 265), (709, 788)],
    "3030_comments": [(709, 789)],
    "3035_toplevel_composer": [(709, 789)],
    "3050_sent": [(513, 1142)],
    "820_after_select": [(709, 788)],
    "939_preclose_check": [(513, 1142)],
    "live_forward": [(480, 882)],
}

PROMPT = """This is an iPhone screenshot (750x1334 pixels) of a Reddit post in mobile Safari.
Find the Reply button of the FIRST fully visible user comment (reply-arrow glyph + 'Reply' text; skip the post header, skip the bottom 'Join the conversation' bar).
Return STRICT JSON only, no other text: {"reply_xy": [x, y]} with the button center in device pixels. Measure exactly, never guess or round."""

RADIUS = 80


def load_key() -> str:
    from friend_poster.services.comment_llm import load_api_key

    return load_api_key()


def pricing() -> dict:
    import requests

    ms = requests.get("https://openrouter.ai/api/v1/models", timeout=30).json()["data"]
    out = {}
    for m in ms:
        p = m.get("pricing", {}) or {}
        try:
            out[m["id"]] = (float(p.get("prompt") or 0), float(p.get("completion") or 0))
        except Exception:
            pass
    return out


def one_shot(model_id: str, key: str, png: bytes) -> dict:
    import requests

    b64 = base64.b64encode(prep_frame(png)).decode("ascii")
    t0 = time.time()
    try:
        r = requests.post(
            OPENROUTER,
            headers={"Authorization": "Bearer " + key,
                     "Content-Type": "application/json",
                     "HTTP-Referer": "https://github.com/reddit-bot",
                     "X-Title": "Reddit iPhone Bench"},
            json={"model": model_id,
                  "messages": [{"role": "user", "content": [
                      {"type": "text", "text": PROMPT},
                      {"type": "image_url", "image_url": {
                          "url": "data:image/png;base64," + b64}}]}],
                  "max_tokens": 100, "temperature": 0.0},
            timeout=120)
    except Exception as e:
        return {"ok": False, "err": "transport: %s" % str(e)[:100], "elapsed": time.time() - t0}
    el = time.time() - t0
    if r.status_code != 200:
        return {"ok": False, "err": "HTTP %d: %s" % (r.status_code, (r.text or "")[:120]), "elapsed": el}
    try:
        data = r.json()
        text = (data.get("choices") or [{}])[0].get("message", {}).get("content") or ""
        usage = data.get("usage") or {}
    except Exception:
        return {"ok": False, "err": "bad envelope", "elapsed": el}
    m = re.search(r"\{.*\}", text, re.S)
    try:
        got = json.loads(m.group(0))["reply_xy"] if m else None
    except Exception:
        got = None
    if not (isinstance(got, list) and len(got) == 2
            and all(isinstance(v, (int, float)) for v in got)):
        return {"ok": False, "err": "no-parse: %s" % text[:80], "elapsed": el,
                "ptok": usage.get("prompt_tokens", 0), "ctok": usage.get("completion_tokens", 0)}
    return {"ok": True, "xy": [int(got[0]), int(got[1])], "elapsed": el,
            "ptok": usage.get("prompt_tokens", 0), "ctok": usage.get("completion_tokens", 0)}


def main() -> int:
    import argparse

    ap = argparse.ArgumentParser()
    ap.add_argument("--models", default=",".join(MODELS))
    ap.add_argument("--frames", type=int, default=20)
    args = ap.parse_args()
    want = [m.strip() for m in args.models.split(",") if m.strip() in MODELS]
    if not want:
        print("ERROR unknown models")
        return 2
    key = load_key()
    if not key:
        print("ERROR no openrouter_api_key")
        return 2
    prices = pricing()
    stems = sorted(TRUTH)[:max(1, args.frames)]
    results: dict = {"frames": len(stems), "models": {}}
    for name in want:
        mid = MODELS[name]
        pp, pc = prices.get(mid, (0, 0))
        hits, errs, secs, cost = 0, [], 0.0, 0.0
        per_frame = []
        for stem in stems:
            with open(os.path.join(FRAMES_DIR, stem + ".png"), "rb") as f:
                png = f.read()
            r = one_shot(mid, key, png)
            secs += r.get("elapsed", 0)
            cost += r.get("ptok", 0) * pp + r.get("ctok", 0) * pc
            if not r.get("ok"):
                errs.append("%s:%s" % (stem, r.get("err")))
                per_frame.append({"stem": stem, "hit": False, "err": r.get("err")})
                continue
            d = min(abs(r["xy"][0] - tx) + abs(r["xy"][1] - ty) for tx, ty in TRUTH[stem])
            hit = d <= RADIUS
            hits += 1 if hit else 0
            per_frame.append({"stem": stem, "hit": hit, "xy": r["xy"], "delta": d,
                              "ptok": r.get("ptok"), "elapsed": round(r.get("elapsed", 0), 1)})
            print("%s %s hit=%s d=%d $%.5f %ss" % (
                name, stem, hit, d, r.get("ptok", 0) * pp + r.get("ctok", 0) * pc,
                round(r.get("elapsed", 0), 1)), flush=True)
        n = len(stems)
        results["models"][name] = {
            "id": mid, "hits": hits, "of": n,
            "hit_rate": round(hits / n, 3) if n else 0,
            "avg_cost_per_shot": round(cost / n, 6) if n else 0,
            "avg_seconds": round(secs / n, 1) if n else 0,
            "errors": errs, "frames": per_frame,
        }
    os.makedirs(os.path.dirname(OUT_PATH), exist_ok=True)
    with open(OUT_PATH, "w", encoding="utf-8") as f:
        json.dump(results, f, indent=2)
        f.write("\n")
    print("=== SUMMARY ===", flush=True)
    for name, m in results["models"].items():
        print("%s: %d/%d hit_rate=%.3f $%.6f/shot %ss avg errors=%d" % (
            name, m["hits"], m["of"], m["hit_rate"], m["avg_cost_per_shot"],
            m["avg_seconds"], len(m["errors"])), flush=True)
    print("WROTE", OUT_PATH, flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
