#!/usr/bin/env python3
"""memory/ 索引の**ベクトル層**（RAG化 第2層）。

第1層(index.json/msearch.py)は語の一致で引く。言い換え（「無音」↔「通知が来ない」）に
弱いため、埋め込みで意味検索を足す。**無くても第1層だけで完全に動く**。

方式: ローカル埋め込み（intfloat/multilingual-e5-small・384次元）。
  ★APIキー不要・課金なし・**memoryの内容が外部に一切送信されない**（2026-09-03 利用者判断）。
  Anthropic には埋め込みAPIが無く、Voyage/OpenAI は新規課金＋外部送信になるため見送った。
  実測: 441件の埋め込みに0.8秒（M系Mac・CPU）。

前提（未導入なら何もせず exit 3・第1層は無傷）:
  ~/.claude/memory-index/.venv に sentence-transformers + numpy
  初回のみモデルを自動DL（約120MB・以後はローカルキャッシュ）

使い方:
  ./.venv/bin/python build_vectors.py          # 埋め込みを作る
  ./.venv/bin/python build_vectors.py --check  # 前提確認だけ
出力: vectors.npz（names / matrix）
"""
from __future__ import annotations
import json, sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
IDX, OUT = HERE/"index.json", HERE/"vectors.npz"
MODEL = "intfloat/multilingual-e5-small"


def preflight() -> tuple[bool, str]:
    try:
        import sentence_transformers, numpy  # noqa: F401
    except ImportError as e:
        return False, (f"ライブラリ未導入: {e.name}。"
                       f"{HERE}/.venv/bin/python -m pip install sentence-transformers numpy")
    if not IDX.exists():
        return False, "index.json が無い（先に build_index.py）"
    return True, "OK（ローカル埋め込み・APIキー不要）"


def main() -> int:
    ok, why = preflight()
    if "--check" in sys.argv:
        print(("✅ 前提OK: " if ok else "⚠️  ベクトル層は無効: ") + why)
        if OUT.exists():
            print(f"   vectors.npz あり（{OUT.stat().st_size/1024:.0f}KB）")
        print("   ※第1層(msearch.py)はベクトル無しでも完全に動作します。")
        return 0 if ok else 3
    if not ok:
        print(f"⚠️  ベクトル層をスキップ: {why}", file=sys.stderr)
        return 3

    from sentence_transformers import SentenceTransformer
    import numpy as np
    recs = json.loads(IDX.read_text(encoding="utf-8"))["records"]
    # 全文ではなく name+description+先頭行だけ（要点は description に凝縮済み・安価）
    docs = [f"{r['name']} {r.get('description','')} {r.get('first_line','')}"[:512] for r in recs]
    m = SentenceTransformer(MODEL)
    # e5 系は "passage: " / "query: " の接頭辞が前提（付け忘れると精度が落ちる）
    M = m.encode(["passage: " + d for d in docs], normalize_embeddings=True,
                 batch_size=64, show_progress_bar=False).astype("float32")
    np.savez_compressed(OUT, names=np.array([r["name"] for r in recs]), matrix=M)
    print(f"✅ vectors.npz 作成: {M.shape[0]}件 x {M.shape[1]}次元 / {OUT.stat().st_size/1024:.0f}KB")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
