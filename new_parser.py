# new_parser.py
import aiohttp
import re
import logging
from typing import Dict, Optional, List, Any

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

class WBParser:
    async def setup(self):
        if not hasattr(self, 'session') or self.session is None:
            self.session = aiohttp.ClientSession()
            logger.info("✅ Сессия aiohttp создана")

    async def close(self):
        if hasattr(self, 'session') and self.session:
            await self.session.close()
            self.session = None
            logger.info("🛑 Сессия aiohttp закрыта")

    @staticmethod
    def extract_articul(url: str) -> Optional[str]:
        m = re.search(r'/catalog/(\d+)/detail', url)
        if m:
            return m.group(1)
        m2 = re.search(r'nm=(\d+)', url)
        if m2:
            return m2.group(1)
        return None

    async def parse_card_json(self, articul: str) -> Dict[str, Any]:
        """
        Парсинг card.json (если доступен) — собираем name, brand, description, images (полные url).
        """
        if not self.session:
            await self.setup()

        vol = articul[:4]
        part = articul[:6]
        json_url = f"https://sam-basket-cdn-01mt.geobasket.ru/vol{vol}/part{part}/{articul}/info/ru/card.json"
        try:
            async with self.session.get(json_url, timeout=10) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    name = data.get("imt_name") or data.get("name") or ""
                    brand = data.get("selling", {}).get("brand_name") or data.get("brand") or ""
                    description = data.get("description") or data.get("shortDescription") or ""
                    characteristics = {}
                    if isinstance(data.get("options"), list):
                        for opt in data.get("options", []):
                            try:
                                k = opt.get("name")
                                v = opt.get("value")
                                if k:
                                    characteristics[k] = v
                            except Exception:
                                continue

                    images: List[str] = []
                    # Попытки собрать URL-ы картинок, если они уже полные
                    for key in ("images", "imt_images", "pics", "gallery", "media", "mediaFiles"):
                        val = data.get(key)
                        if isinstance(val, list):
                            for it in val:
                                if isinstance(it, str) and it.startswith(("http://", "https://")):
                                    images.append(it)
                                elif isinstance(it, dict):
                                    u = it.get("url") or it.get("image")
                                    if isinstance(u, str) and u.startswith(("http://", "https://")):
                                        images.append(u)
                        elif isinstance(val, str) and val.startswith(("http://", "https://")):
                            images.append(val)

                    # очистка дубликатов
                    images = [u for i, u in enumerate(images) if images.index(u) == i]

                    return {
                        "name": name,
                        "brand": brand,
                        "description": description,
                        "characteristics": characteristics,
                        "images": images,
                    }
        except Exception as e:
            logger.debug(f"❌ Ошибка при получении card.json {json_url}: {e}", exc_info=True)

        return {}

    async def _check_url_is_image(self, url: str, timeout: float = 5.0) -> bool:
        """
        Проверка доступности URL-а картинки.
        Сначала делает HEAD, если HEAD отвечает неудачно — пытает GET с небольшим таймаутом и только заголовки.
        """
        if not self.session:
            await self.setup()
        try:
            # HEAD
            async with self.session.head(url, timeout=timeout, allow_redirects=True) as resp:
                if resp.status == 200:
                    ctype = resp.headers.get("Content-Type", "")
                    if ctype and ("image" in ctype or "webp" in ctype):
                        return True
                    # иногда WB отвечает без content-type, но статус 200 — считаем рабочим
                    return True
        except Exception:
            # попробуем GET, но не читаем тело полностью
            try:
                async with self.session.get(url, timeout=timeout, allow_redirects=True) as resp:
                    if resp.status == 200:
                        ctype = resp.headers.get("Content-Type", "")
                        if ctype and ("image" in ctype or "webp" in ctype or "jpeg" in ctype or "jpg" in ctype):
                            return True
                        # если нет content-type — всё равно принимаем 200
                        return True
            except Exception:
                return False
        return False
    
    async def _find_valid_images(self, articul: str, candidate_idxs: List[int] = None, max_images: int = 2) -> List[str]:
        """
        Попытки найти реальные рабочие URL для изображений:
        - используем набор доменов (sam-basket-cdn-01mt, ...),
        - пробуем шаблоны /images/c516x688/{i}.webp и /images/big/{i}.jpg и другие,
        - проверяем доступность через HEAD/GET.
        Возвращаем список до max_images валидных ссылок.
        """
        if not self.session:
            await self.setup()

        if candidate_idxs is None:
            candidate_idxs = list(range(1, max_images + 1))

        vol = articul[:4]
        part = articul[:6]
        bucket = str((int(articul) % 100)).zfill(2)

        # список доменов/шаблонов, порядок важен: наиболее вероятные — первыми
        domains = [
            "https://sam-basket-cdn-01mt.geobasket.ru",
            "https://sam-basket-cdn-02mt.geobasket.ru",
            "https://sam-basket-cdn-03mt.geobasket.ru",
            f"https://basket-{bucket}.wbbasket.ru",
            "https://img1.wbstatic.net",
        ]

        patterns = [
            "/vol{vol}/part{part}/{articul}/images/c516x688/{i}.webp",
            "/vol{vol}/part{part}/{articul}/images/c800x1000/{i}.webp",
            "/vol{vol}/part{part}/{articul}/images/big/{i}.jpg",
            "/vol{vol}/part{part}/{articul}/images/{i}.jpg",
            "/vol{vol}/part{part}/{articul}/images/{i}.webp",
        ]

        found: List[str] = []

        # Перебираем домены → паттерны → номера картинок и проверяем по очереди
        for d in domains:
            for pat in patterns:
                if len(found) >= max_images:
                    break
                for i in candidate_idxs:
                    if len(found) >= max_images:
                        break
                    url = d + pat.format(vol=vol, part=part, articul=articul, i=i)
                    try:
                        ok = await self._check_url_is_image(url, timeout=4.0)
                    except Exception:
                        ok = False
                    if ok:
                        found.append(url)
                        logger.debug(f"🖼️ Valid image found: {url}")
            if len(found) >= max_images:
                break

        # Если не нашлось ни одной — всё равно вернуть синтетические ссылки на наиболее вероятный домен
        if not found:
            # гарантируем хотя бы стандартные webp ссылки на sam-basket-cdn-01mt
            fallback_domain = "https://sam-basket-cdn-01mt.geobasket.ru"
            fallback = [
                fallback_domain + f"/vol{vol}/part{part}/{articul}/images/c516x688/{i}.webp"
                for i in candidate_idxs[:max_images]
            ]
            logger.warning(f"⚠️ Не найдено валидных изображений для {articul}. Возвращаем fallback URLs.")
            return fallback

        # убираем дубликаты, оставляем максимум max_images
        unique = []
        for u in found:
            if u not in unique:
                unique.append(u)
            if len(unique) >= max_images:
                break

        return unique
    
    async def parse_api_detail(self, articul: str) -> Dict[str, Any]:
        """
        Получение деталей товара через card.wb.ru (v2).
        Возвращает: id, name, price, basic_price, seller, rating, feedbacks, stocks, stocks_by_size, images.
        """
        if not self.session:
            await self.setup()

        url = f"https://card.wb.ru/cards/v2/detail?appType=1&curr=rub&dest=-1257786&lang=ru&nm={articul}"
        logger.info(f"📩 Запрос к WB API: {url}")

        try:
            async with self.session.get(url, timeout=10) as resp:
                if resp.status != 200:
                    logger.error(f"❌ WB API вернул статус {resp.status} для артикула {articul}")
                    return {}
                data = await resp.json()
        except Exception as e:
            logger.error(f"❌ Ошибка запроса к WB API для артикула {articul}: {e}", exc_info=True)
            return {}

        products = data.get("data", {}).get("products") or []
        if not products:
            logger.warning(f"⚠️ В ответе WB API нет products для артикула {articul}")
            return {}

        p = products[0]
        sizes = p.get("sizes") or []

        # логируем сырые поля
        logger.info(f"💰 WB RAW: salePriceU={p.get('salePriceU')}, priceU={p.get('priceU')} | sizes_count={len(sizes)}")

        # --- цены ---
        sale_price = 0.0
        basic_price = 0.0
        try:
            sale_u = p.get("salePriceU")
            price_u = p.get("priceU")
            if sale_u:
                sale_price = float(sale_u) / 100.0
            if price_u:
                basic_price = float(price_u) / 100.0
        except Exception:
            pass

        # fallback через sizes[].price
        if (not sale_price or sale_price == 0.0) or (not basic_price or basic_price == 0.0):
            for s in sizes:
                try:
                    price_info = s.get("price") or s.get("prices") or {}
                    if isinstance(price_info, dict):
                        product_val = price_info.get("product") or price_info.get("sale") or price_info.get("total")
                        basic_val = price_info.get("basic") or price_info.get("old") or price_info.get("base")
                        if product_val:
                            sale_price = float(product_val) / 100.0
                        if basic_val:
                            basic_price = float(basic_val) / 100.0
                        if sale_price > 0:
                            logger.info(f"💰 Fallback price from sizes: {sale_price}/{basic_price}")
                            break
                except Exception:
                    continue

        discount = int(100 - (sale_price / basic_price * 100)) if basic_price else 0

        # --- изображения: сначала ищем прямые url в API, иначе используем поиск/генерацию ---
        images: List[str] = []
        # если в API есть поле images с int-индексами — сформируем ссылки с проверкой
        api_images = p.get("images")
        if isinstance(api_images, list) and api_images and all(isinstance(x, int) for x in api_images):
            # попробуем проверить для каждого индекса варианты доменов/паттернов
            images = await self._find_valid_images(articul, candidate_idxs=api_images, max_images=min(1, len(api_images)))
        else:
            # если API содержит уже url-ы (реже) — взять их
            possible_keys = ("images", "image", "imageUrl", "iis", "files", "media")
            for key in possible_keys:
                val = p.get(key)
                if isinstance(val, list):
                    for it in val:
                        if isinstance(it, str) and it.startswith(("http://", "https://")):
                            images.append(it)
                        elif isinstance(it, dict):
                            url = it.get("url") or it.get("image") or it.get("file")
                            if isinstance(url, str) and url.startswith(("http://", "https://")):
                                images.append(url)
                elif isinstance(val, str) and val.startswith(("http://", "https://")):
                    images.append(val)

            # если пока нет — попробовать искать первые 6 индексов
            if not images:
                images = await self._find_valid_images(articul, candidate_idxs=list(range(1, 3)), max_images=2)

        # очистка и уникализация
        images = [u for i, u in enumerate(images) if isinstance(u, str) and u.startswith(("http://", "https://")) and images.index(u) == i]

        # --- остатки: stocks_by_size и total ---
        stocks_by_size: List[Dict[str, Any]] = []
        for s in sizes:
            try:
                size_name = s.get("name") or s.get("size") or s.get("opt") or ""
                qty = 0
                stocks_arr = s.get("stocks") or s.get("offers") or []
                if isinstance(stocks_arr, list):
                    for st in stocks_arr:
                        if isinstance(st, dict):
                            try:
                                qty += int(st.get("qty", 0) or 0)
                            except Exception:
                                continue
                if not stocks_arr and s.get("qty") is not None:
                    try:
                        qty += int(s.get("qty") or 0)
                    except Exception:
                        pass
                stocks_by_size.append({"size": size_name, "qty": qty})
            except Exception:
                continue

        total_stocks = sum(item.get("qty", 0) for item in stocks_by_size)

        result: Dict[str, Any] = {
            "id": p.get("id") or int(articul),
            "name": p.get("name"),
            "brand": p.get("brand"),
            "supplier": p.get("supplierName") or p.get("supplier"),
            "seller": p.get("supplierName") or p.get("supplier"),
            "rating": p.get("reviewRating") or p.get("rating") or 0,
            "feedbacks": p.get("feedbacks") or 0,
            "price": float(round(sale_price, 2)),
            "basic_price": float(round(basic_price, 2)),
            "discount": discount,
            "stocks": total_stocks,
            "stocks_by_size": stocks_by_size,
            "images": images,
            "raw_product": p,  # можно убрать, но полезно для отладки
        }

        logger.info(f"✅ Итог для {articul}: price={result['price']} (base={result['basic_price']}), total_stocks={result['stocks']}, images={len(images)}")
        return result

    async def parse_product(self, url: str) -> Dict[str, Any]:
        """
        Основной метод: объединяем card.json и API (api_data имеет приоритет).
        """
        articul = self.extract_articul(url)
        if not articul:
            return {"success": False, "error": "Не удалось извлечь артикул из URL", "url": url}

        await self.setup()

        card_data = await self.parse_card_json(articul)
        api_data = await self.parse_api_detail(articul)

        if not card_data and not api_data:
            return {"success": False, "error": "Не удалось получить данные о товаре", "articul": articul}

        merged: Dict[str, Any] = {**card_data, **api_data}
        merged.update({
            "success": True,
            "articul": articul,
            "url": url,
            "id": int(api_data.get("id") or articul),
        })

        # если нет images из API — берем из card.json
        if not merged.get("images") and card_data.get("images"):
            merged["images"] = card_data.get("images")

        if merged.get("supplier") and not merged.get("seller"):
            merged["seller"] = merged.get("supplier")

        # можно удалить сырые данные, если не нужно
        # merged.pop("raw_product", None)

        return merged


# Утилиты
_parser: Optional[WBParser] = None

async def get_parser() -> WBParser:
    global _parser
    if _parser is None:
        _parser = WBParser()
    await _parser.setup()
    return _parser

async def parse_wb_product_api(url: str) -> Dict:
    parser = await get_parser()
    return await parser.parse_product(url)
