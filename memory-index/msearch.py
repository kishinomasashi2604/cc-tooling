#!/usr/bin/env python3
"""memory/ 索引検索（RAG化 第1層の検索側）。全文2.5MBを読まずに該当メモリだけを引く。

使い方:
  msearch.py "deploy 手順"           # キーワード検索（上位10件）
  msearch.py "canary" --topic testing      # トピックで絞る
  msearch.py "送信" --type feedback -n 5   # 種別で絞る／件数指定
  msearch.py --topic naming --list         # トピック一覧表示
  msearch.py "API" --show 1               # 1位のメモリ本文を表示（これだけ全文を読む）
  msearch.py --topics                      # 利用可能なトピック一覧

出力は「name / type / description / path」。中身が要るものだけ --show で開く。
"""
from __future__ import annotations
import os
import argparse, json, re, sys, unicodedata
from pathlib import Path

IDX = Path(__file__).resolve().parent/"index.json"

def norm(s: str) -> str:
    return unicodedata.normalize("NFKC", s).lower()

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

SKIP = ("MEMORY.md", "MEMORY_ARCHIVE_INDEX.md")

def _stale() -> bool:
    """memory/ と索引がずれていれば陳腐化とみなす。

    「在るものを無いと報告しない」だけでなく、**無いものを在ると報告しない**ことも要る。
    mtimeだけ見ると削除が検知できず、消したメモリを現存として返す（レビュー指摘C-1・実証済み）。
    → 件数のずれも条件に入れる（追加でも削除でも件数が変わる）。再構築は実測0.2秒。
    """
    if not IDX.exists():
        return True
    try:
        it = IDX.stat().st_mtime
        files = [p for p in MEM.glob("*.md") if p.name not in SKIP]
        n = json.loads(IDX.read_text(encoding="utf-8")).get("count", -1)
        if len(files) != n:
            return True
        return any(p.stat().st_mtime > it for p in files)
    except Exception:
        # ★判断できないときは作り直す。False（＝古い索引を使う）は危険側フェイル
        #   （レビュー指摘C-2）。正しさの判定は fail-safe にする。
        return True

def load() -> list[dict]:
    if _stale():
        import subprocess
        try:
            r = subprocess.run([sys.executable, str(Path(__file__).resolve().parent/"build_index.py")],
                               capture_output=True, text=True, timeout=120)
            if r.returncode != 0:
                # 握り潰すと「更新されたつもりで古い索引を引く」のが一番危ない（C-3）
                err = (r.stderr or "").strip().splitlines()
                print(f"⚠️ 索引の再構築に失敗（結果は古い可能性）: {err[-1] if err else 'rc=%d' % r.returncode}",
                      file=sys.stderr)
        except Exception as e:  # noqa: BLE001
            print(f"⚠️ 索引の再構築を実行できず（結果は古い可能性）: {type(e).__name__}", file=sys.stderr)
    if not IDX.exists():
        print("索引がありません。先に build_index.py を実行してください。", file=sys.stderr)
        raise SystemExit(2)
    return json.loads(IDX.read_text(encoding="utf-8"))["records"]

VEC = Path(__file__).resolve().parent/"vectors.npz"

def _semantic(query: str, names: list[str]) -> dict[str, float]:
    """意味的な近さを 0〜1 で返す。使えない環境では空dict（＝第1層のまま）。"""
    if not VEC.exists():
        return {}
    try:
        import numpy as np
        from sentence_transformers import SentenceTransformer
    except ImportError:
        return {}
    try:
        z = np.load(VEC, allow_pickle=False)
        M, vnames = z["matrix"], [str(x) for x in z["names"]]
        if len(vnames) != len(names):
            # 索引と埋め込みの件数がずれている＝vectors.npz が古い。誤った類似度を
            # 混ぜるより使わない方が安全（黙って第1層に落とす）。
            print("⚠️ vectors.npz が索引と一致しません（build_vectors.py で作り直してください）",
                  file=sys.stderr)
            return {}
        m = SentenceTransformer("intfloat/multilingual-e5-small")
        q = m.encode(["query: " + query], normalize_embeddings=True)[0].astype("float32")
        sims = M @ q
        lo, hi = float(sims.min()), float(sims.max())
        rng = (hi - lo) or 1.0
        # e5 は絶対値が 0.8 付近に固まるため、順位が意味を持つよう min-max で伸ばす
        return {n: (float(v) - lo) / rng for n, v in zip(vnames, sims)}
    except Exception as e:  # noqa: BLE001
        print(f"⚠️ 意味検索を使えませんでした（第1層で継続）: {type(e).__name__}", file=sys.stderr)
        return {}


def score(rec: dict, terms: list[str]) -> float:
    """出現場所で重み付け: 名前 > description > keywords/topics > 本文先頭。"""
    if not terms:
        return 0.0
    nm, dc = norm(rec["name"]), norm(rec.get("description", ""))
    kw = norm(" ".join(rec.get("keywords", []) + rec.get("topics", [])))
    fl = norm(rec.get("first_line", ""))
    s = 0.0
    for t in terms:
        if t in nm: s += 6
        if t in dc: s += 4
        if t in kw: s += 2.5
        if t in fl: s += 1.5
    if s:
        # 重要度で微加点（⭐/🔴 は利用者が重要と印を付けたもの）
        s += min(rec.get("starred", 0), 2) * 0.5
        if rec.get("critical"): s += 0.8
    return s

def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("query", nargs="*", help="検索語（スペース区切り・AND寄りのOR）")
    ap.add_argument("--type", help="project / feedback / reference / user")
    ap.add_argument("--topic", help="トピックで絞る（--topics で一覧）")
    ap.add_argument("-n", type=int, default=10, help="表示件数（既定10）")
    ap.add_argument("--list", action="store_true", help="スコア0でも条件一致を全部出す")
    ap.add_argument("--topics", action="store_true", help="トピック一覧を出して終了")
    ap.add_argument("--show", type=int, metavar="RANK", help="指定順位のメモリ本文を表示")
    ap.add_argument("--semantic", action="store_true",
                    help="意味検索を併用（言い換えに強い。第2層 vectors.npz が要る）")
    ap.add_argument("--no-semantic", action="store_true", help="意味検索を使わない")
    a = ap.parse_args()

    recs = load()
    if a.topics:
        from collections import Counter
        for t, c in Counter(x for r in recs for x in r["topics"]).most_common():
            print(f"{t:14s} {c}")
        return 0

    if a.type:
        recs = [r for r in recs if r["type"] == a.type]
    if a.topic:
        recs = [r for r in recs if a.topic in r["topics"]]

    # 引用符付きで丸ごと1語として渡されても動くよう、空白で割ってから正規化する
    # （実測: msearch.py "利用者 氏名" が0件になった。語ごとに分けないと部分一致しない）
    terms = [norm(w) for t in a.query for w in t.split() if w.strip()]

    # ── 第2層: 意味検索スコアを混ぜる（ハイブリッド） ────────────────
    # 語が一致しない言い換え（「無音」↔「通知が来ない」）を拾う。
    # vectors.npz やライブラリが無ければ**黙って第1層のまま**動く（degradation）。
    vec_score: dict[str, float] = {}
    if terms and not a.no_semantic:
        vec_score = _semantic(" ".join(a.query), [r["name"] for r in recs])
    # キーワード点に意味点を加算する。ただし**意味点でキーワードの順位を壊さない**。
    #   実測: 重み4.0だと「deploy 手順」の1位が正解(deadline_boundary)から
    #   別memoryへ入れ替わった。意味検索の役割は「語が当たらないときに拾う」ことなので、
    #   キーワードが十分当たっている候補の並びは動かさないのが正しい。
    #   → 重みを下げ(1.5)、かつ**意味点は上位20件だけに与える**（薄く広く足すと
    #     無関係な441件全部に下駄を履かせて相対順位が崩れる）。
    if vec_score:
        top_sem = sorted(vec_score.items(), key=lambda x: -x[1])[:20]
        vec_score = {n: v for n, v in top_sem}
    scored = [(score(r, terms) + vec_score.get(r["name"], 0.0) * 1.5, r) for r in recs]
    if terms and not a.list:
        scored = [(s, r) for s, r in scored if s > 0]
    scored.sort(key=lambda x: (-x[0], -x[1]["mtime"]))
    hits = scored[:a.n]

    if a.show is not None:
        if not 1 <= a.show <= len(hits):
            print(f"順位 {a.show} は範囲外（{len(hits)}件）", file=sys.stderr); return 2
        p = Path(hits[a.show-1][1]["path"])
        if not p.exists():
            print(f"このメモリは既に削除されています（索引が古い）: {p.name}\n"
                  f"→ python3 build_index.py で索引を作り直してください。", file=sys.stderr)
            return 2
        print(p.read_text(encoding="utf-8"))
        return 0

    if not hits:
        print("該当なし。語を減らすか --topics でトピックを確認してください。")
        return 1
    print(f"{len(scored)}件ヒット（上位{len(hits)}件）")
    for i, (s, r) in enumerate(hits, 1):
        mark = "🔴" if r.get("critical") else ("⭐" if r.get("starred") else "  ")
        print(f"\n{i:2d}. {mark} [{r['type']}] {r['name']}  (score {s:.1f})")
        if r.get("topics"): print(f"     topics: {', '.join(r['topics'])}")
        d = r.get("description") or r.get("first_line", "")
        if d: print(f"     {d[:150]}")
    print(f"\n本文を読む: msearch.py {' '.join(a.query)} --show <順位>")
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
