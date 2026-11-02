# M3U Link Manager — FastAPI + Vercel

مشروع لوحة بسيطة لرفع ملفات M3U وإنشاء رابط مباشر على نفس نطاق موقعك. جميع ملفات المشروع موجودة في الجذر، ولا توجد مجلدات فرعية.

## الملفات

- `app.py` — تطبيق FastAPI والواجهة وواجهات API.
- `requirements.txt` — المكتبات المطلوبة.
- `.python-version` — إصدار Python المستخدم على Vercel.
- `.env.example` — نموذج متغيرات البيئة.

## النشر على Vercel

1. فك ضغط ZIP وارفع **محتويات المشروع نفسها** إلى مستودع GitHub أو استورد المجلد في Vercel.
2. في مشروع Vercel افتح **Storage** ثم أنشئ **Blob Store** جديدًا.
3. اختر نوع الوصول **Public**، ثم اربط المخزن بالمشروع وفعّل البيئة Production (وPreview عند الحاجة). يجب أن يضيف Vercel بيانات اعتماد Blob للمشروع.
4. افتح **Settings → Environment Variables** وأضف:
   - `ADMIN_PASSWORD` = `admin1234` (يمكنك تغييره إلى كلمة أقوى).
   - `SESSION_SECRET` = قيمة عشوائية طويلة. أنشئها مثلًا بالأمر: `python -c "import secrets; print(secrets.token_urlsafe(48))"`.
5. أعد نشر المشروع Deploy.

FastAPI مدعوم مباشرة على Vercel؛ نقطة الدخول هي `app.py` وفيها المتغير `app`.

## الاستخدام

1. افتح رابط الموقع.
2. سجل الدخول بكلمة المرور التي وضعتها في `ADMIN_PASSWORD` (الافتراضي `admin1234`).
3. ارفع ملف `.m3u` أو `.m3u8`.
4. انسخ الرابط الذي يظهر وأضفه إلى تطبيقك.

سيكون شكل الرابط مثل:

`https://your-project.vercel.app/p/0123456789abcdef0123456789abcdef.m3u`

الرابط يعيد نص M3U نفسه مباشرة، ولا يحتاج إلى تسجيل دخول عند فتحه داخل التطبيق.

## حدود وملاحظات مهمة

- الحد الأقصى للرفع في هذه النسخة 4 ميغابايت، لأن طلبات Vercel Functions لها حد جسم طلب يبلغ 4.5 MB.
- يجب أن يكون Blob Store من نوع **Public** لأن روابط القوائم مخصصة للاستخدام المباشر داخل التطبيقات.
- أي شخص يملك رابط القائمة يستطيع فتحها؛ لا ترفع قوائم خاصة أو محتوى لا تملك إذنًا لمشاركته.
- Vercel لا يحفظ الملفات المرفوعة بشكل دائم على القرص المحلي. هذا المشروع يستخدم Vercel Blob للتخزين الدائم.
- لا تغيّر `SESSION_SECRET` بعد النشر إلا عند الحاجة؛ تغييره سيؤدي إلى تسجيل خروج الجلسات الحالية.

## تشغيل محلي اختياري

```bash
python -m venv .venv
# Windows: .venv\\Scripts\\activate
# Linux / macOS / Termux: source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
uvicorn app:app --host 0.0.0.0 --port 8000
```

للتخزين محليًا، أضف `BLOB_READ_WRITE_TOKEN` صالحًا لمخزن Vercel Blob إلى ملف `.env`.
