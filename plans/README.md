# بررسی پروژه و نقشهٔ توسعه برای رزومهٔ Python Backend

مبنای بررسی: commit `f8900a8`، تاریخ محلی ۲۰۲۶-۰۹-۰۴. وضعیت اولیهٔ Git تمیز بود. فقط فایل‌های این پوشه در بررسی ایجاد شده‌اند؛ کد اجرایی تغییر نکرده است.

## نتیجهٔ بررسی

این پروژه اکنون یک ابزار داخلی ثبت سفارش در تلگرام است. نقاط قوت آن کنترل دسترسی سمت سرور، queryهای پارامتری، تراکنش ثبت سفارش، idempotency با draft_token، ایندکس تشخیص سفارش مشابه، HTML escaping، ثبت وضعیت ارسال، Docker با کاربر غیر root و تست‌های مستقل از شبکه است.

مسیر پیشنهادی برای رزومه: تبدیل همین پروژه به «سامانهٔ مدیریت سفارش و موجودی با API و رابط تلگرام». ارزش فنی اصلی از صحت تراکنش، کنترل دسترسی، تحمل خطا و شواهد تست می‌آید؛ تعداد کتابخانه‌ها معیار موفقیت نیست.

## شواهد و محدودیت بررسی

- همهٔ فایل‌های برنامه، تست‌ها، README، برنامهٔ اولیه، تنظیمات Docker و Git بررسی شدند.
- `python -B -m unittest discover -v`: هر ۲۳ تست موجود در Python 3.11.0 و aiogram 3.31.0 پاس شد.
- بررسی‌های تکمیلی آفلاین با SQLite موقت، FSM واقعی و فراخوانی handlerهای ثبت‌شده، همراه با transport/auth mock برای ادمین مجاز: رفتارهای F01 تا F05 و F08 بازتولید شدند. این بررسی‌ها تست اتصال واقعی تلگرام یا تست کامل dispatcher نبودند.
- پیاده‌سازی نصب‌شدهٔ Dispatcher بررسی شد: isolation پیش‌فرض غیرفعال است و polling پیش‌فرض eventها را به صورت task پردازش می‌کند.
- `python -B -m pip check` در Python عمومی سیستم شکست خورد: ناسازگاری google-genai/google-auth که dependency این پروژه نیست. این نتیجه باگ dependency پروژه تلقی نشده است. نصب تمیز مستقل بررسی نشد.
- نصب package، تغییر دادهٔ واقعی، ارسال پیام تلگرام، build/deploy، تست فشار، اجرای Python 3.10 و اسکن کامل CVE انجام نشد. pip-audit، coverage و Ruff در interpreter بررسی‌شده موجود نبودند؛ دربارهٔ نبود آسیب‌پذیری یا درصد پوشش ادعایی نمی‌شود.

## ایرادهای تأییدشده

P1: پیش از توسعهٔ قابلیت‌ها؛ P2: در آماده‌سازی نسخهٔ بعدی. S چند ساعت، M حدود یک تا دو روز، L چند روز؛ شامل تست. ریسک ستون زیر، ریسک خود اصلاح است. اطمینان همهٔ موارد بالا است؛ شدت اثر عملکردی F09 بدون benchmark اندازه‌گیری نشده است.

| شناسه | اولویت | ایراد و اثر | شواهد | تلاش / ریسک | اقدام |
|---|---|---|---|---|---|
| F01 | P1 | callbackهای تأیید، ویرایش و لغو به پیش‌نویس یا نسخهٔ پیش‌نمایش متصل نیستند. تأیید فقط وجود keyها را می‌سنجد؛ دکمهٔ قدیمی می‌تواند دادهٔ فعلی یا دادهٔ در حال ویرایش را ثبت کند. رضایت سفارش تکراری نیز به هشدار فعلی متصل نیست. | `order_bot/bot.py:87`, `:388`, `:406`, `:426`, `:457` | M / متوسط | شناسه و revision پیش‌نویس در callback، کنترل state و snapshot پیش‌نمایش؛ پلن 002 |
| F02 | P1 | در مرحلهٔ preview، عکس جدید بدون caption در draft ذخیره می‌شود اما preview تازه نشان داده نمی‌شود؛ عکس نهایی با عکس تأییدشده فرق می‌کند. | `order_bot/bot.py:341`, `:355` | S / کم | تغییر عکس باید preview و revision را تازه کند؛ پلن 002 |
| F03 | P1 | eventهای یک کاربر isolation ندارند. هنگام await ارسال، کاربر می‌تواند فرم جدید بسازد؛ پایان confirm قبلی با state.clear فرم جدید را پاک می‌کند. | `order_bot/bot.py:303`, `:443`, `:444`, `:521` | M / متوسط | isolation برای هر context و پاک‌سازی مشروط به draft؛ پلن 002 |
| F04 | P1 | سفارش پیش از پاسخ callback ذخیره می‌شود. خطای callback.answer مانع publish می‌شود و pending می‌ماند. تأیید مجدد state را پاک می‌کند و فقط برای failed دکمهٔ retry می‌دهد؛ startup هم فقط sending را بازیابی می‌کند. | `order_bot/bot.py:416`, `:432`, `:436`, `:442`; `order_bot/database.py:222` | M / متوسط | مستقل‌کردن acknowledgment از delivery و مسیر بازیابی pending/failed؛ پلن 003 |
| F05 | P2 | regex تلفن از Unicode-wide digit استفاده می‌کند ولی فقط ارقام فارسی/عربی تبدیل می‌شوند؛ ارقام Unicode دیگر canonical key متفاوت می‌سازند و duplicate detection را دور می‌زنند. | `order_bot/validation.py:6`, `:46`, `:48`; `order_bot/database.py:151` | S / کم؛ اصلاح دادهٔ قدیمی متوسط | خروجی ASCII قطعی و تست invariant؛ پلن 001 |
| F06 | P1 | تست‌ها جریان واقعی router/FSM را اجرا نمی‌کنند؛ تست preview فقط رشتهٔ دکمه‌ها را چک می‌کند. پوشش توصیف‌شده در README بیش از پوشش واقعی است. | `tests/test_bot.py:9`, `:51`, `:59`; `README.md:111` | M / کم | تست از Dispatcher.feed_update با transport جعلی؛ پلن 001 و regression در 002/003 |
| F07 | P2 | README پشتیبانی Python 3.10 را اعلام می‌کند ولی datetime.UTC حداقل Python 3.11 می‌خواهد؛ روی 3.10 import شکست می‌خورد. | `README.md:7`; `order_bot/database.py:8` | S / کم | هماهنگ‌کردن حداقل نسخه و CI؛ پلن 001 |
| F08 | P2 | /admins همهٔ ردیف‌ها را در یک پیام می‌فرستد؛ ۲۵ رکورد با طول مجاز می‌توانند از سقف پیام تلگرام عبور کنند. | `order_bot/bot.py:295`; `order_bot/database.py:90` | S / کم | تقسیم بر مرز ردیف و تست طول؛ پلن 001 |
| F09 | P2 | SQLite همگام مستقیماً داخل handlerهای async اجرا می‌شود؛ انتظار lock تا ۱۵ ثانیه می‌تواند event loop را متوقف کند. افت سرعت واقعی اندازه‌گیری نشده است. | `order_bot/database.py:34`, `:37`; `order_bot/bot.py:108`, `:416` | M / متوسط | در نسخهٔ فعلی offload کل عملیات DB با connection داخل worker؛ در توسعه async SQLAlchemy؛ پلن 004 |
| F10 | P2 | schema فقط CREATE TABLE IF NOT EXISTS است؛ سازوکار ارتقای schema داده‌های موجود وجود ندارد. برای افزودن آیتم سفارش/موجودی، شروع برنامه migration انجام نمی‌دهد. | `order_bot/database.py:40` | M / متوسط تا زیاد | Alembic، import و آزمون حفظ داده پیش از انتقال؛ پلن 004 |
| F11 | P2 | CI، lint/typecheck configuration و lock کامل وابستگی‌ها در فایل‌های tracked وجود ندارد. دو وابستگی مستقیم pin شده‌اند ولی وابستگی‌های غیرمستقیم قفل نشده‌اند. | `requirements.txt:1`; `Dockerfile:7`; فهرست tracked فاقد `.github/workflows` و `pyproject.toml` | M / کم | محیط تکرارپذیر و gate خودکار؛ پلن 001 |

### مواردی که باگ تلقی نشدند

- SQLite، polling تک‌نمونه‌ای و FSM حافظه‌ای تصمیم صریح `docs/PLAN.md` هستند. ماندگاری پیش‌نویس و اجرای چند نمونه قابلیت توسعه‌اند.
- محدودیت ابهام بین موفقیت ارسال تلگرام و ثبت نتیجه در DB در طرح اولیه مستند است. ادعای exactly-once برای ارسال تلگرام قابل دفاع نیست؛ F04 شکاف مستقل بازیابی است.
- خصوصی‌سازی کانال و بررسی اعضای پنهان در مستندات به راه‌اندازی سپرده شده است. نبود فهرست اعضای پنهان آسیب‌پذیری جدید گزارش نشده است.
- SQL injection و HTML injection در مسیرهای بررسی‌شده شواهدی نداشتند؛ query پارامتری و escaping موجودند.
- نبود API، پرداخت، سبد خرید و موجودی نقص نسبت به MVP اولیه نیست؛ فاصلهٔ آن تا هدف جدید رزومه است.
- Microservices، Kubernetes، Kafka، Elasticsearch و AI در این مرحله توجیه مشخصی ندارند. اضافه‌کردنشان پیش‌شرط حرفه‌ای‌شدن پروژه نیست.

## انتخاب مسیر محصول

۱. **هستهٔ سفارش با دو ورودی API و تلگرام — پیشنهاد اصلی.** منطق ذخیره و تأیید اکنون در bot.py است و مدل سفارش در database.py وجود دارد. استخراج سرویس مشترک، API و تست مستقل از تلگرام را ممکن می‌کند؛ هزینهٔ اصلی تعریف قرارداد و حفظ کنترل دسترسی قبلی است.

۲. **موجودی و سفارش چندآیتمی — قابلیت شاخص.** هر سفارش فعلی فقط یک product_raw و quantity دارد. افزودن SKU، order_items و رزرو موجودی مسئلهٔ واقعی تراکنش و هم‌زمانی ایجاد می‌کند و از قابلیت‌های عمومی CRUD متمایز است؛ نیازمند migration و آزمون PostgreSQL است.

۳. **ارسال قابل بازیابی و عملیات فروش — امتداد طبیعی پروژه.** delivery_status و retry از قبل وجود دارند. worker پایدار، صف ارسال ناموفق، تاریخچهٔ وضعیت و گزارش عملیاتی این بخش را کامل می‌کنند؛ پیچیدگی broker جداگانه را فقط با نیاز روشن اضافه کنید.

۴. **پرداخت آزمایشی و fulfillment — مرحلهٔ بعد.** amount و اطلاعات ارسال از قبل ثبت می‌شوند. پرداخت sandbox با webhook تکرارشونده، لغو و آزادسازی موجودی، tracking و مرجوعی ارزش عملی دارند؛ اتصال واقعی به درگاه به انتخاب کشور/ارائه‌دهنده وابسته است.

## معماری هدف

یک modular monolith با همان مخزن: API و bot ورودی‌اند، سرویس‌های کاربردی قوانین کسب‌وکار را اجرا می‌کنند، PostgreSQL منبع حقیقت است و worker از همان کد مشترک استفاده می‌کند. منطق سفارش نباید دو بار در bot و API نوشته شود.

~~~mermaid
flowchart LR
  A[FastAPI /api/v1] --> S[Order and Inventory Services]
  B[Telegram Bot] --> S
  S --> D[(PostgreSQL)]
  W[Delivery Worker] --> D
  W --> T[Telegram / Sandbox Integrations]
~~~

پیشنهاد فناوری: FastAPI، Pydantic، SQLAlchemy 2، Alembic و PostgreSQL. FastAPI برای OpenAPI و validation آماده مناسب این مسیر است ([مستندات رسمی](https://fastapi.tiangolo.com/features/)). قرارداد هر transaction و session باید مشخص باشد؛ یک AsyncSession بین taskهای هم‌زمان مشترک نباشد ([SQLAlchemy](https://docs.sqlalchemy.org/en/20/orm/extensions/asyncio.html)). برای migration از Alembic استفاده شود ([راهنما](https://alembic.sqlalchemy.org/en/latest/tutorial.html)).

Redis فقط هنگام افزودن FSM با TTL یا cache اندازه‌گیری‌شده؛ Celery فقط اگر زمان‌بندی و صف مستقل واقعاً لازم شد. برای شروع worker متکی به جدول outbox در PostgreSQL کافی است. Django + DRF هم در صورت اولویت بالای admin آماده گزینه است، ولی پیشنهاد این پلن حفظ مسیر async فعلی و FastAPI است.

## ترتیب اجرا و زمان‌بندی پیشنهادی

این تخمین برای یک نفر با حدود ۱۵ تا ۲۰ ساعت در هفته و آشنایی پایه با Python است؛ یادگیری ابزارها زمان اضافه می‌خواهد. تخمین کل حدود ۱۲ تا ۱۶ هفته است، نه تعهد زمانی.

| مرحله | زمان تقریبی | خروجی قابل تحویل | معیار پذیرش |
|---|---|---|---|
| ۱. baseline و اصلاح رفتار | هفتهٔ ۱ تا ۲ | CI، تست router، اصلاح F01 تا F08 | تست دکمهٔ قدیمی، ارسال هم‌زمان، failure و retry پاس شود |
| ۲. API و مدل داده | هفتهٔ ۳ تا ۵ | FastAPI، PostgreSQL، migration، نقش‌ها، OpenAPI | تست دسترسی object-level و migration روی DB خالی و نمونهٔ قبلی |
| ۳. سفارش و موجودی | هفتهٔ ۶ تا ۸ | کالا/SKU، مشتری، آدرس، آیتم سفارش، snapshot قیمت، رزرو | برای آخرین موجودی، از دو خرید هم‌زمان دقیقاً یک خرید پذیرفته شود |
| ۴. delivery و پرداخت آزمایشی | هفتهٔ ۹ تا ۱۰ | outbox worker، retry محدود، sandbox payment، webhook | restart کار را گم نکند؛ event تکراری اثر مالی/موجودی دوباره نداشته باشد |
| ۵. عملیات فروش | هفتهٔ ۱۱ تا ۱۲ | status history، tracking، فیلتر و pagination، گزارش و CSV | انتقال نامعتبر status رد شود؛ گزارش جمع سفارش‌های معتبر را نشان دهد |
| ۶. نسخهٔ رزومه | هفتهٔ ۱۳ تا ۱۶ | demo امن، metrics، benchmark، restore drill، README انگلیسی | راه‌اندازی مستند از clone تمیز، شواهد p95 و تست با تنظیمات دقیق، demo ۳ تا ۵ دقیقه‌ای |

حداقل نسخهٔ قابل ارائه بعد از مرحلهٔ ۳ می‌تواند آماده باشد، اگر مستندات، demo و CI همان موقع تکمیل شوند. مراحل بعدی نسخهٔ enhanced را می‌سازند.

## امکانات نسخهٔ enhanced

- کاتالوگ: SKU یکتا، دسته‌بندی، variant محدود، قیمت و فعال/غیرفعال‌سازی.
- سفارش: چند آیتم، snapshot قیمت و آدرس، محاسبهٔ مبلغ سمت سرور، مبلغ integer با واحد پول صریح، idempotency key و رد استفادهٔ مجدد با payload متفاوت.
- موجودی: رزرو اتمیک، انقضای رزرو، آزادسازی هنگام لغو، جلوگیری از overselling و ثبت گردش موجودی.
- دسترسی: owner، manager، seller و warehouse؛ ماتریس permission و تست دسترسی به سفارش دیگری. نمایش اطلاعات مشتری مطابق نقش؛ قرارداد محدود فعلی فروشنده آگاهانه بازنگری شود.
- عملیات: چرخهٔ draft/confirmed/packing/shipped/delivered/cancelled، وضعیت پرداخت مستقل، تاریخچهٔ تغییرات و tracking.
- یکپارچه‌سازی: تلگرام، پرداخت آزمایشی، webhook با امضای معتبر و deduplication، مدیریت خطا و reconciliation.
- گزارش: فروش بر بازهٔ زمانی، عملکرد فروشنده، کالاهای پرفروش، سفارش‌های ناموفق در انتشار و CSV امن در برابر formula injection.
- کیفیت عملیات: log ساختاریافته بدون PII، correlation_id، health/readiness، اندازهٔ backlog و خطاها، backup و آزمون restore، CI و تست‌های PostgreSQL.
- بعد از این نسخه: کوپن با سقف مصرف اتمیک، مرجوعی/refund، notification ایمیل، multi-store و upload مستقل از Telegram. هرکدام فقط پس از تعریف نیاز و تست مستقل.

## پلن‌های اجرایی مستقل

کاربر اجرای هر پنج پلن توسط subagentهای GPT Luna با نظارت و ثبت تغییرات در GitHub را تأیید کرده است. نتیجهٔ بازبینی و تست هر مرحله در [گزارش اجرا](EXECUTION.md) ثبت می‌شود. وضعیت DONE فقط پس از تأیید معیارهای پذیرش ثبت خواهد شد.

| پلن | وضعیت | وابستگی |
|---|---|---|
| [001 — محیط و تست قابل اتکا](001-verification-baseline.md) | DONE — ۳۴ تست و CI هر دو نسخه موفق | ندارد |
| [002 — صحت پیش‌نمایش و تأیید](002-draft-integrity.md) | IN PROGRESS | 001 |
| [003 — بازیابی delivery](003-delivery-recovery.md) | TODO | 001 و 002 |
| [004 — بک‌اند API و PostgreSQL](004-backend-foundation.md) | IN PROGRESS — بخش مستقل؛ ادغام وابسته به مراحل قبل | 001، 002، 003 |
| [005 — تجارت، عملیات و خروجی رزومه](005-commerce-and-portfolio.md) | TODO | 004 |

## چیزی که در رزومه نشان بدهید

README انگلیسی با مسئله، معماری، ERD، quickstart، تست و tradeoffها؛ Swagger قابل استفاده با دادهٔ ساختگی؛ ویدئوی ثبت سفارش از API و تلگرام؛ نمایش رقابت برای آخرین موجودی؛ قطع worker و بازیابی؛ webhook تکراری بدون اثر مضاعف؛ و گزارش benchmark با سخت‌افزار، داده، concurrency، مدت و p50/p95/error rate.

نمونهٔ bullet پس از پیاده‌سازی و اندازه‌گیری: «Built a Python order-management backend with FastAPI and PostgreSQL, atomic inventory reservations, idempotent order creation, and recoverable Telegram delivery; validated with integration and concurrency tests.» عدد performance یا ادعای production-scale فقط از اندازه‌گیری واقعی اضافه شود.

منابع تکمیلی: [ورود datetime.UTC در Python 3.11](https://docs.python.org/3.11/whatsnew/3.11.html)، [تنظیمات Dispatcher](https://docs.aiogram.dev/en/dev-3.x/dispatcher/dispatcher.html)، [قفل‌گذاری PostgreSQL](https://www.postgresql.org/docs/current/explicit-locking.html).
