#!/usr/bin/env python3
"""memory/ の事前分析インデックス作成（RAG化 第1層・ローカル/APIコスト0）。

ax_channel事例「①情報を与える→②AIが事前に分析→③DBに保存→④検索」の②③に相当。
毎回LLMが2.5MB全文を読む方式をやめ、**事前に構造化した索引**を引く形にする。

出力: index.json … 1メモリ=1レコード（name/type/topic/keywords/links/mtime/summary/path/size）
使い方:
  python3 build_index.py            # 索引を作る/更新する
  python3 build_index.py --stats    # 索引の統計だけ表示
"""
from __future__ import annotations
import json, os, re, sys, unicodedata
from pathlib import Path

# memory の置き場。環境変数 CC_MEMORY_DIR で上書きできる。
# 既定は Claude Code がプロジェクトごとに作る memory ディレクトリ。
# 例: ~/.claude/projects/<プロジェクトのスラッグ>/memory
def _default_memory_dir() -> Path:
    """memory の置き場を決める。環境変数 CC_MEMORY_DIR で明示指定できる。

    未指定なら ~/.claude/projects/*/memory のうち **.md が最も多いもの** を選ぶ。
    （プロジェクトが複数あると単純な先頭一致では空のディレクトリを掴むため）
    """
    env = os.environ.get("CC_MEMORY_DIR")
    if env:
        return Path(env).expanduser()
    cands = sorted(Path.home().glob(".claude/projects/*/memory"))
    if not cands:
        return Path.home()/".claude"/"memory"
    return max(cands, key=lambda d: len(list(d.glob("*.md"))))


MEM = _default_memory_dir()
OUT = Path(__file__).resolve().parent/"index.json"

# トピック辞書: 表層語 → 正規トピック。検索時の絞り込み軸になる。
# ★ここは**自分の仕事の語彙に置き換えて使う**。既定は汎用的な開発語彙のみ。
#   案件名・製品名・社名を足すと絞り込みが一気に効くようになる。
TOPIC_RULES = [
    ("api",         ["api", "endpoint", "rest", "webhook"]),
    ("gas",         ["gas", "clasp", "apps script", "webapp", "web app"]),
    ("launchd",     ["launchd", "plist", "cron", "trigger", "トリガー", "定期実行"]),
    ("slack",       ["slack", "dm", "チャンネル"]),
    ("sheets",      ["スプシ", "管理表", "スプレッドシート", "sheet", "gid"]),
    ("drive",       ["drive", "ドライブ", "フォルダ"]),
    ("mail",        ["gmail", "メール", "mail", "送信"]),
    ("db",          ["sqlite", "postgres", "mysql", "クエリ", "テーブル"]),
    ("testing",     ["テスト", "pytest", "ミューテーション", "canary", "カナリア"]),
    ("security",    ["セキュリティ", "キー", "秘密", "credential", "keychain"]),
    ("naming",      ["氏名", "名前", "表記", "呼称"]),
    ("alert",       ["アラート", "通知", "無音", "dedup"]),
    ("dashboard",   ["ダッシュボード", "kpi", "集計", "メトリク"]),
    ("session",     ["セッション", "開発ログ", "台帳"]),
]

STOP = set("""これ それ あれ この その ある いる する なる れる られる こと もの ため よう
の に は を が と で も や から まで より へ ね よ か な だ です ます した して いる ない
and the for with that this from not are was were will has have had you your""".split())

def norm(s: str) -> str:
    return unicodedata.normalize("NFKC", s).lower()

def parse_front(text: str) -> tuple[dict, str]:
    """--- で囲まれた frontmatter を素朴に読む（PyYAML非依存）。"""
    meta, body = {}, text
    if text.startswith("---"):
        end = text.find("\n---", 3)
        if end != -1:
            raw, body = text[3:end], text[end+4:]
            for line in raw.splitlines():
                m = re.match(r"^\s{0,4}([A-Za-z_]+):\s*(.*)$", line)
                if m:
                    k, v = m.group(1), m.group(2).strip()
                    if v and k not in meta:
                        meta[k] = v
    return meta, body

def topics_of(name: str, desc: str, body: str) -> list[str]:
    """トピックは「どこに出たか」で重み付けする。

    本文の通りすがりの1語で全件にタグが付くと絞り込みに使えない（実測: 素朴な
    部分一致では mail が439件中262件に付き、フィルタとして機能しなかった）。
    → ファイル名/description は強い証拠、本文は出現回数が閾値を超えたときだけ採用。
    """
    nm, dc, bd = norm(name), norm(desc), norm(body)
    out = []
    for t, kws in TOPIC_RULES:
        score = 0
        for k in kws:
            if k in nm:
                score += 5           # ファイル名にある = そのものズバリ
            if k in dc:
                score += 3           # description にある = 主題である
            score += min(bd.count(k), 6)  # 本文は頻度（上限つき・1回では付けない）
        if score >= 4:
            out.append(t)
    return out

def keywords_of(blob: str, limit: int = 14) -> list[str]:
    """日本語は2-4gramの頻出漢字/カタカナ列、英語は単語で拾う素朴抽出。"""
    n = norm(blob)
    freq: dict[str, int] = {}
    for w in re.findall(r"[a-z_][a-z0-9_\-]{2,}", n):
        if w not in STOP and not w.isdigit():
            freq[w] = freq.get(w, 0) + 1
    for w in re.findall(r"[一-鿿]{2,6}|[ァ-ヶー]{3,8}", blob):
        if w not in STOP:
            freq[w] = freq.get(w, 0) + 2  # 日本語の内容語を優遇
    return [w for w, _ in sorted(freq.items(), key=lambda x: -x[1])[:limit]]

def build() -> list[dict]:
    recs = []
    for p in sorted(MEM.glob("*.md")):
        if p.name in ("MEMORY.md", "MEMORY_ARCHIVE_INDEX.md"):
            continue
        try:
            text = p.read_text(encoding="utf-8")
        except Exception as e:
            print(f"skip {p.name}: {e}", file=sys.stderr); continue
        meta, body = parse_front(text)
        desc = meta.get("description", "").strip()
        # 本文の最初の意味のある行（見出し/空行を除く）を要約補助に使う
        first = ""
        for line in body.splitlines():
            s = line.strip()
            if s and not s.startswith("#") and not s.startswith("---"):
                first = s[:200]; break
        blob = f"{p.stem} {desc} {body[:4000]}"
        tps = topics_of(p.stem, desc, body)
        recs.append({
            "name": meta.get("name", p.stem),
            "path": str(p),
            "type": meta.get("type", "unknown"),
            "description": desc,
            "first_line": first,
            "topics": tps,
            "keywords": keywords_of(blob),
            "links": sorted(set(re.findall(r"\[\[([^\]]+)\]\]", body)))[:20],
            "starred": body.count("⭐"),
            "critical": "🔴" in body,
            "size": len(text),
            "mtime": p.stat().st_mtime,
        })
    return recs

def main() -> int:
    if "--stats" in sys.argv and OUT.exists():
        recs = json.loads(OUT.read_text(encoding="utf-8"))["records"]
    else:
        recs = build()
        OUT.write_text(json.dumps({"version": 1, "count": len(recs), "records": recs},
                                  ensure_ascii=False, indent=1), encoding="utf-8")
        print(f"✅ index.json 作成: {len(recs)}件 / {OUT.stat().st_size/1024:.0f}KB")
        # 第2層があるなら件数を揃える（古いまま残すと意味検索が黙って無効になる）
        vec = OUT.parent/"vectors.npz"
        venv = OUT.parent/".venv/bin/python"
        if vec.exists() and venv.exists():
            import subprocess
            r = subprocess.run([str(venv), str(OUT.parent/"build_vectors.py")],
                               capture_output=True, text=True, timeout=600)
            print("  " + (r.stdout.strip().splitlines() or ["ベクトル層の更新に失敗"])[-1])
    from collections import Counter
    print("type:", dict(Counter(r["type"] for r in recs)))
    tc = Counter(t for r in recs for t in r["topics"])
    print("topic上位:", dict(tc.most_common(12)))
    print("トピック無し:", sum(1 for r in recs if not r["topics"]), "件")
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
