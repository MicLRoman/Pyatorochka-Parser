# -*- coding: utf-8 -*-
# Разбивает products.json на отдельные файлы по ГЛОБАЛЬНЫМ категориям.
# Запуск:  py.exe split_global.py            (берёт products.json)
#          py.exe split_global.py имя.json   (берёт другой файл)

import json
import os
import re
import sys

src = sys.argv[1] if len(sys.argv) > 1 else "products.json"
outdir = "by_global_category"

with open(src, encoding="utf-8") as f:
    data = json.load(f)

# группируем по глобальной категории
groups = {}
for item in data:
    cat = item.get("category") or "без категории"
    
    # Пытаемся извлечь глобальную категорию
    # Разделителем в Чижике является " / "
    parts = [p.strip() for p in re.split(r'\s*/\s*', cat)]
    
    if len(parts) > 1 and parts[0].lower() == "основной каталог":
        # У Чижика структура "Основной каталог / Глобальная / Подкатегория"
        global_cat = parts[1]
    elif len(parts) > 0:
        # Для других форматов берём первую часть иерархии
        global_cat = parts[0]
    else:
        global_cat = cat

    # Убираем префикс поиска, если есть (от Пятёрочки)
    global_cat = re.sub(r"^поиск:\s*", "", global_cat)
    if not global_cat:
        global_cat = "без категории"
        
    groups.setdefault(global_cat, []).append(item)

os.makedirs(outdir, exist_ok=True)

for cat, items in sorted(groups.items()):
    # делаем безопасное имя файла
    name = re.sub(r'[\\/:*?"<>|«»]+', " ", cat)   # убрать запрещённые символы
    name = re.sub(r"\s+", " ", name).strip()[:120] or "без категории"

    path = os.path.join(outdir, name + ".json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(items, f, ensure_ascii=False, indent=2)
    print(f"{len(items):4d}  {path}")

print(f"\nГотово: {len(groups)} файлов (глобальных категорий) в папке {outdir}/, всего товаров {len(data)}.")
