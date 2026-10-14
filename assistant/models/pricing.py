"""أسعار النماذج (دولار لكل مليون توكن) وتحويل الكلفة إلى نقاط.

تم التحقق من الأسعار من الصفحات الرسمية بتاريخ 2026-10-13:
  Anthropic: https://platform.claude.com/docs/en/about-claude/pricing
  Google:    https://ai.google.dev/gemini-api/docs/pricing
الأسعار تتغير؛ عدّلها هنا فقط. شريحة Haiku للمطالبات فوق 100 ألف توكن أغلى ولا تُستعمل في هذا التطبيق (مطالباتنا صغيرة).
"""
from typing import Dict, Tuple

PRICES_VERIFIED_ON = "2026-10-13"

# اسم النموذج → (سعر الإدخال، سعر الإخراج) لكل مليون توكن بالدولار
PRICES_PER_MTOK: Dict[str, Tuple[float, float]] = {
    "claude-haiku-5-5": (0.10, 0.50),
    "claude-sonnet-5-5": (2.00, 10.00),
    "gemini-3.1-flash-lite": (0.25, 1.50),
    "gemini-3.5-flash-lite": (0.30, 2.50),
    "gemini-3.6-flash": (0.75, 3.75),   # سعر حتى 2026-12-31؛ يصبح 1.50/7.50 من 2027-01-01
    "gemini-3.1-pro-preview": (2.00, 12.00),
}

# كلفتنا الحالية لكل نقطة (دراسة الكلفة: ~3 سنت)؛ تُستبدل بالقياس الفعلي
COST_USD_PER_POINT = 0.03


# نموذج غير مسعَّر لا يُحتسب مجاناً أبداً: نستعمل أعلى سعر معروف (تقدير متحفظ) ويُنبَّه في التقرير
FALLBACK_PRICE: Tuple[float, float] = (2.00, 12.00)


def is_priced(model: str) -> bool:
    return model in PRICES_PER_MTOK


def cost_usd(model: str, input_tokens: int, output_tokens: int) -> float:
    """كلفة استدعاء بالدولار؛ النموذج المجهول يُحسب بأعلى سعر معروف (تقدير متحفظ)."""
    price = PRICES_PER_MTOK.get(model, FALLBACK_PRICE)
    return (input_tokens * price[0] + output_tokens * price[1]) / 1_000_000.0


def usd_to_points(usd: float, usd_per_point: float = COST_USD_PER_POINT) -> float:
    return usd / usd_per_point if usd_per_point > 0 else 0.0
