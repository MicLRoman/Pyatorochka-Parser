# -*- coding: utf-8 -*-
"""
Парсер Чижика (X5) -> товары (название, описание, БЖУ) для нейросети.

БЕЗ библиотеки chizhik_api (она сломана — тянет несуществующий ApiParent).
Дёргаем публичные эндпоинты app.chizhik.club напрямую через твой Chrome по CDP
(на чистом Playwright), выполняя fetch внутри страницы — так проходит антибот
и работают куки. Логиниться в аккаунт НЕ нужно (эндпоинты unauthorized).

КАК ПОЛЬЗОВАТЬСЯ:
    # 1) открыть Chrome с отладкой (тот же, что для 5ka):
    py.exe parser5ka.py --open-chrome
    #    в окне открой https://chizhik.club/catalog/ , дождись витрины (не логинься)

    # 2) разведка (категории + один товар -> sample_chizhik.json):
    py.exe parser_chizhik.py --probe --cdp 9222

    # 3) список категорий:
    py.exe parser_chizhik.py --list-categories --cdp 9222

    # 4) собрать товары:
    py.exe parser_chizhik.py --cdp 9222

Результат -> chizhik.json. Чистить/разбивать — теми же split.py и parser5ka.py --clean.
"""

import argparse
import asyncio
import json
import os
import re
import sys
import urllib.parse

try:
    from playwright.async_api import async_playwright
except ImportError:
    print("Нет playwright. Выполни:  pip install playwright")
    sys.exit(1)

API = "https://app.chizhik.club/api"
MAIN = "https://chizhik.club/catalog/"
CHALLENGE = ("servicepipe", "sp_rotated_captcha", "captcha_root", "id_spinner", "проверк")


# --- Поиск БЖУ / описания -------------------------------------------------
NUTRITION_PATTERNS = {
    "proteins":      re.compile(r"белк|protein", re.I),
    "fats":          re.compile(r"жир|fat", re.I),
    "carbohydrates": re.compile(r"углевод|carb", re.I),
    "calories":      re.compile(r"калор|ккал|kcal|энерг|energy", re.I),
}
DESCRIPTION_KEYS = re.compile(r"описан|description|состав|ingredient|примечан", re.I)


def _walk(obj, path=""):
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
    found = {}
    for _p, key, value in _walk(product_json):
        if value in (None, ""):
            continue
        for nutr, pat in NUTRITION_PATTERNS.items():
            if nutr not in found and pat.search(key):
                found[nutr] = str(value)

    def scan(obj):
        if isinstance(obj, dict):
            name = next((obj[k] for k in ("name", "title", "label", "key")
                         if isinstance(obj.get(k), str)), None)
            val = next((obj[k] for k in ("value", "val", "text")
                        if obj.get(k) not in (None, "")), None)
            if name and val is not None:
                for nutr, pat in NUTRITION_PATTERNS.items():
                    if nutr not in found and pat.search(name):
                        found[nutr] = str(val)
            for v in obj.values():
                scan(v)
        elif isinstance(obj, list):
            for v in obj:
                scan(v)
    scan(product_json)
    return found


def _strip_html(s):
    s = re.sub(r"<[^>]+>", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def extract_description(product_json):
    raw = []
    for _p, key, value in _walk(product_json):
        if isinstance(value, str) and len(value) > 15 and DESCRIPTION_KEYS.search(key):
            t = _strip_html(value)
            if t:
                raw.append(t)
    parts = []
    for t in sorted(set(raw), key=len, reverse=True):
        if not any(t in p for p in parts):
            parts.append(t)
    return " | ".join(parts)


def chizhik_fields(info):
    """Достаёт поля из карточки Чижика (title + meta_data по кодам)."""
    meta = info.get("meta_data") or []
    nutrition, desc = {}, []
    weight = None
    for m in meta:
        code = (m.get("code") or "").lower()
        val = m.get("value")
        if val in (None, ""):
            continue
        if code == "proteins":
            nutrition["proteins"] = str(val)
        elif code == "fats":
            nutrition["fats"] = str(val)
        elif code in ("carboh", "carbohydrates"):
            nutrition["carbohydrates"] = str(val)
        elif code in ("energy_value", "energy", "kkal", "calories"):
            nutrition["calories"] = str(val)
        elif code in ("composition", "about_product"):
            desc.append(str(val))
        elif code == "product_net_weight":
            weight = f"{val} г"
    # фолбэк на общий поиск, если по кодам не нашли
    if not nutrition:
        nutrition = extract_nutrition(info)
    if not desc:
        d = extract_description(info)
        if d:
            desc = [d]
    return {
        "name": info.get("title") or info.get("name"),
        "article": info.get("plu"),
        "price": info.get("price"),
        "old_price": info.get("old_price"),
        "weight": weight or info.get("base_unit"),
        "description": " | ".join(dict.fromkeys(desc)),
        "nutrition": nutrition,
    }


def _find_id_list(j):
    """Первый список объектов с 'id' (категории/города/товары)."""
    if isinstance(j, list) and j and isinstance(j[0], dict) and "id" in j[0]:
        return j
    if isinstance(j, dict):
        for key in ("items", "results", "categories", "children", "products", "data"):
            v = j.get(key)
            if isinstance(v, list) and v and isinstance(v[0], dict) and "id" in v[0]:
                return v
        for v in j.values():
            r = _find_id_list(v)
            if r:
                return r
    return []


# Категории, которые не нужны для плана питания (и временная «НАДО УСПЕТЬ»).
NON_FOOD_CATS = re.compile(
    r"надо успеть|косметик|чемпионат|товары для дома|гигиен|стирк|уборк|"
    r"для животных|одежда|обувь|текстиль|алкоголь|энергетик|аптек|"
    r"детские товары|посуд|канцеляр|бытов|сад|авто|табак|сигарет",
    re.I)


def flatten_categories(tree, parent=""):
    leaves = []
    nodes = tree if isinstance(tree, list) else _find_id_list(tree)
    for node in nodes:
        name = node.get("name", "")
        children = node.get("children") or node.get("categories") or []
        full = f"{parent} / {name}".strip(" /")
        if children:
            leaves += flatten_categories(children, full)
        else:
            leaves.append({"id": node.get("id"), "name": full})
    return leaves


def get_filtered_leaves(tree, args):
    leaves = flatten_categories(tree)
    if args.categories:
        wanted = {c.strip() for c in args.categories.split(",")}
        leaves = [l for l in leaves if str(l["id"]) in wanted]
    if getattr(args, "food_only", False):
        exclude_cats = [
            "надо успеть",
            "косметика",
            "детские средства гигиены",
            "детская одежда",
            "игры/игрушки/книги",
            "детские товары / прочее",
            "средства гигиены",
            "товары для дома",
            "стирка и уборка",
            "товары для животных",
            "одежда, обувь, текстиль",
            "футболу"
        ]
        leaves = [
            l for l in leaves
            if not any(exc in l["name"].lower() for exc in exclude_cats)
        ]
    return leaves


# --- CDP / доступ к API через страницу ------------------------------------
def cdp_alive(port):
    import urllib.request
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/json/version", timeout=2) as r:
            return r.status == 200
    except Exception:
        return False


_JS_FETCH = """
async (url) => {
  try {
    const r = await fetch(url, {
      headers: {'Accept': 'application/json, text/plain, */*'},
      credentials: 'include'
    });
    const t = await r.text();
    return {ok: true, status: r.status, body: t};
  } catch (e) { return {ok: false, error: String(e)}; }
}
"""


async def api_get(page, url):
    res = await page.evaluate(_JS_FETCH, url)
    if not res.get("ok"):
        raise RuntimeError(f"fetch error: {res.get('error')}")
    if res["status"] >= 400:
        raise RuntimeError(f"HTTP {res['status']} на {url} :: {res['body'][:200]}")
    return json.loads(res["body"])


async def _retry(coro_factory, label, tries=4, base=3.0):
    last = None
    for i in range(tries):
        try:
            return await coro_factory()
        except Exception as e:
            last = e
            if i < tries - 1:
                wait = base * (i + 1)
                print(f"      ! {label}: попытка {i + 1}/{tries} ({e}). Жду {wait:.0f}с...")
                await asyncio.sleep(wait)
    raise last


async def connect(port):
    pw = await async_playwright().start()
    if not cdp_alive(port):
        raise RuntimeError(f"На порту {port} нет Chrome с отладкой. "
                           f"Сначала:  py.exe parser5ka.py --open-chrome")
    print(f"Подключаюсь к Chrome по CDP на 127.0.0.1:{port} ...")
    browser = await pw.chromium.connect_over_cdp(f"http://127.0.0.1:{port}")
    ctxs = browser.contexts
    if not ctxs:
        raise RuntimeError("В подключённом Chrome нет вкладок.")
    ctx = ctxs[0]
    page = ctx.pages[0] if ctx.pages else await ctx.new_page()
    return pw, browser, page


async def warmup(page, timeout=30000):
    # 1) зайти на витрину (куки .chizhik.club, пройти антибот если надо)
    try:
        await page.goto(MAIN, wait_until="domcontentloaded", timeout=timeout)
    except Exception as e:
        print(f"warmup: витрина не открылась ({e})")
    try:
        content = (await page.content()).lower()
    except Exception:
        content = ""
    if any(m in content for m in CHALLENGE):
        print("warmup: на chizhik.club висит проверка — пройди её РУКАМИ в окне Chrome "
              "и запусти команду снова.")
    # 2) перейти на origin API, чтобы fetch был same-origin и с куками
    try:
        await page.goto(f"{API}/v1", wait_until="domcontentloaded", timeout=timeout)
        print("warmup: API-origin открыт.")
    except Exception as e:
        print(f"warmup: API не открылся ({e})")


# --- Эндпоинты ------------------------------------------------------------
async def get_city_id(page, name):
    # у городов Чижика id = fias_id (uuid), поле 'id' отсутствует
    queries = list(dict.fromkeys([name, name.split()[0], name.split("-")[0]]))
    for q in queries:
        try:
            j = await api_get(page, f"{API}/v1/geo/cities/?name={urllib.parse.quote(q)}&page=1")
        except Exception as e:
            print(f"Город: запрос '{q}' — ошибка ({e})")
            continue
        items = j.get("items") if isinstance(j, dict) else (j if isinstance(j, list) else [])
        if not items:
            continue
        # точное совпадение по имени, иначе первый с магазином, иначе просто первый
        exact = next((c for c in items if str(c.get("name", "")).lower() == name.lower()), None)
        pick = exact or next((c for c in items if c.get("has_shop")), None) or items[0]
        cid = pick.get("fias_id") or pick.get("slug") or pick.get("id")
        print(f"Город: {pick.get('name')} (city_id={cid})")
        return cid
    print(f"Город '{name}' не найден — работаю без city_id "
          "(ассортимент Чижика почти одинаков по городам).")
    return None


async def get_tree(page, city_id):
    url = f"{API}/v1/catalog/unauthorized/categories/"
    if city_id:
        url += f"?city_id={city_id}"
    return await api_get(page, url)


async def get_products(page, category_id, cpage, city_id):
    url = f"{API}/v1/catalog/unauthorized/products/?page={cpage}&category_id={category_id}"
    if city_id:
        url += f"&city_id={city_id}"
    return await api_get(page, url)


async def get_info(page, product_id, city_id):
    url = f"{API}/v1/catalog/unauthorized/products/{product_id}/"
    if city_id:
        url += f"?city_id={city_id}"
    return await api_get(page, url)


# --- Команды --------------------------------------------------------------
async def cmd_list_categories(page, city, args):
    tree = await get_tree(page, city)
    for leaf in get_filtered_leaves(tree, args):
        print(f"{leaf['id']}\t{leaf['name']}")


async def cmd_probe(page, city, args):
    tree = await get_tree(page, city)
    leaves = get_filtered_leaves(tree, args)
    print(f"Категорий (листьев): {len(leaves)}")
    if not leaves:
        with open("sample_chizhik.json", "w", encoding="utf-8") as f:
            json.dump(tree, f, ensure_ascii=False, indent=2)
        print("Сохранил дерево в sample_chizhik.json — глянем структуру.")
        return

    print(f"Беру первую категорию: {leaves[0]['name']}")
    plist = await get_products(page, leaves[0]["id"], 1, city)
    products = _find_id_list(plist)
    print(f"Товаров в категории: {len(products)}")
    if not products:
        with open("sample_chizhik.json", "w", encoding="utf-8") as f:
            json.dump(plist, f, ensure_ascii=False, indent=2)
        print("Сохранил ответ списка в sample_chizhik.json — глянем структуру.")
        return

    pid = products[0].get("id")
    info = await get_info(page, pid, city)
    with open("sample_chizhik.json", "w", encoding="utf-8") as f:
        json.dump(info, f, ensure_ascii=False, indent=2)
    fields = chizhik_fields(info)
    print(f"\nПервый товар (id={pid}): {fields['name']}  ({fields['price']}₽, {fields['weight']})")
    print("Найденное БЖУ:", json.dumps(fields["nutrition"], ensure_ascii=False))
    print("Описание/состав:", (fields["description"][:300] or "(не найдено)"))
    print("Сырой JSON карточки -> sample_chizhik.json")


async def cmd_crawl(page, city, args):
    tree = await get_tree(page, city)
    leaves = get_filtered_leaves(tree, args)
    print(f"Категорий к обходу: {len(leaves)}")

    prev = {}
    if os.path.exists(args.out):
        try:
            with open(args.out, encoding="utf-8") as f:
                for r in json.load(f):
                    if r.get("plu") is not None:
                        prev[r["plu"]] = r
            if prev:
                print(f"Подхватил из {args.out}: {len(prev)} товаров.")
        except Exception:
            pass

    results, seen = [], set()
    consec_fail = 0
    saved_sample = False

    def dump():
        with open(args.out, "w", encoding="utf-8") as f:
            json.dump(results, f, ensure_ascii=False, indent=2)

    aborted = False
    for ci, leaf in enumerate(leaves, 1):
        if aborted:
            break
        cpage = 1
        category_seen = set()
        while True:
            try:
                plist = await _retry(lambda: get_products(page, leaf["id"], cpage, city),
                                     f"список {leaf['name']} стр.{cpage}")
            except Exception as e:
                print(f"  [{ci}/{len(leaves)}] {leaf['name']} стр.{cpage}: не получен ({e})")
                break
            products = _find_id_list(plist)
            if not products:
                break
            
            # Если все товары на этой странице мы уже видели в этой категории,
            # значит мы попали в зацикливание пагинации (API вернул ту же/последнюю страницу)
            current_page_pids = {p.get("id") for p in products if p.get("id") is not None}
            if not current_page_pids or current_page_pids.issubset(category_seen):
                break
            category_seen.update(current_page_pids)

            print(f"  [{ci}/{len(leaves)}] {leaf['name']} стр.{cpage}: {len(products)} товаров")

            for p in products:
                pid = p.get("id")
                if pid in seen:
                    continue
                seen.add(pid)
                old = prev.get(pid)
                if old and old.get("nutrition"):
                    results.append(old)
                    dump()
                    continue
                item = {
                    "plu": pid,
                    "name": p.get("title") or p.get("name"),
                    "category": leaf["name"],
                    "price": None,
                    "weight": None,
                    "description": "",
                    "nutrition": {},
                }
                try:
                    info = await _retry(lambda pid=pid: get_info(page, pid, city),
                                        f"карточка {pid}")
                    if not saved_sample:
                        with open("sample_chizhik.json", "w", encoding="utf-8") as f:
                            json.dump(info, f, ensure_ascii=False, indent=2)
                        saved_sample = True
                    fields = chizhik_fields(info)
                    item["name"] = fields["name"] or item["name"]
                    item["article"] = fields["article"]
                    item["price"] = fields["price"]
                    item["old_price"] = fields["old_price"]
                    item["weight"] = fields["weight"]
                    item["description"] = fields["description"]
                    item["nutrition"] = fields["nutrition"]
                    consec_fail = 0
                except Exception as e:
                    item["error"] = str(e)
                    consec_fail += 1
                results.append(item)
                dump()
                if consec_fail >= 6:
                    print("\nАнтибот требует проверку. Обнови chizhik.club в окне Chrome, "
                          "пройди её и запусти ту же команду снова (докачает).")
                    aborted = True
                    break
                await asyncio.sleep(args.delay)

            # Конец пагинации по метаданным ответа (API за последней страницей
            # не отдаёт пустоту, а повторяет последнюю — поэтому смотрим total_pages/next).
            if aborted:
                break
            total_pages = plist.get("total_pages") if isinstance(plist, dict) else None
            nxt = plist.get("next") if isinstance(plist, dict) else None
            if total_pages and cpage >= total_pages:
                break
            if total_pages is None and not nxt:
                break
            cpage += 1
            await asyncio.sleep(args.delay)
        await asyncio.sleep(args.cat_delay)

    dump()
    with_bju = sum(1 for r in results if r["nutrition"])
    print(f"\nГотово{' (прервано)' if aborted else ''}. Товаров: {len(results)}, из них с БЖУ: {with_bju}.")
    print(f"Результат -> {args.out}")


async def main():
    ap = argparse.ArgumentParser(description="Парсер Чижика (name/description/БЖУ) без chizhik_api.")
    ap.add_argument("--cdp", type=int, default=9222, metavar="PORT", help="порт отладки Chrome")
    ap.add_argument("--list-categories", action="store_true")
    ap.add_argument("--probe", action="store_true", help="разведка: категории + один товар")
    ap.add_argument("--categories", help="id категорий через запятую (по умолчанию все)")
    ap.add_argument("--food-only", action="store_true", help="собирать только категории еды и исключить 'Надо успеть'")
    ap.add_argument("--city", default="Ростов-на-Дону", help="город (цены/ассортимент по городу)")
    ap.add_argument("--delay", type=float, default=0.7, help="пауза между товарами, сек")
    ap.add_argument("--cat-delay", type=float, default=2.0, help="пауза между категориями, сек")
    ap.add_argument("--out", default="chizhik.json", help="файл результата")
    args = ap.parse_args()

    pw = browser = None
    try:
        pw, browser, page = await connect(args.cdp)
        await warmup(page)
        city = await get_city_id(page, args.city)
        if args.list_categories:
            await cmd_list_categories(page, city, args)
        elif args.probe:
            await cmd_probe(page, city, args)
        else:
            await cmd_crawl(page, city, args)
    except RuntimeError as e:
        print(f"\nОшибка: {e}")
    finally:
        try:
            if browser:
                await browser.close()
        except Exception:
            pass
        try:
            if pw:
                await pw.stop()
        except Exception:
            pass


if __name__ == "__main__":
    asyncio.run(main())
