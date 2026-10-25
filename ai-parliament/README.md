# AI Parlament (Telegram, har bir agent — alohida bot)

Oltita AI agent: Mastermind, Strict English Tutor, Code & DB Optimizer, Time Cop, Joker, Security.
Bitta Python jarayoni oltita botni boshqaradi; har bir agent o'z Telegram boti va (xohlasangiz) o'z API kaliti bilan ishlaydi.

## Imkoniyatlar
- Guruh rejimi: barcha botlar bitta guruhda, har biri o'z nomidan yozadi
- `@tutor`/`@code`/... yoki botning @username'i yoki agent xabariga reply — shu agent batafsil javob beradi, qolganlar qisqa replika qo'shadi
- Tutor: xatolar daftari (`/mistakes`, yakshanba hisoboti) va ovozli xabarni baholash
- Security: Google qidiruv orqali faktlarni tekshiradi (Gemini)
- Har bir agentning shaxsiy yozuvlari (`/notes`)
- Time Cop: cho'zilgan suhbatni to'xtatadi, `/session` taymeri, Mastermind xulosasi

## Sozlash (qadamlar)
1. BotFather'da 6 ta bot yarating. Har biri uchun `/setprivacy` → **Disable** (aks holda guruhdagi oddiy xabarlarni ko'rmaydi).
2. Yangi guruh oching, oltita botni qo'shing (privacy o'zgargan bo'lsa, botni guruhdan chiqarib, qayta qo'shing).
3. `.env.example` ni `.env` ga nusxalab, tokenlar va kalitlarni kiriting. Birinchi marta `ALLOWED_USER_IDS` bo'sh qoladi:
   botga shaxsiy chatda `/start` yozing — ID'ingizni ko'rsatadi.
4. `python main.py`. Guruhda `/status` — qaysi bot guruhda emasligini ko'rsatadi, `/llmtest` — kalitlarni tekshiradi.

## Railway
GitHub'ga yuklang (`.env` yuklanmasin) → Deploy from GitHub repo → Variables'ga `.env` dagilarni kiriting →
ma'lumotlar saqlanishi uchun Postgres qo'shing: `DATABASE_URL=${{Postgres.DATABASE_URL}}` → Replicas = 1.

## Buyruqlar
/start, /agents, /status, /session <daqiqa> [mavzu], /stop, /xulosa, /mistakes, /notes, /llmtest, /reset, /myid
