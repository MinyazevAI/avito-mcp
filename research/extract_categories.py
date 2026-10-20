#!/usr/bin/env python3
"""
Извлекает дерево категорий Авито (рубрикатор) из сохранённого HTML страницы выдачи.
Рубрикатор лежит в window.__preloadedState__ (JSON внутри JS-строки): узлы {categoryId, id, level, name, url, subs}.
  .venv/Scripts/python.exe research/extract_categories.py research/dumps/flats/serp_full.html research/categories.json
"""
import json
import re
import sys
from pathlib import Path


def preloaded_state(html: str) -> dict:
    m = re.search(r'window\.__preloadedState__\s*=\s*("(?:[^"\\]|\\.)*")', html)
    return json.loads(json.loads(m.group(1))) if m else {}


def find_trees(obj, path=""):
    """Ищет списки узлов вида {name, url, subs}. Возвращает (путь в state, список)."""
    if isinstance(obj, list):
        if obj and all(isinstance(x, dict) and {"name", "url", "subs"} <= x.keys() for x in obj):
            yield path, obj
            return
        for i, x in enumerate(obj):
            yield from find_trees(x, f"{path}[{i}]")
    elif isinstance(obj, dict):
        for k, v in obj.items():
            yield from find_trees(v, f"{path}.{k}")


def flatten(nodes, parent=None, out=None):
    out = [] if out is None else out
    for n in nodes:
        path = (n.get("url") or "").split("?")[0]
        item = {"name": n.get("name"), "level": n.get("level"), "category_id": n.get("categoryId"),
                "path": re.sub(r"^/[^/]+/", "", path), "parent": parent}
        out.append(item)
        flatten(n.get("subs") or [], n.get("name"), out)
    return out


if __name__ == "__main__":
    state = preloaded_state(Path(sys.argv[1]).read_text(encoding="utf-8"))
    print("state keys:", list(state)[:40])
    best = []
    for p, tree in find_trees(state):
        flat = flatten(tree)
        print(f"tree at {p}: top={len(tree)} total={len(flat)}")
        if len(flat) > len(best):
            best = flat
    if len(sys.argv) > 2:
        Path(sys.argv[2]).write_text(json.dumps(best, ensure_ascii=False, indent=1), encoding="utf-8")
    print(len(best), "categories")
