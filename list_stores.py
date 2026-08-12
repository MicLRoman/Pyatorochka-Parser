# -*- coding: utf-8 -*-
# Список магазинов Пятёрочки в районе: сканирует сетку точек вокруг центра
# и собирает все уникальные магазины (sapCode + адрес + доставка).
# Ничего не меняет, только печатает.
#
# Запуск (окно Chrome с отладкой открыто, проверка 5ka пройдена):
#     py.exe list_stores.py                       # центр — Ростов-на-Дону
#     py.exe list_stores.py 47.2261 39.7580       # свой центр lat lon
#     py.exe list_stores.py 47.2261 39.7580 3 0.01  # + радиус(шагов) и шаг(градусы)

import asyncio
import sys

from human_requests.abstraction import HttpMethod

from parser5ka import RobustPyaterochkaAPI

LAT = float(sys.argv[1]) if len(sys.argv) > 2 else 47.226126
LON = float(sys.argv[2]) if len(sys.argv) > 2 else 39.757970
STEPS = int(sys.argv[3]) if len(sys.argv) > 3 else 4       # сетка (2*STEPS+1)^2 точек
STEP = float(sys.argv[4]) if len(sys.argv) > 4 else 0.008  # ~0.9 км широта / ~0.6 км долгота


async def main():
    api = RobustPyaterochkaAPI(headless=False, timeout_ms=30000, proxy=None)
    api.cdp_port = 9222
    await api.__aenter__()
    try:
        points = [(LAT + i * STEP, LON + j * STEP)
                  for i in range(-STEPS, STEPS + 1)
                  for j in range(-STEPS, STEPS + 1)]
        print(f"Сканирую {len(points)} точек вокруг ({LAT}, {LON})...")

        found = {}   # sap_code -> address
        for n, (lat, lon) in enumerate(points, 1):
            url = f"{api.CATALOG_URL}/orders/v1/orders/stores/?lat={lat}&lon={lon}"
            try:
                data = (await api._request(method=HttpMethod.GET, url=url)).json()
            except Exception:
                continue
            sap = data.get("sap_code")
            if sap and sap not in found:
                found[sap] = data.get("shop_address") or "(адрес не указан)"
                print(f"  +{len(found):2d}  {sap}  {found[sap]}")
            await asyncio.sleep(0.25)

        print(f"\nВсего уникальных магазинов: {len(found)}")
        for sap, addr in sorted(found.items(), key=lambda x: x[1]):
            print(f"  {sap}\t{addr}")
        print("\nВыбери нужный sapCode и запусти сбор:")
        print("  py.exe parser5ka.py --cdp 9222 --food-only --sap-code <SAP> "
              "--out products_rostov.json --max-products 200")
    finally:
        await api.__aexit__(None, None, None)


if __name__ == "__main__":
    asyncio.run(main())
