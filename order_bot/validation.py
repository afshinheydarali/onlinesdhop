from __future__ import annotations

import re


_DIGITS = str.maketrans("۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧٨٩", "01234567890123456789")
_CAPTION_KEYS = {
    "نام": "customer_name",
    "تلفن": "phone",
    "استان": "province",
    "شهر": "city",
    "آدرس": "address",
    "کدپستی": "postal_code",
    "محصول": "product",
    "تعداد": "quantity",
    "مبلغ": "amount",
    "توضیحات": "notes",
}
REQUIRED_FIELDS = ("customer_name", "phone", "province", "city", "address", "product", "quantity")
CAPTION_TEMPLATE = """نام: علی رضایی
تلفن: 09121234567
استان: تهران
شهر: تهران
آدرس: خیابان نمونه، پلاک ۱
کدپستی:
محصول: کد یا نام محصول
تعداد: 1
مبلغ:
توضیحات:"""


def clean_text(value: str, *, maximum: int, field: str) -> str:
    value = " ".join(value.strip().split())
    if not value:
        raise ValueError(f"{field} نمی‌تواند خالی باشد.")
    if len(value) > maximum:
        raise ValueError(f"{field} حداکثر {maximum} نویسه است.")
    return value


def normalize_phone(value: str) -> str:
    value = value.translate(_DIGITS)
    value = re.sub(r"[\s\-()]", "", value)
    if value.startswith("+98"):
        value = value[1:]
    if re.fullmatch(r"09\d{9}", value):
        value = "98" + value[1:]
    if not re.fullmatch(r"989\d{9}", value):
        raise ValueError("شماره تماس باید مانند 09121234567 باشد.")
    return value


def normalize_product(value: str) -> str:
    return clean_text(value, maximum=200, field="محصول").casefold()


def parse_positive_int(value: str, *, field: str, maximum: int) -> int:
    normalized = value.translate(_DIGITS).replace(",", "").replace("٬", "").strip()
    if not normalized.isdecimal():
        raise ValueError(f"{field} باید عدد صحیح باشد.")
    result = int(normalized)
    if not 1 <= result <= maximum:
        raise ValueError(f"{field} باید بین ۱ و {maximum} باشد.")
    return result


def parse_optional_amount(value: str) -> int | None:
    return None if not value.strip() else parse_positive_int(value, field="مبلغ", maximum=10**15)


def parse_caption(caption: str) -> dict[str, object]:
    values: dict[str, str] = {}
    for raw_line in caption.splitlines():
        if not raw_line.strip():
            continue
        key, separator, value = raw_line.partition(":")
        key = key.strip()
        if not separator or key not in _CAPTION_KEYS:
            raise ValueError("قالب caption شناخته نشد.")
        canonical = _CAPTION_KEYS[key]
        if canonical in values:
            raise ValueError(f"فیلد «{key}» تکراری است.")
        values[canonical] = value.strip()
    if any(not values.get(field) for field in REQUIRED_FIELDS):
        raise ValueError("فیلدهای الزامی caption کامل نیستند.")
    return {
        "customer_name": clean_text(values["customer_name"], maximum=120, field="نام"),
        "phone": clean_text(values["phone"], maximum=30, field="تلفن"),
        "phone_normalized": normalize_phone(values["phone"]),
        "province": clean_text(values["province"], maximum=80, field="استان"),
        "city": clean_text(values["city"], maximum=80, field="شهر"),
        "address": clean_text(values["address"], maximum=600, field="آدرس"),
        "postal_code": clean_text(values["postal_code"], maximum=20, field="کدپستی") if values.get("postal_code") else None,
        "product": clean_text(values["product"], maximum=200, field="محصول"),
        "product_normalized": normalize_product(values["product"]),
        "quantity": parse_positive_int(values["quantity"], field="تعداد", maximum=100_000),
        "amount": parse_optional_amount(values.get("amount", "")),
        "notes": clean_text(values["notes"], maximum=1000, field="توضیحات") if values.get("notes") else None,
    }

