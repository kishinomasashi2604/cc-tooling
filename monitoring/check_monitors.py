#!/usr/bin/env python3
"""登録簿(monitors.json)に沿って自動化の生死を判定する。

  check_monitors.py            … 判定を表示
  check_monitors.py --json     … JSON
  check_monitors.py --config <path>

★設計の要点（これが本体）
  1. **登録はJSON 1件**。監視を足すときにコードを編集させない。
  2. **痕跡（side effect）で判定する**。プロセスの有無ではなく、
     「成果物がいつ更新されたか」を見る。トリガー一覧が取れない
     GAS のような環境でも実測できる。
  3. **「止まっている」と「対象が来ていない」を区別する**。
     区別できないときは断定せず「要確認」と言う。誤報は監視を殺す。
  4. **判定できないことを「正常」と言わない**。確認手段が無いものは
     blocked に理由を書いて「確認不可」として可視化する（放置ではない）。

同梱の probe は file_mtime（成果物の更新時刻）と http（WebAppの疎通）の2種類。
gmail_label など環境依存のものは evaluate() に分岐を足して拡張する。

出力の状態: 正常 / 要確認 / エラー / 確認不可
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
DEFAULT_CONFIG = HERE / "monitors.json"


def load_monitors(path: Path) -> list[dict]:
    """登録簿を読む。壊れていても例外にせず、警告して空を返す。

    ★登録簿の不備で監視全体を止めない（可用性を人質に取らない）。
    """
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        print(f"⚠️ {path} がありません", file=sys.stderr)
        return []
    except Exception as e:  # noqa: BLE001
        print(f"⚠️ {path} を読めません: {type(e).__name__}", file=sys.stderr)
        return []

    out = []
    for m in data.get("monitors", []):
        if not isinstance(m, dict) or not m.get("label") or not m.get("what"):
            print("⚠️ label と what は必須です。1件スキップしました", file=sys.stderr)
            continue
        if m.get("blocked"):
            out.append(m)
            continue
        if not (isinstance(m.get("probe"), list) and m["probe"]):
            print(f"⚠️ {m['label']} に probe がありません", file=sys.stderr)
            continue
        m = dict(m)
        m.setdefault("stale_hours", 72)
        out.append(m)
    return out


def file_mtime(path: str):
    p = Path(os.path.expanduser(path))
    if not p.exists():
        return None, f"ファイルが無い: {path}"
    return dt.datetime.fromtimestamp(p.stat().st_mtime), None


def resolve_url(spec: str):
    """'plist:<ラベル>:<環境変数名>' なら launchd plist から URL を取り出す。

    ★リポジトリに URL が無くても plist に入っていることがある。
      「設定が見つからない＝未接続」と決めつけないための経路。
    """
    if not spec.startswith("plist:"):
        return spec, None
    _, label, key = spec.split(":", 2)
    p = Path.home() / "Library/LaunchAgents" / f"{label}.plist"
    if not p.exists():
        return None, f"plistが無い: {label}"
    try:
        out = subprocess.run(["plutil", "-p", str(p)], capture_output=True,
                             text=True, timeout=15).stdout
        m = re.search(rf'"{re.escape(key)}"\s*=>\s*"([^"]+)"', out)
        return (m.group(1), None) if m else (None, f"plistに {key} が無い")
    except Exception as e:  # noqa: BLE001
        return None, f"plistを読めない: {type(e).__name__}"


def http_alive(url: str, expect=(200, 302)):
    try:
        r = subprocess.run(
            ["curl", "-s", "-o", "/dev/null", "-w", "%{http_code}", "-L",
             "--max-time", "25", url],
            capture_output=True, text=True, timeout=40)
        code = (r.stdout or "").strip()
        return (code.isdigit() and int(code) in expect), f"HTTP {code or '応答なし'}"
    except Exception as e:  # noqa: BLE001
        return False, f"疎通できない: {type(e).__name__}"


def evaluate(monitors: list[dict]) -> list[dict]:
    rows, now = [], dt.datetime.now()
    for c in monitors:
        label, what = c["label"], c["what"]

        if c.get("blocked"):
            # 確認手段が無いことを状態として明示する。「正常」とも言わない。
            rows.append({"label": label, "status": "確認不可",
                         "note": f"{c['blocked']}（{what}）"})
            continue

        kind = c["probe"][0]

        if kind == "file_mtime":
            ts, err = file_mtime(c["probe"][1])
            if ts is None:
                rows.append({"label": label, "status": "要確認",
                             "note": f"{err} / {what}"})
                continue
            age_h = (now - ts).total_seconds() / 3600
            ok = age_h <= c["stale_hours"]
            rows.append({
                "label": label,
                "status": "正常" if ok else "エラー",
                "note": (f"最終更新 {ts:%m/%d %H:%M}（{age_h:.1f}時間前）/ {what}" if ok
                         else f"最終更新が {ts:%Y/%m/%d}＝{age_h/24:.0f}日前"
                              f"（想定 {c['stale_hours']}h以内）/ {what}")})
            continue

        if kind == "http":
            url, err = resolve_url(c["probe"][1])
            if url is None:
                rows.append({"label": label, "status": "要確認",
                             "note": f"{err} / {what}"})
                continue
            ok, detail = http_alive(url)
            rows.append({
                "label": label,
                "status": "正常" if ok else "エラー",
                # ★到達＝デプロイ生存。中の処理の稼働とは別問題であることを必ず書く。
                "note": (f"{detail}＝デプロイ生存（中の処理の稼働は別途確認）/ {what}" if ok
                         else f"{detail}＝到達できない / {what}")})
            continue

        rows.append({"label": label, "status": "確認不可",
                     "note": f"未対応の probe: {kind} / {what}"})
    return rows


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--config", default=str(DEFAULT_CONFIG))
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args()

    rows = evaluate(load_monitors(Path(a.config).expanduser()))
    if a.json:
        print(json.dumps(rows, ensure_ascii=False, indent=1))
        return 0
    if not rows:
        print("登録がありません。monitors.json に1件足してください。")
        return 0
    for r in rows:
        print("%-46s %-6s %s" % (r["label"][:45], r["status"], r["note"][:90]))
    bad = [r for r in rows if r["status"] in ("エラー", "要確認")]
    print(f"\n{len(rows)}件中 要対応 {len(bad)}件")
    return 1 if bad else 0


if __name__ == "__main__":
    raise SystemExit(main())
