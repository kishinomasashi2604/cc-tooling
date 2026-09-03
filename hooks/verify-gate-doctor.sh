#!/bin/bash
# 監督AIゲートの健全性チェック＆自己修復（2026-09-03）。
#   verify-gate-doctor.sh        … 状態を表示
#   verify-gate-doctor.sh --fix  … settings.json への登録が外れていたら戻す
# ★settings.json を書き換える前に必ずバックアップを取る（横断原則）。
set -u
S="$HOME/.claude/settings.json"
H="$HOME/.claude/hooks/stop-verify-reminder.sh"
OFF="$HOME/.claude/hooks/off/stop-verify-reminder.off"

echo "── 監督AIゲート 状態 ──"
[ -x "$H" ] && echo "  本体      : ✅ 実行可能" || echo "  本体      : 🔴 無い/実行不可 $H"
bash -n "$H" 2>/dev/null && echo "  構文      : ✅ OK" || echo "  構文      : 🔴 エラー"
[ -f "$OFF" ] && echo "  キルSW    : ⏸ 停止中（$OFF を消すと再開）" || echo "  キルSW    : ✅ 稼働（offファイル無し）"
command -v jq >/dev/null && echo "  jq        : ✅ あり" || echo "  jq        : 🔴 無い＝ゲートは無効化される"

REG=$(python3 - "$S" <<'PY'
import json,sys
try:
    d=json.load(open(sys.argv[1],encoding="utf-8"))
    print("yes" if any("stop-verify" in str(x.get("command"))
        for m in d.get("hooks",{}).get("Stop",[]) for x in m.get("hooks",[])) else "no")
except Exception: print("err")
PY
)
case "$REG" in
  yes) echo "  登録      : ✅ Stop フックに登録済み";;
  no)  echo "  登録      : 🔴 未登録（--fix で復旧できます）";;
  *)   echo "  登録      : ⚠️ settings.json を読めません";;
esac

HB=/tmp/cc-hook-heartbeat-stop-verify-reminder
if [ -f "$HB" ]; then
  echo "  最終発火  : $(date -r "$(cat "$HB")" '+%Y-%m-%d %H:%M' 2>/dev/null)"
else
  echo "  最終発火  : 記録なし（まだ一度も動いていない可能性）"
fi

[ "${1:-}" = "--fix" ] || exit 0
[ "$REG" = "no" ] || { echo "→ 修復不要"; exit 0; }
cp "$S" "$S.bak.$(date +%Y%m%d_%H%M%S)" && echo "→ バックアップ作成"
python3 - "$S" "$H" <<'PY'
import json,sys
p,h=sys.argv[1],sys.argv[2]
d=json.load(open(p,encoding="utf-8"))
d.setdefault("hooks",{}).setdefault("Stop",[]).append(
    {"hooks":[{"type":"command","command":h}]})
json.dump(d,open(p,"w",encoding="utf-8"),ensure_ascii=False,indent=2)
print("→ Stop フックへ再登録しました")
PY
