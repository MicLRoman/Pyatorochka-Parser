# -*- coding: utf-8 -*-
"""
Парсер Пятёрочки -> товары (название, описание, БЖУ) для скармливания нейросети.

Использует библиотеку pyaterochka_api (её методы API), но движок — настоящий Chrome
через Playwright. Сайт защищён антиботом ServicePipe, который ловит автоматику, поэтому
ОСНОВНОЙ режим — подключение к твоему Chrome, где проверку проходит человек (--cdp).

УСТАНОВКА (один раз):
    pip install pyaterochka_api      # тянет за собой playwright; Chrome берётся системный

КАК ПОЛЬЗОВАТЬСЯ (подробно — в файле КАК_ЗАПУСТИТЬ.md):
    # 1) открыть свой Chrome с отладкой, пройти проверку руками, окно не закрывать:
    python parser5ka.py --open-chrome

    # 2) разведка одного товара (где лежит БЖУ):
    python parser5ka.py --probe 3551126 --cdp 9222

    # 3) собрать товары:
    python parser5ka.py --cdp 9222 --categories 73C2261,73C2271 --max-products 100

Результат -> products.json (массив name/description/nutrition/...),
плюс sample_product.json (сырой JSON первого товара — для проверки полей БЖУ).
"""

import argparse
import asyncio
import json
import os
import re
import sys

try:
    from pyaterochka_api import PyaterochkaAPI
    from playwright.async_api import async_playwright
    from human_requests import HumanBrowser, HumanPage
    from human_requests.abstraction import Proxy
    from human_requests.network_analyzer.anomaly_sniffer import (
        HeaderAnomalySniffer, WaitHeader, WaitSource)
    from collections import defaultdict
except ImportError:
    print("Не установлена библиотека. Выполни:\n"
          "    pip install pyaterochka_api\n"
          "    python -m camoufox fetch")
    sys.exit(1)


class RobustPyaterochkaAPI(PyaterochkaAPI):
    """
    То же, что PyaterochkaAPI, но с устойчивым прогревом: не падает, если на сайте
    больше нет элемента next-route-announcer (он сменился после обновления сайта).
    Дополнительно сохраняет warmup.png и warmup.html для диагностики.
    """

    async def _launch_chrome(self):
        """Запускает НАСТОЯЩИЙ Chrome (или Edge / встроенный Chromium) через Playwright."""
        if getattr(self, "_pw", None) is None:
            self._pw = await async_playwright().start()
        proxy = None
        if self.proxy:
            try:
                proxy = Proxy(self.proxy).as_dict()
            except Exception:
                proxy = None

        args = ["--disable-blink-features=AutomationControlled"]
        if proxy:
            print(f"warmup: прокси для браузера: {self.proxy}")
        else:
            # ВАЖНО: Chromium сам подхватывает переменные окружения HTTP(S)_PROXY/ALL_PROXY,
            # даже если прокси не передавали. Глушим это, чтобы выходить с прямого IP.
            removed = [v for v in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy",
                                   "https_proxy", "ALL_PROXY", "all_proxy")
                       if os.environ.pop(v, None)]
            args.append("--no-proxy-server")
            print(f"warmup: прокси отключён принудительно. Вычищены env: {removed or 'нет'}")
        last_err = None
        for channel in ("chrome", "msedge", None):
            try:
                kwargs = dict(headless=self.headless, args=args)
                if channel:
                    kwargs["channel"] = channel
                if proxy:
                    kwargs["proxy"] = proxy
                br = await self._pw.chromium.launch(**kwargs)
                print(f"warmup: запущен браузер: {channel or 'встроенный chromium'}")
                return br
            except Exception as e:
                last_err = e
        raise RuntimeError(
            "Не удалось запустить Chrome/Edge/Chromium. Установи Google Chrome, "
            "либо выполни:  python -m playwright install chromium\n"
            f"Последняя ошибка: {last_err}")

    async def _warmup(self) -> None:
        self._pw = await async_playwright().start()
        cdp_port = getattr(self, "cdp_port", 0)

        if cdp_port:
            # ПЛАН В: подключаемся к УЖЕ ОТКРЫТОМУ твоему Chrome, где проверку прошёл человек.
            if not cdp_alive(cdp_port):
                raise RuntimeError(
                    f"На порту {cdp_port} нет браузера с отладкой.\n"
                    f"Сначала открой его:  py.exe parser5ka.py --open-chrome\n"
                    f"пройди в нём проверку 5ka руками и НЕ закрывай окно, потом повтори команду.")
            print(f"warmup: подключаюсь к Chrome по CDP на 127.0.0.1:{cdp_port} ...")
            browser = await self._pw.chromium.connect_over_cdp(f"http://127.0.0.1:{cdp_port}")
            self.session = HumanBrowser.replace(browser)
            ctxs = self.session.contexts
            if not ctxs:
                raise RuntimeError("В подключённом Chrome нет вкладок. Открой в нём вкладку с 5ka.ru.")
            self.ctx = ctxs[0]
            pages = self.ctx.pages
            self.page = HumanPage.replace(pages[0]) if pages else await self.ctx.new_page()
        else:
            br = await self._launch_chrome()
            self.session = HumanBrowser.replace(br)
            self.ctx = await self.session.new_context(locale="ru-RU")
            try:
                await self.ctx.add_init_script(
                    "Object.defineProperty(navigator,'webdriver',{get:()=>undefined});")
            except Exception:
                pass
            self.page = await self.ctx.new_page()

        sniffer = HeaderAnomalySniffer(
            extra_request_allow=["x-forwarded-for", "x-real-ip"],
            extra_response_allow=[],
            include_subresources=True,
            url_filter=lambda u: u.startswith("https://5d.5ka.ru/"),
        )
        await sniffer.start(self.ctx)

        # Маркеры страницы-проверки ServicePipe (заглушка / спиннер / капча «разверни картинку»).
        CHALLENGE = ("servicepipe", "id_spinner", "id_captcha_frame_div",
                     "sp_rotated_captcha", "captcha_root", "captcha-holder", "captchaintgen")

        try:
            await self.page.goto("https://5ka.ru", wait_until="domcontentloaded", timeout=self.timeout_ms)
        except Exception as e:
            print(f"warmup: страница не открылась ({e})")

        if cdp_port:
            # Проверку в этом Chrome проходит человек — мы её не ломимся обходить, только убеждаемся.
            try:
                await self.page.wait_for_load_state("networkidle", timeout=self.timeout_ms)
            except Exception:
                pass
            content = ""
            try:
                content = (await self.page.content()).lower()
            except Exception:
                pass
            if any(m in content for m in CHALLENGE):
                print("warmup: в подключённом Chrome всё ещё висит проверка ServicePipe.\n"
                      "        Пройди её РУКАМИ в том окне, дождись витрины 5ka и запусти скрипт снова.")
            else:
                print("warmup: подключился к твоему Chrome — проверка пройдена, витрина загружена.")
        else:
            passed = False
            for attempt in range(1, 11):
                try:
                    await self.page.wait_for_load_state("networkidle", timeout=15000)
                except Exception:
                    pass
                try:
                    content = (await self.page.content()).lower()
                except Exception:
                    content = ""
                if content and not any(m in content for m in CHALLENGE):
                    passed = True
                    print(f"warmup: антибот ServicePipe пройден (попытка {attempt}).")
                    break
                print(f"warmup: антибот ещё держит (попытка {attempt}/10) — жду и перезагружаю...")
                await asyncio.sleep(5)
                try:
                    await self.page.reload(wait_until="domcontentloaded", timeout=self.timeout_ms)
                except Exception:
                    pass
            if not passed:
                print("warmup: НЕ удалось пройти ServicePipe автоматически (Playwright палится).\n"
                      "        Используй режим --cdp (подключение к твоему Chrome). См. КАК_ЗАПУСТИТЬ.md.")

        # Необязательный чекпойнт настоящего сайта.
        try:
            await self.page.wait_for_selector("next-route-announcer", state="attached", timeout=8000)
        except Exception:
            pass

        # Диагностика: что реально на странице.
        try:
            await self.page.screenshot(path="warmup.png", full_page=True)
            html = await self.page.content()
            with open("warmup.html", "w", encoding="utf-8") as f:
                f.write(html)
            print("warmup: сохранил warmup.png и warmup.html")
        except Exception as e:
            print(f"warmup: не смог сохранить диагностику ({e})")

        # Ловим фирменные заголовки из запросов сайта к 5d.5ka.ru (это и есть цель прогрева).
        try:
            await sniffer.wait(
                tasks=[WaitHeader(source=WaitSource.REQUEST,
                                  headers=["x-app-version", "x-device-id", "x-platform"])],
                timeout_ms=self.timeout_ms,
            )
        except Exception as e:
            print(f"warmup: фирменные заголовки не пойманы ({e}). Запросы к API могут не пройти.")

        result_sniffer = await sniffer.complete()
        result = defaultdict(set)
        for _url, headers in result_sniffer["request"].items():
            for header, values in headers.items():
                result[header].update(values)
        self.unstandard_headers = {k: list(v) for k, v in result.items()}
        print(f"warmup: пойманные заголовки: {list(self.unstandard_headers.keys()) or '(пусто)'}")

    async def close(self):
        # Аккуратно: warmup мог упасть до создания self.session.
        try:
            session = getattr(self, "session", None)
            if session is not None:
                await session.close()
        except Exception:
            pass
        finally:
            pw = getattr(self, "_pw", None)
            if pw:
                try:
                    await pw.stop()
                except Exception:
                    pass


# --- Поиск БЖУ -------------------------------------------------------------
# Подписи, по которым ищем питательную ценность в любом месте JSON-карточки.
NUTRITION_PATTERNS = {
    "proteins":      re.compile(r"белк|protein", re.I),
    "fats":          re.compile(r"жир|fat", re.I),
    "carbohydrates": re.compile(r"углевод|carb", re.I),
    "calories":      re.compile(r"калор|ккал|kcal|энерг|energy", re.I),
}
DESCRIPTION_KEYS = re.compile(r"описан|description|состав|ingredient|примечан", re.I)


def _walk(obj, path=""):
    """Рекурсивно обходит весь JSON, возвращая пары (путь, ключ, значение)."""
    if isinstance(obj, dict):
        for k, v in obj.items():
            cur = f"{path}.{k}" if path else str(k)
            if isinstance(v, (dict, list)):
                yield from _walk(v, cur)
            else:
                yield cur, str(k), v
    elif isinstance(obj, list):
        for i, v in enumerate(obj):
            cur = f"{path}[{i}]"
            if isinstance(v, (dict, list)):
                yield from _walk(v, cur)
            else:
                yield cur, "", v


def extract_nutrition(product_json):
    """
    Пытается достать БЖУ двумя способами:
    1) пары ключ-значение, где ключ похож на 'белки'/'protein' и т.п.;
    2) списки характеристик вида {"name": "Белки", "value": "12 г"}.
    """
    found = {}

    # Способ 1: ключ сам по себе называется как нутриент
    for full_path, key, value in _walk(product_json):
        if value is None or value == "":
            continue
        for nutr, pat in NUTRITION_PATTERNS.items():
            if nutr not in found and pat.search(key):
                found[nutr] = str(value)

    # Способ 2: характеристики-объекты {name/title/label: ..., value/val: ...}
    def scan_charts(obj):
        if isinstance(obj, dict):
            name_field = next((obj[k] for k in ("name", "title", "label", "key")
                               if isinstance(obj.get(k), str)), None)
            val_field = next((obj[k] for k in ("value", "val", "text")
                              if obj.get(k) not in (None, "")), None)
            if name_field and val_field is not None:
                for nutr, pat in NUTRITION_PATTERNS.items():
                    if nutr not in found and pat.search(name_field):
                        found[nutr] = str(val_field)
            for v in obj.values():
                scan_charts(v)
        elif isinstance(obj, list):
            for v in obj:
                scan_charts(v)

    scan_charts(product_json)
    return found


def _strip_html(s):
    s = re.sub(r"<[^>]+>", " ", s)       # убрать html-теги
    s = re.sub(r"\s+", " ", s)           # схлопнуть пробелы/переносы
    return s.strip()


def extract_description(product_json):
    """Собирает текстовые поля, похожие на описание/состав (без html и дублей)."""
    raw_parts = []
    for full_path, key, value in _walk(product_json):
        if isinstance(value, str) and len(value) > 15 and DESCRIPTION_KEYS.search(key):
            text = _strip_html(value)
            if text:
                raw_parts.append(text)
    # убираем дубли и куски, которые уже входят в более длинный кусок
    parts = []
    for t in sorted(set(raw_parts), key=len, reverse=True):
        if not any(t in p for p in parts):
            parts.append(t)
    return " | ".join(parts)


def flatten_categories(tree):
    """Разворачивает дерево категорий в плоский список листьев (id, name)."""
    leaves = []

    def rec(nodes, parent_name=""):
        for node in nodes:
            name = node.get("name", "")
            children = node.get("categories") or []
            full_name = f"{parent_name} / {name}".strip(" /")
            if children:
                rec(children, full_name)
            else:
                leaves.append({"id": node.get("id"), "name": full_name})

    rec(tree)
    return leaves


# --- Основные сценарии ------------------------------------------------------
def find_browser():
    """Ищет Chromium-браузер (Chrome / Edge / Yandex) в стандартных местах."""
    la = os.environ.get("LOCALAPPDATA", "")
    candidates = [
        r"C:\Program Files\Google\Chrome\Application\chrome.exe",
        r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
        os.path.join(la, r"Google\Chrome\Application\chrome.exe"),
        r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
        r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
        os.path.join(la, r"Yandex\YandexBrowser\Application\browser.exe"),
    ]
    return next((c for c in candidates if os.path.exists(c)), None)


def cdp_alive(port):
    """Проверяет, слушает ли на порту Chrome с отладкой."""
    import urllib.request
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/json/version", timeout=2) as r:
            return r.status == 200
    except Exception:
        return False


def open_chrome(port):
    """Запускает твой браузер с включённой отладкой (CDP) в отдельном профиле."""
    import subprocess
    import time
    if cdp_alive(port):
        print(f"На порту {port} уже есть браузер с отладкой — можно запускать парсер с --cdp {port}.")
        return
    browser = find_browser()
    if not browser:
        print("Не нашёл Chrome/Edge/Yandex. Запусти браузер вручную (закрыв обычные окна Chrome):\n"
              f'  & "C:\\путь\\к\\chrome.exe" --remote-debugging-port={port} '
              f'--user-data-dir="%USERPROFILE%\\chrome-5ka" https://5ka.ru')
        return
    udd = os.path.join(os.path.expanduser("~"), "chrome-5ka")
    # Глушим прокси/VPN так же, как в режиме парсинга: Chromium сам подхватывает
    # HTTP(S)_PROXY/ALL_PROXY и системный прокси, из-за чего сайт может счесть IP «VPN».
    removed = [v for v in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy",
                           "https_proxy", "ALL_PROXY", "all_proxy")
               if os.environ.pop(v, None)]
    print(f"Запускаю: {browser}\n  профиль: {udd}, порт отладки: {port}")
    print(f"Прокси отключён принудительно (--no-proxy-server). Вычищены env: {removed or 'нет'}")
    subprocess.Popen([browser, f"--remote-debugging-port={port}",
                      f"--user-data-dir={udd}", "--no-proxy-server",
                      "--proxy-server=direct://", "--proxy-bypass-list=*",
                      "https://5ka.ru"])

    for _ in range(15):
        time.sleep(1)
        if cdp_alive(port):
            print(f"\nОК: браузер поднялся, отладка на порту {port} доступна.")
            break
    else:
        print(f"\nВНИМАНИЕ: порт {port} так и не открылся. Возможные причины:\n"
              "  - уже был запущен этот же браузер: закрой ВСЕ его окна и повтори;\n"
              "  - браузер не Chromium. Тогда запусти Chrome/Edge вручную с флагами выше.")
        return

    print("\nТеперь в ОТКРЫВШЕМСЯ окне браузера:")
    print("  1) пройди проверку ServicePipe (капчу) руками;")
    print("  2) дождись нормальной витрины 5ka;")
    print("  3) НЕ закрывай это окно.")
    print(f"\nПосле этого:\n  py.exe parser5ka.py --probe 3551126 --cdp {port}")


# Несъедобное, что цепляется поиском по словам (корм, косметика, семена и т.п.).
NON_FOOD = re.compile(
    r"корм|для кошек|для собак|для котят|для животных|гель для душа|шампунь|"
    r"крем-гель|\bмыло\b|семена|кинетическ|наполнитель для|подгузник|салфетк|"
    r"зубн|стиральн|для посева|игрушк",
    re.I)


def cmd_clean(args):
    """Чистит уже собранный products.json: убирает несъедобное и записи без БЖУ."""
    src = args.out
    if not os.path.exists(src):
        print(f"Нет файла {src} — сначала собери товары.")
        return
    with open(src, encoding="utf-8") as f:
        data = json.load(f)
    kept = [r for r in data
            if r.get("nutrition") and not NON_FOOD.search(r.get("name", ""))]
    out = "products_clean.json"
    with open(out, "w", encoding="utf-8") as f:
        json.dump(kept, f, ensure_ascii=False, indent=2)
    print(f"Очистка: было {len(data)}, осталось съедобных с БЖУ — {len(kept)}.")
    print(f"Чистый файл для нейросети -> {out}")


def _safe_filename(s):
    s = re.sub(r"^поиск:\s*", "", s)               # убрать префикс поиска
    s = re.sub(r'[\\/:*?"<>|«»]+', " ", s)         # выкинуть недопустимые символы
    s = re.sub(r"\s+", " ", s).strip()
    return (s or "без категории")[:120]


def cmd_split(args):
    """Разбивает products.json на файлы по категориям в папке by_category/."""
    src = args.out
    if not os.path.exists(src):
        print(f"Нет файла {src} — сначала собери товары.")
        return
    with open(src, encoding="utf-8") as f:
        data = json.load(f)

    groups = {}
    for r in data:
        groups.setdefault(r.get("category") or "без категории", []).append(r)

    outdir = "by_category"
    os.makedirs(outdir, exist_ok=True)
    for cat, items in sorted(groups.items()):
        path = os.path.join(outdir, _safe_filename(cat) + ".json")
        with open(path, "w", encoding="utf-8") as f:
            json.dump(items, f, ensure_ascii=False, indent=2)
        print(f"  {len(items):4d}  {path}")
    print(f"\nГотово: {len(groups)} файлов в папке {outdir}/ (всего товаров: {len(data)}).")


async def check_egress_ip(headless, proxy):
    """Показывает, с какого IP реально выходит наш Chrome (диагностика прокси/VPN)."""
    async with async_playwright() as pw:
        launch_proxy = None
        args = ["--disable-blink-features=AutomationControlled"]
        if proxy:
            launch_proxy = {"server": proxy}
            print(f"checkip: использую прокси {proxy}")
        else:
            removed = [v for v in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy",
                                   "https_proxy", "ALL_PROXY", "all_proxy")
                       if os.environ.pop(v, None)]
            args.append("--no-proxy-server")
            print(f"checkip: прокси отключён. Вычищены env: {removed or 'нет'}")

        br = None
        for channel in ("chrome", "msedge", None):
            try:
                kw = dict(headless=headless, args=args)
                if channel:
                    kw["channel"] = channel
                if launch_proxy:
                    kw["proxy"] = launch_proxy
                br = await pw.chromium.launch(**kw)
                print(f"checkip: браузер: {channel or 'встроенный chromium'}")
                break
            except Exception as e:
                print(f"checkip: {channel} не запустился ({e})")
        if not br:
            print("checkip: не смог запустить браузер."); return

        page = await (await br.new_context()).new_page()
        for url in ("https://api.ipify.org?format=json", "https://ifconfig.me/ip"):
            try:
                await page.goto(url, wait_until="domcontentloaded", timeout=20000)
                body = await page.content()
                print(f"checkip: {url}\n   -> {body[:300]}")
                break
            except Exception as e:
                print(f"checkip: {url} — ошибка ({e})")
        await br.close()


def purchase_mode(name):
    """'store' — ассортимент в зале, 'delivery' — каталог доставки (шире)."""
    from pyaterochka_api.enums import PurchaseMode
    return PurchaseMode.DELIVERY if str(name).lower() == "delivery" else PurchaseMode.STORE


async def get_store_and_tree(api, mode_name="store", sap_override=None):
    if sap_override:
        sap_code = sap_override
    else:
        store_info = await api.delivery_panel_store()
        sap_code = store_info["selectedStore"]["sapCode"]
    mode = purchase_mode(mode_name)
    # subcategories=True — тянем самое полное дерево (со всеми вложенными уровнями).
    tree = (await api.Catalog.tree(sap_code_store_id=sap_code, subcategories=True,
                                   mode=mode)).json()
    return sap_code, tree


# Категории Пятёрочки, которые не нужны для плана питания:
# несъедобное + временные/акционные (там дубли и товары, которых уже нет на полке).
NON_FOOD_CATS = re.compile(
    r"выручает|скидки недели|летний сезон|"
    r"кешбэк|кэшбэк|призумие|ножи за|комбо звёзд|комбо звезд|больше.{0,3}дешевле|"
    r"вот это находки|марки .?пятёрочки|марки .?пятерочки|детский клуб|пушистый клуб|"
    r"охладиться|вкусы пикника|время шашлыка|ритуалы красоты|блеск и порядок|"
    r"ароматы свежести|лет доставки|закажите с выгодой|гурмэ|"
    r"красота|гигиен|аптек|уход за|бритьё|бритье|депиляц|полост|бумага и салфетк|"
    r"стирк|уборк|чистящ|мытья посуды|ароматы для дома|предметы для уборки|"
    r"для дома и дачи|хозтовар|декор|одежда|обувь|текстиль|кухня|дача и отдых|"
    r"для животных|для кошек|для собак|питомц|"
    r"игры и игрушк|школа и хобби|детская гигиена|бытовая химия|"
    r"пиво, вино|алкогол|энергетик|табак|сигарет",
    re.I)


def filter_food(leaves, enabled=True):
    """Выкидывает несъедобные категории (бытовое, косметика, животные, алкоголь)."""
    if not enabled:
        return leaves
    kept = [l for l in leaves if not NON_FOOD_CATS.search(l["name"])]
    print(f"Фильтр «только еда»: {len(leaves)} -> {len(kept)} категорий")
    return kept


async def cmd_list_categories(api, args=None):
    sap_code, tree = await get_store_and_tree(api, getattr(args, "mode", "store"),
                                              getattr(args, "sap_code", None))
    print(f"Магазин (sapCode): {sap_code}\n")
    leaves = flatten_categories(tree)
    leaves = filter_food(leaves, getattr(args, "food_only", False))
    for leaf in leaves:
        print(f"{leaf['id']}\t{leaf['name']}")


async def cmd_probe(api, plu):
    sap_code, _ = await get_store_and_tree(api)
    resp = await api.Catalog.Product.info(sap_code_store_id=sap_code, plu_id=plu)
    data = resp.json()
    with open("sample_product.json", "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    print("Сырой JSON товара сохранён в sample_product.json")
    print("\nНайденное БЖУ:", json.dumps(extract_nutrition(data), ensure_ascii=False))
    print("Найденное описание:", extract_description(data)[:300] or "(не найдено)")


async def _retry(call, label, tries=4, base=3.0):
    """Повтор с нарастающей паузой — против троттлинга антибота ServicePipe."""
    last = None
    for i in range(tries):
        try:
            return await call()
        except Exception as e:
            last = e
            if i < tries - 1:
                wait = base * (i + 1)
                print(f"      ! {label}: попытка {i + 1}/{tries} ({e}). Жду {wait:.0f}с...")
                await asyncio.sleep(wait)
    raise last


def _extract_products(j):
    """Достаёт список товаров из ответа products_list/search (форматы немного разные)."""
    if isinstance(j, list):
        return j
    if isinstance(j, dict):
        p = j.get("products")
        if isinstance(p, list):
            return p
        if isinstance(p, dict) and isinstance(p.get("items"), list):
            return p["items"]
        # глубокий фолбэк: первый список объектов, у которых есть "plu"
        def find(o):
            if isinstance(o, list):
                if o and isinstance(o[0], dict) and "plu" in o[0]:
                    return o
                for x in o:
                    r = find(x)
                    if r:
                        return r
            elif isinstance(o, dict):
                for v in o.values():
                    r = find(v)
                    if r:
                        return r
            return None
        return find(j) or []
    return []


async def _collect_groups(api, sap_code, groups, args):
    """
    Общий сборщик. groups = список (label, fetch_factory),
    где fetch_factory() -> корутина, возвращающая ответ со списком товаров.
    Качает карточки, тянет БЖУ, сохраняет по ходу, поддерживает докачку и ретраи.
    """
    # Подхватываем уже собранное из прошлых запусков (докачка по частям).
    prev = {}
    if os.path.exists(args.out):
        try:
            with open(args.out, encoding="utf-8") as f:
                for r in json.load(f):
                    if r.get("plu") is not None:
                        prev[r["plu"]] = r
            if prev:
                print(f"Подхватил из {args.out}: {len(prev)} товаров (готовые пропущу).")
        except Exception:
            pass

    results, seen = [], set()
    saved_sample = False
    consec_fail = 0  # подряд упавших карточек — признак, что антибот снова душит

    def dump():
        with open(args.out, "w", encoding="utf-8") as f:
            json.dump(results, f, ensure_ascii=False, indent=2)

    aborted = False
    for gi, (label, factory) in enumerate(groups, 1):
        if aborted:
            break
        try:
            j = (await _retry(factory, f"список {label}")).json()
        except Exception as e:
            print(f"  [{gi}/{len(groups)}] {label}: список не получен ({e}) — пропускаю")
            continue

        products = _extract_products(j)[: args.max_products]
        print(f"  [{gi}/{len(groups)}] {label}: {len(products)} товаров")

        for p in products:
            plu = p.get("plu")
            if plu is None or plu in seen:
                continue
            seen.add(plu)
            old = prev.get(plu)
            if old and old.get("nutrition"):   # уже собран с БЖУ — не дёргаем API
                results.append(old)
                dump()
                continue
            item = {
                "plu": plu,
                "name": p.get("name"),
                "category": label,
                "weight": p.get("property_clarification") or p.get("uom"),
                "price": (p.get("prices") or {}).get("regular"),
                "description": "",
                "nutrition": {},
            }
            try:
                info = (await _retry(
                    lambda plu=plu: api.Catalog.Product.info(
                        sap_code_store_id=sap_code, plu_id=plu),
                    f"карточка {plu}")).json()
                if not saved_sample:
                    with open("sample_product.json", "w", encoding="utf-8") as f:
                        json.dump(info, f, ensure_ascii=False, indent=2)
                    saved_sample = True
                item["description"] = extract_description(info)
                item["nutrition"] = extract_nutrition(info)
                item["name"] = info.get("name") or item["name"]
                consec_fail = 0
            except Exception as e:
                item["error"] = str(e)
                consec_fail += 1
            results.append(item)
            dump()  # сохраняем по ходу — при обрыве собранное не потеряется
            if consec_fail >= 6:
                print("\nАнтибот снова требует проверку (много ошибок подряд).\n"
                      "Открой окно Chrome, обнови 5ka.ru, пройди капчу — и запусти ту же команду снова.\n"
                      f"Уже собранное сохранено в {args.out}")
                aborted = True
                break
            await asyncio.sleep(args.delay)

        await asyncio.sleep(args.cat_delay)

    dump()
    with_bju = sum(1 for r in results if r["nutrition"])
    print(f"\nГотово{' (прервано)' if aborted else ''}. Товаров: {len(results)}, "
          f"из них с БЖУ: {with_bju}.")
    print(f"Результат -> {args.out}")


async def cmd_crawl(api, args):
    sap_code, tree = await get_store_and_tree(api, getattr(args, "mode", "store"),
                                              getattr(args, "sap_code", None))
    leaves = flatten_categories(tree)
    if args.categories:
        wanted = {c.strip() for c in args.categories.split(",")}
        leaves = [l for l in leaves if str(l["id"]) in wanted]
    else:
        leaves = filter_food(leaves, getattr(args, "food_only", False))
    if args.max_categories:
        leaves = leaves[: args.max_categories]

    print(f"Магазин: {sap_code}. Категорий к обходу: {len(leaves)}")
    groups = [
        (l["name"], (lambda l=l: api.Catalog.products_list(
            category_id=l["id"], sap_code_store_id=sap_code, limit=args.max_products)))
        for l in leaves
    ]
    await _collect_groups(api, sap_code, groups, args)


# Базовые продукты для готовки — ищем их по названию, не завися от категорий магазина.
STAPLES = [
    "молоко", "кефир", "творог", "сметана", "сыр", "йогурт", "яйца",
    "масло сливочное", "масло подсолнечное", "курица", "филе куриное",
    "говядина", "свинина", "фарш", "индейка", "сосиски", "колбаса", "рыба",
    "гречка", "рис", "макароны", "овсянка", "пшено", "мука", "сахар",
    "картофель", "лук", "морковь", "капуста", "помидор", "огурец",
    "яблоки", "банан", "хлеб", "чай", "кофе", "фасоль", "чечевица",
]


async def cmd_search(api, args):
    sap_code, _ = await get_store_and_tree(api)
    raw = args.search
    queries = STAPLES if raw in (None, "", "__default__") else \
        [q.strip() for q in raw.split(",") if q.strip()]
    print(f"Магазин: {sap_code}. Поисковых запросов: {len(queries)}")
    groups = [
        (f"поиск: {q}", (lambda q=q: api.Catalog.search(
            sap_code_store_id=sap_code, query=q, limit=args.max_products)))
        for q in queries
    ]
    await _collect_groups(api, sap_code, groups, args)


async def main():
    ap = argparse.ArgumentParser(description="Парсер товаров Пятёрочки (name/description/БЖУ).")
    ap.add_argument("--list-categories", action="store_true", help="показать категории и выйти")
    ap.add_argument("--probe", metavar="PLU", help="разведка одного товара по его PLU")
    ap.add_argument("--categories", help="id категорий через запятую (по умолчанию все)")
    ap.add_argument("--search", nargs="?", const="__default__", default=None,
                    help="собирать товары ПОИСКОМ по словам (через запятую). Без значения — "
                         "набор базовых продуктов для готовки. Обходит ограничение категорий магазина.")
    ap.add_argument("--max-categories", type=int, default=0, help="ограничить число категорий")
    ap.add_argument("--food-only", action="store_true",
                    help="только съедобные категории (убрать косметику, бытовое, "
                         "животных, алкоголь, игрушки)")
    ap.add_argument("--sap-code", default=None,
                    help="жёстко задать магазин по sapCode (напр. L887), минуя дефолтный 35XY")
    ap.add_argument("--max-products", type=int, default=40, help="товаров с одной категории")
    ap.add_argument("--delay", type=float, default=0.8, help="пауза между товарами, сек")
    ap.add_argument("--cat-delay", type=float, default=3.0, help="пауза между категориями, сек")
    ap.add_argument("--out", default="products.json", help="файл результата")
    ap.add_argument("--headless", action="store_true",
                    help="скрыть окно браузера (быстрее, но антибот может блокировать)")
    ap.add_argument("--timeout", type=float, default=30000,
                    help="таймаут загрузки/запросов в миллисекундах (по умолчанию 30000)")
    ap.add_argument("--proxy", default=None,
                    help="прокси (напр. http://user:pass@host:port). По умолчанию БЕЗ прокси, "
                         "даже если в системе задан HTTPS_PROXY")
    ap.add_argument("--checkip", action="store_true",
                    help="диагностика: показать, с какого IP выходит наш Chrome, и выйти")
    ap.add_argument("--cdp", type=int, default=0, metavar="PORT",
                    help="подключиться к твоему Chrome по этому порту отладки (напр. 9222) "
                         "вместо запуска своего браузера. Так обходится ServicePipe.")
    ap.add_argument("--open-chrome", action="store_true",
                    help="открыть твой Chrome с отладкой (для режима --cdp) и выйти")
    ap.add_argument("--clean", action="store_true",
                    help="почистить готовый products.json от несъедобного -> products_clean.json")
    ap.add_argument("--split", action="store_true",
                    help="разбить products.json по категориям в папку by_category/ и выйти")
    args = ap.parse_args()

    if args.clean:
        cmd_clean(args)
        return

    if args.split:
        cmd_split(args)
        return

    if args.open_chrome:
        open_chrome(args.cdp or 9222)
        return

    if args.checkip:
        await check_egress_ip(args.headless, args.proxy)
        return

    api = RobustPyaterochkaAPI(headless=args.headless, timeout_ms=args.timeout, proxy=args.proxy)
    api.cdp_port = args.cdp
    try:
        try:
            await api.__aenter__()
        except RuntimeError as e:
            print(f"\nОшибка: {e}")
            return
        if args.list_categories:
            await cmd_list_categories(api, args)
        elif args.probe:
            await cmd_probe(api, args.probe)
        elif args.search is not None:
            await cmd_search(api, args)
        else:
            await cmd_crawl(api, args)
    finally:
        await api.__aexit__(None, None, None)


if __name__ == "__main__":
    asyncio.run(main())
