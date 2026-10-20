#!/usr/bin/env python3
"""
Собирает справочник категорий для сервера (data/categories.json) из сохранённых страниц Авито:
дерево категорий из шапки, деревья из футера (там подкатегории «Услуг») и плитки витрин
(category-widget/sliced-link). Офлайн, без загрузок.
  .venv/Scripts/python.exe research/build_catalog.py
"""
import html as htmllib
import json
import re
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(HERE))
from extract_categories import find_trees, preloaded_state  # noqa: E402

OUT = ROOT / "data" / "categories.json"
SKIP_PREFIXES = ("business360", "rabota/", "all")


def norm_path(url: str) -> str:
    path = url.split("?")[0].split("#")[0]
    path = re.sub(r"^https?://[^/]+", "", path)
    parts = [p for p in path.split("/") if p]
    return "/".join(parts[1:])  # без города (/all/, /kazan/ …)


def slug(path: str) -> str:
    return re.sub(r"/?-?ASgB[^/]*$", "", path).rstrip("/")


def walk(nodes, parent, out):
    for n in nodes:
        path = norm_path(n.get("url") or "")
        name = (n.get("name") or "").strip()
        if path and name and not path.startswith(SKIP_PREFIXES):
            out.append({"name": name, "parent": parent, "path": path})
        walk(n.get("subs") or [], name if name != "Все категории" else None, out)


def tiles(html: str, parent: str):
    res = []
    for m in re.finditer(r'<a[^>]*data-marker="category-widget/sliced-link"[^>]*href="([^"]+)"[^>]*>(.*?)</a>', html, re.S):
        name = htmllib.unescape(re.sub(r"<[^>]+>", " ", m.group(2)))
        name = re.sub(r"\s+", " ", name).strip()
        if name and not name.lower().startswith("все категории"):
            res.append({"name": name, "parent": parent, "path": norm_path(htmllib.unescape(m.group(1)))})
    return res


def main():
    items = []
    for f in sorted((HERE / "dumps").rglob("*.html")):
        text = f.read_text(encoding="utf-8")
        state = preloaded_state(text)
        for _, tree in find_trees(state):
            walk(tree, None, items)
        if "category-widget/sliced-link" in text:
            items += tiles(text, None)
    # родитель плитки — категория, чей слаг (без хвоста -ASgB…) совпадает с началом пути плитки
    by_slug = {slug(it["path"]): it["name"] for it in items if it["parent"] is not None or "/" not in it["path"]}
    for it in items:
        if it["parent"] is None and "/" in it["path"]:
            parts = slug(it["path"]).split("/")
            for k in range(len(parts) - 1, 0, -1):
                if "/".join(parts[:k]) in by_slug:
                    it["parent"] = by_slug["/".join(parts[:k])]
                    break
    seen, result = set(), []
    for it in items:
        key = (it["name"].lower(), it["path"])
        if key in seen or not re.match(r"^[A-Za-z0-9_%'.~-]+(/[A-Za-z0-9_%'.~-]+)*$", it["path"]):
            continue
        seen.add(key)
        result.append(it)
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(result, ensure_ascii=False, indent=0), encoding="utf-8")
    print(f"{len(result)} категорий -> {OUT.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
