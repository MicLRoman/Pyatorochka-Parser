# -*- coding: utf-8 -*-
# Разбивает products.json на отдельные файлы по категориям (папка by_category).
# Запуск:  py.exe split.py            (берёт products.json)
#          py.exe split.py имя.json   (берёт другой файл)

import json
import os
import re
import sys

src = sys.argv[1] if len(sys.argv) > 1 else "products.json"
outdir = "by_category_category"

with open(src, encoding="utf-8") as f:
    data = json.load(f)

# группируем по полю "category"
groups = {}
for item in data:
    cat = item.get("category") or "без категории"
    groups.setdefault(cat, []).append(item)

os.makedirs(outdir, exist_ok=True)

for cat, items in sorted(groups.items()):
    # делаем безопасное имя файла
    name = re.sub(r"^поиск:\s*", "", cat)          # убрать префикс "поиск:"
    name = re.sub(r'[\\/:*?"<>|«»]+', " ", name)   # убрать запрещённые символы
    name = re.sub(r"\s+", " ", name).strip()[:120] or "без категории"

    path = os.path.join(outdir, name + ".json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(items, f, ensure_ascii=False, indent=2)
    print(f"{len(items):4d}  {path}")

print(f"\nГотово: {len(groups)} файлов в папке {outdir}/, всего товаров {len(data)}.")
