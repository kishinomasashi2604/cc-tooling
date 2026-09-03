#!/bin/bash
# 監督AI（Stop hook）: コード変更があった会話で「独立レビュー/検証」の痕跡が無ければ
# **1回だけ差し戻す**（decision:block）。2回目以降は警告に落として素通りさせる。
#
# CLAUDE.md「実装後：独立レビュー必須（自己レビューで完結させない）」を、文章でも
# リマインドでもなく**実際のゲート**にする。2026-09-03 ブロック化（旧=リマインドのみ）。
#
# ── 設計上の制約（壊すと本番が止まる。触る前に必ず読む） ─────────────
# (1) headless(claude -p)では絶対にブロックしない。
#     candidate-eval の書類選考採点は claude -p のサブプロセスで動くため、ここで
#     decision:block を返すとモデルが求められたJSONの代わりに散文を返し**採点が失敗する**
#     (2026-08-21 実測: 10:21/10:25 の採点2件が本フック起因で失敗)。
#     → CC_SKIP_STOP_HOOKS があれば即 exit 0（従来どおり）。
# (2) 同一セッションで差し戻すのは1回だけ。
#     Stop hook の block は「モデルを続行させる」ため、無条件に返すと無限ループになる。
#     → セッションIDごとにフラグを置き、2回目以降は systemMessage（非ブロック）に降格。
# (3) キルスイッチ: ~/.claude/hooks/off/stop-verify-reminder.off があれば全機能停止。
#     可用性を人質に取らない（security-model 横断原則）。
#
# 入力: stdin JSON { session_id, transcript_path, ... }
# 出力: 差し戻し時 {"decision":"block","reason":"..."} / 降格時 {"systemMessage":"..."} / 他は無出力

# ★稼働ハートビート。このフックはログを出さないため「最後にいつ動いたか」を残す。
#   参照: ~/.claude/session-ledger/collect_automations_extra.py
date +%s > "/tmp/cc-hook-heartbeat-stop-verify-reminder" 2>/dev/null || true

set -u

# ── (3) キルスイッチ ────────────────────────────────────────────
[ -f "$HOME/.claude/hooks/off/stop-verify-reminder.off" ] && exit 0

# ── (1) headless では発火しない（本番採点を壊さないための最優先ガード） ──
[ -n "${CC_SKIP_STOP_HOOKS:-}" ] && exit 0

# ── (4) jq 不在は「無言で全停止」させない（レビュー指摘B-4） ──────────────
#   ハートビートだけ更新され続けてゲートは死んでいる、が最悪の見え方。
if ! command -v jq >/dev/null 2>&1; then
  cat <<'JSON'
{"systemMessage":"⚠️ [[verify-reminder]] jq が無いため検証ゲートは無効です（コード変更時の差し戻しが行われません）。","suppressOutput":true}
JSON
  exit 0
fi

INPUT=$(cat)

TRANSCRIPT=$(echo "$INPUT" | jq -r '.transcript_path // empty' 2>/dev/null)
[ -z "$TRANSCRIPT" ] || [ ! -f "$TRANSCRIPT" ] && exit 0

# stop_hook_active: 既にこのStopがhook起因で再開している場合は二重発火させない（公式フラグ）。
ACTIVE=$(echo "$INPUT" | jq -r '.stop_hook_active // false' 2>/dev/null)
[ "$ACTIVE" = "true" ] && exit 0

# ★「Edit/Writeが在る」と「どこかに.pyが在る」を別々に見ると、**.pyをReadしただけ**の
#   調査セッションでも差し戻す（レビュー指摘B-3・実測で誤block再現）。
#   → 同一 tool_use の中で「編集ツール名 → file_path が該当拡張子」が繋がる場合だけ拾う。
CODE_EDIT_RE='"name":"(Edit|Write|NotebookEdit)","input":\{[^}]*"file_path":"[^"]+\.(js|ts|py|gs|jsx|tsx|sh|vue|go)"'
grep -q -E "$CODE_EDIT_RE" "$TRANSCRIPT" 2>/dev/null || exit 0

# このhook自身が過去に出した文面を判定材料から除外する(自家中毒防止)。
# マーカー [[verify-reminder]] を仕込み、痕跡判定の前に grep -v で消す。
# ※単語("code-review"等)での判定は、hook自身の文面や会話の話題に必ずヒットして
#   一度出たら永久沈黙する死コードになるため使わない。
# ★編集より**前**の Agent 起動やテスト実行を「検証した証拠」に数えると、調査で一度
#   Explore を起動しただけでゲートが素通りする（レビュー指摘B-2・実測）。
#   → 最後のコード編集より後ろの行だけを判定対象にする。34MBの全走査も避けられる(B-5)。
LAST=$(grep -n -E "$CODE_EDIT_RE" "$TRANSCRIPT" 2>/dev/null | tail -1 | cut -d: -f1)
[ -z "$LAST" ] && LAST=1
STRIPPED=$(tail -n "+$LAST" "$TRANSCRIPT" 2>/dev/null | grep -v 'verify-reminder')

# 検証/レビューが「実際に実行された」痕跡だけを見る(語の言及ではなくツール実体)。
if printf '%s' "$STRIPPED" | grep -q -E \
  '"command":"[^"]*(node --check|python3? -m py_compile|ast\.parse|pytest|npm (run )?test|--dry|jq empty|bash -n)' \
  2>/dev/null; then
  exit 0
fi
if printf '%s' "$STRIPPED" | grep -q -E \
  '"name":"Skill"[^}]*"skill":"[^"]*(code-review|verification|requesting-code-review)' \
  2>/dev/null; then
  exit 0
fi
# 独立レビュー = サブエージェント起動でも可（CLAUDE.md 本格版レビュー）。
if printf '%s' "$STRIPPED" | grep -q -E '"name":"(Agent|Task)"' 2>/dev/null; then
  exit 0
fi

# ── (2) 同一セッション2回目以降は降格（無限ループ防止） ──────────────
SID=$(echo "$INPUT" | jq -r '.session_id // empty' 2>/dev/null)
[ -z "$SID" ] && SID="nosid"
# セッションIDはパス材料になるため英数・ハイフン以外を落とす（パストラバーサル防止）。
SAFE_SID=$(printf '%s' "$SID" | tr -cd 'A-Za-z0-9_-' | cut -c1-64)
[ -z "$SAFE_SID" ] && SAFE_SID="nosid"
# フラグは $HOME 配下に置く（/tmp は tmpwatch/再起動で消え、消えると「1回だけ」が
# 保証されず何度でも差し戻す。レビュー指摘 2026-09-03）。
FLAG_DIR="${XDG_STATE_HOME:-$HOME/.claude/state}/verify-gate"
mkdir -p "$FLAG_DIR" 2>/dev/null || true
FLAG="$FLAG_DIR/$SAFE_SID"

# 古いフラグを掃除する（セッションごとに1ファイル増えるため放置すると溜まり続ける）。
# 30日より古いものだけ消す＝現行セッションのフラグは絶対に触らない。
find "$FLAG_DIR" -type f -mtime +30 -delete 2>/dev/null || true

# ★フェイルオープン: フラグを**書けない**環境ではブロックしない。
#   書けないまま block を返すと「1回だけ」が効かず毎回差し戻す＝作業が進まなくなる
#   （実測: ディレクトリを読取専用にすると2回連続でblockした）。
#   可用性を人質に取らない（security-model 横断原則）。
if ! ( : > "$FLAG.probe" ) 2>/dev/null; then
  rm -f "$FLAG.probe" 2>/dev/null || true
  cat <<'JSON'
{"systemMessage":"💡 [[verify-reminder]] コードを変更しました。完了とする前に検証（テスト実行か独立レビュー）を挟んでください。※ゲート用の状態ファイルを書けないため今回は差し戻しません。","suppressOutput":true}
JSON
  exit 0
fi
rm -f "$FLAG.probe" 2>/dev/null || true

if [ -f "$FLAG" ]; then
  # 既に1回差し戻し済み → もう止めない。1行だけ添えて通す。
  cat <<'JSON'
{"systemMessage":"💡 [[verify-reminder]] 検証の痕跡がまだありません（差し戻しは1回のみのため通します）。完了と報告する前に、テスト実行か独立レビューを必ず挟んでください。","suppressOutput":true}
JSON
  exit 0
fi

# 初回 → 差し戻す。何をすれば通るのかを具体的に示す（曖昧な叱責にしない）。
# ★フラグ書込に失敗したらブロックしない（上のprobeで担保済みだが二重に守る）。
if ! ( : > "$FLAG" ) 2>/dev/null; then
  cat <<'JSON'
{"systemMessage":"💡 [[verify-reminder]] コードを変更しました。完了前に検証を挟んでください。","suppressOutput":true}
JSON
  exit 0
fi
cat <<'JSON'
{"decision":"block","reason":"[[verify-reminder]] 監督AIゲート: コードを変更しましたが、検証を実行した痕跡がありません。CLAUDE.md『実装後：独立レビュー必須（自己レビューで完結させない）』に従い、完了とする前に次のいずれかを実施してください。\n(a) 構文/テストを実際に走らせる（node --check / python3 -m py_compile / pytest / npm test / bash -n / --dry-run のいずれか）\n(b) Agent ツールで批判役レビュアーを起動して独立レビューを受ける\n(c) code-review / verification-before-completion スキルを実行する\n実施したら、その結果（成功・失敗の実出力）を踏まえて完了報告をしてください。テストが落ちた場合は落ちたと正直に報告すること。\n※このゲートは同一セッションで1回だけ差し戻します。どうしても不要な場合はそのまま続行すれば通ります。"}
JSON
exit 0
