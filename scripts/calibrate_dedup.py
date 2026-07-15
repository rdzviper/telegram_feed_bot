"""Калибровка порога cosine для семантического дедупа (A1).

Гоняется ЛОКАЛЬНО. Читает золотой сет `tests/dedup_cases.md`, для каждого поста
берёт summary у DeepSeek и вектор у эмбеддинг-API, строит матрицу cosine и
подбирает порог: все пары внутри кластера (одна новость) должны схлопываться,
пары между кластерами (разные новости) — нет.

Запуск (из корня репо):
    python -m scripts.calibrate_dedup
Нужны в .env: DEEPSEEK_API_KEY (для summary) и EMBEDDING_API_KEY (для векторов).
"""
import asyncio
import itertools
import os
import sys

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import analyzer  # noqa: E402
import config  # noqa: E402
import embeddings  # noqa: E402

CASES_MD = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "tests", "dedup_cases.md"
)
SWEEP = [0.72, 0.75, 0.78, 0.80, 0.82, 0.84, 0.86, 0.88, 0.90, 0.92]


def parse_cases(path: str) -> list[tuple[str, str, str]]:
    """Возвращает [(cluster, post_id, text)] — только посты со своим текстом
    (в fenced-блоке). Verbatim-дубли без текста (для текстового слоя) пропускаются."""
    posts, cluster, post_id, in_code, buf = [], None, None, False, []
    with open(path, encoding="utf-8") as f:
        for line in f:
            s = line.rstrip("\n")
            if s.startswith("## Кластер"):
                cluster, post_id = s, None
            elif s.startswith("### "):
                post_id = s[4:].strip()
            elif s.strip().startswith("```"):
                if not in_code:
                    in_code, buf = True, []
                else:
                    in_code = False
                    text = "\n".join(buf).strip()
                    if cluster and post_id and text:
                        posts.append((cluster, post_id, text))
                    post_id = None
            elif in_code:
                buf.append(s)
    return posts


async def main():
    posts = parse_cases(CASES_MD)
    print(f"Разобрано постов с текстом: {len(posts)}")
    for c, pid, _ in posts:
        print(f"  {c.split('—')[0].strip():<14} {pid}")

    if not config.EMBEDDING_API_KEY:
        print("\n⚠ EMBEDDING_API_KEY не задан — cosine посчитать нельзя.")
        print("  Парсинг сета отработал. Добавь ключ в .env и запусти снова.")
        return

    print("\nСчитаю summary (DeepSeek) + вектора (эмбеддинг-API)...")
    labels, vecs = [], []
    for cluster, pid, text in posts:
        res = await analyzer.analyze(text)
        summary = res.get("summary", "")
        v = await embeddings.embed(summary)
        if v is None:
            print(f"  ! {pid}: вектор не получен, пропуск")
            continue
        labels.append((cluster, pid, summary))
        vecs.append(v)

    within, cross = [], []
    for i, j in itertools.combinations(range(len(vecs)), 2):
        cos = float(vecs[i] @ vecs[j])
        pair = (cos, labels[i][1], labels[j][1])
        (within if labels[i][0] == labels[j][0] else cross).append(pair)

    if not within or not cross:
        print("Мало данных для калибровки (нужны и внутри-, и меж-кластерные пары).")
        return

    print("\n— Пары ВНУТРИ кластера (должны быть дублями), по возрастанию cosine:")
    for cos, a, b in sorted(within):
        print(f"  {cos:.3f}  {a}  ↔  {b}")
    print("\n— Пары МЕЖДУ кластерами (НЕ дубли), по убыванию cosine (топ-10):")
    for cos, a, b in sorted(cross, reverse=True)[:10]:
        print(f"  {cos:.3f}  {a}  ↔  {b}")

    min_within = min(c for c, _, _ in within)
    max_cross = max(c for c, _, _ in cross)
    print(f"\nmin(внутри)={min_within:.3f}  max(между)={max_cross:.3f}  "
          f"зазор={min_within - max_cross:+.3f}")

    print("\nτ      recall(дубли)  false-merge(разное)")
    for tau in SWEEP:
        rec = sum(1 for c, _, _ in within if c >= tau) / len(within)
        fp = sum(1 for c, _, _ in cross if c >= tau) / len(cross)
        print(f"{tau:.2f}   {rec:6.0%}          {fp:6.0%}")

    # рекомендация: самый высокий τ с нулём ложных склеек, максимизирующий recall
    safe = [t for t in SWEEP if all(c < t for c, _, _ in cross)]
    if safe:
        rec = sum(1 for c, _, _ in within if c >= min(safe)) / len(within)
        print(f"\nРекоменд. старт (строгий, 0 ложных склеек): τ={min(safe):.2f} "
              f"→ ловит {rec:.0%} дублей. Снижать по мере уверенности.")
    else:
        print("\nНет τ из сетки с нулём ложных склеек — сет пересекается, смотреть пары выше.")


if __name__ == "__main__":
    asyncio.run(main())
