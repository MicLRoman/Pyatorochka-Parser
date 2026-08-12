# -*- coding: utf-8 -*-
# Ищет реальные магазины Пятёрочки по координатам и показывает,
# насколько полный у каждого каталог (сколько групп/подкатегорий).
# Ничего не меняет — только печатает.
#
# Запуск (окно Chrome с отладкой открыто, проверка 5ka пройдена):
#     py.exe find_stores.py                      # Ростов-на-Дону
#     py.exe find_stores.py 55.7558 37.6173      # свои lat lon

import asyncio
import sys

from human_requests.abstraction import HttpMethod

from parser5ka import RobustPyaterochkaAPI, flatten_categories

LAT = float(sys.argv[1]) if len(sys.argv) > 2 else 47.2224566   # Ростов-на-Дону
LON = float(sys.argv[2]) if len(sys.argv) > 2 else 39.718803
CHECK_FIRST = 5          # у скольких магазинов проверить каталог


def find_stores(obj):
    """Вытаскивает магазин(ы) из ответа (может быть один объект или список)."""
    if isinstance(obj, dict):
        if any(k in obj for k in ("sapCode", "sap_code", "storeId")):
            return [obj]
        for key in ("stores", "items", "results", "data"):
            v = obj.get(key)
            r = find_stores(v) if v is not None else None
            if r:
                return r
        for v in obj.values():
            r = find_stores(v)
            if r:
                return r
    elif isinstance(obj, list):
        out = []
        for x in obj:
            out += find_stores(x)
        return out
    return []


def sap_of(store):
    for k in ("sapCode", "sap_code", "storeId", "store_id", "id"):
        if store.get(k):
            return str(store[k])
    return None


def addr_of(store):
    for k in ("shop_address", "address", "addressFull", "fullAddress", "name", "title"):
        v = store.get(k)
        if isinstance(v, str) and v:
            return v
        if isinstance(v, dict):
            for kk in ("full", "name", "text"):
                if isinstance(v.get(kk), str):
                    return v[kk]
    return "(адрес не указан)"


async def main():
    api = RobustPyaterochkaAPI(headless=False, timeout_ms=30000, proxy=None)
    api.cdp_port = 9222
    await api.__aenter__()
    try:
        url = f"{api.CATALOG_URL}/orders/v1/orders/stores/?lat={LAT}&lon={LON}"
        print(f"\nИщу магазины: {url}")
        resp = await api._request(method=HttpMethod.GET, url=url)
        data = resp.json()

        import json
        print("Сырой ответ:", json.dumps(data, ensure_ascii=False))

        stores = find_stores(data)
        print(f"Найдено магазинов: {len(stores)}")
        if not stores:
            import json
            with open("sample_stores.json", "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
            print("Структуру ответа сохранил в sample_stores.json — гляну её.")
            return

        for i, st in enumerate(stores[:CHECK_FIRST], 1):
            sap = sap_of(st)
            print(f"\n[{i}] sapCode={sap}  {addr_of(st)}")
            if not sap:
                continue
            try:
                tree = (await api.Catalog.tree(sap_code_store_id=sap,
                                               subcategories=True)).json()
                leaves = flatten_categories(tree)
                tops = sorted({l["name"].split(" / ")[0] for l in leaves})
                print(f"     листьев: {len(leaves)}, групп: {len(tops)}")
                print("     группы:", ", ".join(tops))
            except Exception as e:
                print(f"     каталог не получен ({e})")
    finally:
        await api.__aexit__(None, None, None)


if __name__ == "__main__":
    asyncio.run(main())
