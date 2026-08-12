# -*- coding: utf-8 -*-
# Диагностика: сравнивает дерево категорий Пятёрочки в режимах store и delivery.
# Ничего не собирает и ничего не меняет — только печатает.
# Запуск (окно Chrome с отладкой должно быть открыто):
#     py.exe check_modes.py            (порт 9222)
#     py.exe check_modes.py 9222

import asyncio
import sys

from parser5ka import RobustPyaterochkaAPI, flatten_categories
from pyaterochka_api.enums import PurchaseMode


async def main():
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 9222

    api = RobustPyaterochkaAPI(headless=False, timeout_ms=30000, proxy=None)
    api.cdp_port = port
    await api.__aenter__()
    try:
        store = await api.delivery_panel_store()
        sap = store["selectedStore"]["sapCode"]
        print(f"\nМагазин (sapCode): {sap}")

        for mode in (PurchaseMode.STORE, PurchaseMode.DELIVERY):
            try:
                tree = (await api.Catalog.tree(sap_code_store_id=sap,
                                               subcategories=True, mode=mode)).json()
                leaves = flatten_categories(tree)
            except Exception as e:
                print(f"\n=== mode={mode.value}: ОШИБКА ({e})")
                continue

            tops = {}
            for l in leaves:
                top = l["name"].split(" / ")[0]
                tops.setdefault(top, 0)
                tops[top] += 1

            print(f"\n=== mode={mode.value}: листьев {len(leaves)}, групп {len(tops)} ===")
            for name in sorted(tops):
                print(f"  {tops[name]:3d}  {name}")
    finally:
        await api.__aexit__(None, None, None)


if __name__ == "__main__":
    asyncio.run(main())
