# Helen-Vault

برنامج مستقل مخصّص **فقط** لإدارة الرموز السرية المرتبطة بخادم Helen:
- Master code للـadmin وللـVault نفسه
- Share codes للمستخدمين (مع الصور والمعلومات)
- أرقام الوصول (invite / guest-auth / share) التي ينشئها العملاء

## متى تستخدم هذه اللوحة؟

- لعرض كل الرموز في مكان واحد
- لإلغاء أي رمز عميل بصلاحية عليا
- لعرض بصمة master codes دون كشف القيم
- لتدوير vault master code بعد اشتباه أمني

## الوصول

1. على نفس الراوتر فقط (IP في 10.x / 172.16-31.x / 192.168.x / 127.x)
2. Master code منفصل: `data/vault_master_code.txt` (يُولَّد مرّة، يُطبع على console)
3. Session TTL = 30 دقيقة
4. Header: `X-Vault-Token`

افتح: `http://<server>:3000/vault/`

## ما يَعرضه

| التاب | المحتوى |
|---|---|
| **لمحة** | إحصائيات + معلومات vault (file path, admin-master fingerprint, جلساتك) + زر تدوير vault master |
| **المستخدمون** | كل user مع avatar + role (admin/user) + share_code قابل للنسخ |
| **أرقام الوصول** | جدول كامل للـ access codes مع الاستخدامات، الانتهاء، الإلغاء |
| **الأدمن** | فقط المستخدمون الذين رولهم admin، وجدول المستويات الثلاثة (admin master · vault master) |

## اللون التمييزي

- الأدمن العادي (`/admin/`) → أزرق
- الأدمن السرّي (`/admin-secret/`) → برتقالي
- **الـVault (هنا)** → قرمزي أحمر (الأعلى حساسية)

## الأمان

- LAN-only enforcement في backend (middleware يردّ 403 لأي طلب من عنوان خارج RFC1918 + loopback)
- الـplaintext لأي master code لا يُعرض — فقط sha256 fingerprint أول 12 char
- كل عملية revocation تُسجَّل مع IP العامل
- Sessions in-memory فقط — إعادة تشغيل السيرفر تُبطلها كلها
