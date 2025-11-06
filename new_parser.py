# new_parser.py
import aiohttp
import re
import logging
from typing import Dict, Optional

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

    async def parse_card_json(self, articul: str) -> Dict:
        """Первичный парсинг через card.json"""
        vol = articul[:4]
        part = articul[:6]
        json_url = f"https://sam-basket-cdn-01mt.geobasket.ru/vol{vol}/part{part}/{articul}/info/ru/card.json"

        try:
            async with self.session.get(json_url, timeout=10) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    name = data.get('imt_name', '')
                    brand = data.get('selling', {}).get('brand_name', '')
                    description = data.get('description', '')
                    characteristics = {opt['name']: opt['value'] for opt in data.get('options', [])}
                    return {
                        'name': name,
                        'brand': brand,
                        'description': description,
                        'characteristics': characteristics
                    }
        except Exception as e:
            logger.error(f"❌ Ошибка при получении card.json: {e}")

        return {}

    async def parse_api_detail(self, articul: str) -> Dict:
        """Получение цены, скидок, рейтинга и поставщика через API"""
        url = f"https://u-card.wb.ru/cards/v4/detail?appType=1&curr=rub&dest=-2133462&lang=ru&nm={articul}"

        try:
            async with self.session.get(url, timeout=10) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    products = data.get("data", {}).get("products") or data.get("products", [])
                    if not products:
                        return {}
                    p = products[0]
                    # Берем первую размерную позицию с ценой
                    sizes = p.get("sizes", [])
                    price_list = [
                        (s["price"]["basic"], s["price"]["product"])
                        for s in sizes
                        if s.get("price") and s["price"].get("product", 0) > 0
                    ]

                    if price_list:
                        # Берём минимальную цену product и соответствующую basic
                        basic_min, product_min = min(price_list, key=lambda x: x[1])
                        basic_price = basic_min / 100
                        product_price = product_min / 100
                        discount = int(100 - (product_price / basic_price * 100)) if basic_price else 0
                    else:
                        basic_price = product_price = discount = 0

                    return {
                        'brand': p.get('brand'),
                        'supplier': p.get('supplier'),
                        'rating': p.get('reviewRating') or p.get('rating', 0),
                        'feedbacks': p.get('feedbacks', 0),
                        'price': product_price,
                        'basic_price': basic_price,
                        'discount': discount
                    }
        except Exception as e:
            logger.error(f"❌ Ошибка при запросе detail API: {e}")

        return {}

    async def parse_product(self, url: str) -> Dict:
        """Основной метод: объединяет данные из двух источников"""
        articul = self.extract_articul(url)
        if not articul:
            return {'success': False, 'error': 'Не удалось извлечь артикул из URL', 'url': url}

        await self.setup()

        card_data = await self.parse_card_json(articul)
        api_data = await self.parse_api_detail(articul)

        if not card_data and not api_data:
            return {'success': False, 'error': 'Не удалось получить данные о товаре', 'articul': articul}

        merged = {**card_data, **api_data}
        merged.update({
            'success': True,
            'articul': articul,
            'url': url
        })
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
