# ربات ثبت سفارش آنلاین‌شاپ

ربات تک‌نمونه‌ای Telegram با Python و aiogram که سفارش را فقط از ادمین‌های فعال می‌گیرد، تصویر و پیش‌نمایش نشان می‌دهد، سفارش تکراری را بدون افشای اطلاعات قبلی تشخیص می‌دهد و نتیجه را به کانال خصوصی مدیریت می‌فرستد. اطلاعات در SQLite ذخیره می‌شوند و شکست ارسال قابل retry است.

## پیش‌نیازها

- Python 3.10 تا 3.14 (پیشنهاد: 3.12)
- یا Docker و Docker Compose
- یک Bot و یک کانال خصوصی Telegram

نسخه پروژه روی [`aiogram 3.31.0`](https://pypi.org/project/aiogram/) ثابت شده است. این نسخه در زمان ساخت، نسخه پایدار منتشرشده و سازگار با Python 3.10 تا 3.14 است.

## ساخت Bot و کانال

1. در Telegram به `@BotFather` پیام بدهید، `/newbot` را اجرا و token را فقط در `.env` نگهداری کنید.
2. Telegram User ID مدیر اصلی را از بخش اطلاعات حساب یک کلاینت مطمئن یا خروجی `getUpdates` Bot API به‌دست آورید. ID کاربر عدد مثبت است.
3. یک **Private Channel** بسازید. فقط مدیر اصلی و Bot باید دسترسی داشته باشند.
4. Bot را administrator کانال کنید و مجوز `Post Messages` بدهید. برنامه هنگام شروع بررسی می‌کند که مدیر اصلی و Bot هر دو administrator باشند و administrator اضافه‌ای وجود نداشته باشد.
5. برای Channel ID می‌توانید لینک یکی از پیام‌های کانال خصوصی را کپی کنید. در لینک `https://t.me/c/1234567890/1`، مقدار Channel ID برابر `-1001234567890` است. Channel ID عدد منفی است.

Bot API امکان فهرست‌کردن همه subscriberهای پنهان کانال را ندارد؛ بنابراین حذف اعضای غیرضروری بر عهده مدیر اصلی است. برنامه administratorهای کانال را کنترل می‌کند.

## تنظیمات محیطی

فایل نمونه را کپی کنید و مقادیر واقعی را فقط در `.env` قرار دهید:

```powershell
Copy-Item .env.example .env
```

```dotenv
BOT_TOKEN=توکن BotFather
OWNER_TELEGRAM_ID=123456789
ORDERS_CHANNEL_ID=-1001234567890
DATABASE_PATH=data/orders.sqlite3
DUPLICATE_WINDOW_DAYS=30
APP_TIMEZONE=Asia/Tehran
LOG_LEVEL=INFO
```

- `DATABASE_PATH`: محل SQLite؛ پوشه والد خودکار ساخته می‌شود.
- `DUPLICATE_WINDOW_DAYS`: بازه مقایسه تلفن normalize‌شده و محصول normalize‌شده.
- `APP_TIMEZONE`: timezone معتبر IANA برای نمایش زمان؛ زمان ذخیره‌شده همیشه UTC است.
- فایل `.env` توسط Git و Docker build نادیده گرفته می‌شود.

## اجرای محلی

PowerShell:

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
Get-Content .env | ForEach-Object {
    if ($_ -match '^([^#=]+)=(.*)$') { [Environment]::SetEnvironmentVariable($matches[1], $matches[2], 'Process') }
}
python -m order_bot
```

Linux/macOS:

```bash
python3 -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt
set -a; . ./.env; set +a
python -m order_bot
```

فقط یک instance را روی یک فایل SQLite اجرا کنید.

## مدیریت ادمین‌ها

مدیر اصلی در چت خصوصی Bot این دستورات را اجرا می‌کند:

```text
/admin_add 123456789 ADM-001 نام فروشنده
/admin_disable 123456789
/admin_enable 123456789
/admins
```

Telegram User ID و کد ادمین هر دو یکتا هستند. غیرفعال‌سازی فوراً ادامه فرم و ثبت نهایی را مسدود می‌کند. مدیر اصلی برای ثبت سفارش باید مانند هر فروشنده با `/admin_add` به فهرست ادمین‌ها اضافه شود؛ مالک‌بودن به‌تنهایی دسترسی سفارش نمی‌دهد.

## ثبت سفارش و قالب caption

ادمین فعال روی «ثبت سفارش جدید» می‌زند و فرم مرحله‌ای را پر می‌کند. «بازگشت»، «شروع مجدد» و «لغو» در طول فرم در دسترس‌اند. در مرحله عکس، تصویر از گالری قابل ارسال است.

یک عکس را می‌توان در هر مرحله با caption کامل زیر فرستاد تا فرم یکجا پردازش شود:

```text
نام: علی رضایی
تلفن: 09121234567
استان: تهران
شهر: تهران
آدرس: خیابان نمونه، پلاک ۱
کدپستی:
محصول: SKU-100
تعداد: 1
مبلغ:
توضیحات:
```

کلیدها باید دقیقاً همین باشند. `کدپستی`، `مبلغ` و `توضیحات` اختیاری‌اند، ولی خط آن‌ها در قالب پذیرفته می‌شود. caption نامعتبر باعث بازگشت به دریافت مرحله‌ای می‌شود. شماره‌های `09...`، `989...` و `+989...` و ارقام فارسی/عربی یکسان‌سازی می‌شوند.

## تست‌ها

```powershell
python -m unittest discover -v
python -m compileall -q order_bot tests
```

تست‌ها دسترسی، مدیریت و غیرفعال‌سازی ادمین، اعتبارسنجی تلفن و ارقام فارسی، caption معتبر/نامعتبر، ثبت معمولی و عکس، تکراری و تأیید دوم، عدم افشای سفارش قبلی، idempotency، کنترل‌های پیش‌نمایش، شکست/retry انتشار، تفکیک caption طولانی و HTML escaping را پوشش می‌دهند.

## اجرای Docker

پس از ساخت `.env`:

```powershell
docker compose up -d --build
docker compose logs -f bot
```

داده در volume نام‌گذاری‌شده `orders-data` باقی می‌ماند. توقف امن:

```powershell
docker compose down
```

گزینه `-v` را به `down` اضافه نکنید، چون volume دیتابیس را حذف می‌کند.

## پشتیبان‌گیری SQLite

برای اجرای محلی، ابتدا Bot را متوقف کنید و سپس از API داخلی backup SQLite استفاده کنید تا WAL نیز درست لحاظ شود:

```powershell
python -c "import sqlite3; s=sqlite3.connect('data/orders.sqlite3'); d=sqlite3.connect('orders-backup.sqlite3'); s.backup(d); d.close(); s.close()"
```

برای Docker، یک backup سازگار داخل volume بسازید و بعد کپی کنید:

```powershell
docker compose exec bot python -c "import sqlite3; s=sqlite3.connect('/data/orders.sqlite3'); d=sqlite3.connect('/data/orders-backup.sqlite3'); s.backup(d); d.close(); s.close()"
docker compose cp bot:/data/orders-backup.sqlite3 .\orders-backup.sqlite3
```

فایل backup حاوی اطلاعات شخصی مشتری است؛ آن را رمزگذاری و دسترسی‌اش را محدود کنید.

## نکات امنیتی

- هر پیام و callback در سرور بر اساس User ID، چت خصوصی و وضعیت فعال دوباره کنترل می‌شود.
- فروشنده هیچ endpoint یا دستور فهرست/آمار سفارش ندارد؛ retry فقط برای سفارش خود او مجاز است.
- queryها parameterized هستند، متن کانال HTML-escape می‌شود و اطلاعات مشتری در log نوشته نمی‌شود.
- `file_id` عکس نگهداری می‌شود؛ فایل عکس دوباره دانلود نمی‌شود.
- token را rotate کنید اگر در chat، log، تاریخچه shell یا Git افشا شد. `.env` را commit نکنید.
- از فایل SQLite و backupها مانند داده شخصی محافظت کنید و دسترسی filesystem را حداقلی نگه دارید.

## عیب‌یابی

- `Missing required environment variables`: متغیرهای اجباری خالی‌اند یا `.env` در اجرای محلی load نشده است.
- `ORDERS_CHANNEL_ID must refer to a channel`: ID اشتباه یا مربوط به group است.
- `owner and bot must both be channel administrators`: مدیر اصلی/Bot در کانال admin نیست یا ID مدیر اشتباه است.
- `unexpected administrators`: administrator دیگری در کانال مدیریت وجود دارد؛ او را حذف یا downgrade کنید.
- ارسال ناموفق: سفارش در دیتابیس با وضعیت `failed` می‌ماند؛ فروشنده از دکمه «تلاش مجدد ارسال» همان سفارش استفاده می‌کند.
- `database is locked`: بیش از یک instance روی یک SQLite اجرا شده یا پردازش دیگری transaction طولانی دارد؛ به یک instance برگردید.
- پس از restart، فرم نیمه‌کاره پاک می‌شود؛ سفارش‌های تأییدشده پاک نمی‌شوند.

## ساختار پروژه

```text
order_bot/config.py      تنظیمات environment
order_bot/validation.py  اعتبارسنجی و normalize/caption
order_bot/database.py    schema و تراکنش‌های SQLite
order_bot/bot.py         handlerها، FSM، preview و انتشار
tests/                   تست‌های unittest
docs/PLAN.md             برنامه و تصمیم‌های اولیه
```
