# Helen-Server 1.0.0 — macOS Native Bundle

سيرفر Helen كامل لنظام macOS، يعمل على **Intel و Apple Silicon** (M1/M2/M3/M4) بشكل أصلي بدون Docker.

## 🔌 Helen مشروع 100% داخلي

كل الاتصال داخل شبكتك المحلية فقط. لا إنترنت، لا cloud، لا خدمات خارجية.

## 📋 المتطلبات

- macOS 10.15 Catalina أو أحدث (موصى به: macOS 13+)
- Python 3.11 أو أحدث

### تثبيت Python على Mac

```bash
# الأسهل: من الموقع الرسمي
# https://www.python.org/downloads/macos/

# أو عبر Homebrew (إذا متوفر):
brew install python@3.12
```

## 🚀 التثبيت والتشغيل

```bash
tar xzf helen-server-macos-1.0.0.tar.gz
cd helen-server-macos-1.0.0
./install.sh        # تثبيت لأول مرة
./start.sh          # تشغيل السيرفر
```

## 🔧 تشغيل كخدمة (تبدأ تلقائياً عند الإقلاع)

```bash
./install-service.sh
```

السيرفر يصبح خدمة launchd. للتحكم:
```bash
launchctl start local.helen.server
launchctl stop  local.helen.server
launchctl unload ~/Library/LaunchAgents/local.helen.server.plist
```

## 🌐 المنافذ

| البروتوكول | المنفذ | الاستخدام |
|---|---|---|
| HTTP REST + Socket.IO | `3000` | الاتصال الرئيسي |
| HTTPS | `3443` | الاتصال المشفّر |
| UDP broadcast | `41234` | LAN auto-discovery |
| mDNS | `_helen-server._tcp` | Bonjour discovery |

## 🧪 التحقق

بعد التشغيل افتح في Safari:
```
http://localhost:3000/health
```

أو من جهاز آخر في الشبكة:
```
http://<IP_جهاز_Mac>:3000/health
```

## 📡 يخدم كل العملاء

- Helen Desktop (Windows / Linux)
- Helen Mobile (Android APK)
- Helen Web PWA (يعمل على iPhone / iPad / Mac عبر Safari)
- Helen Admin (panel على /admin/)

## ⚙️ متغيرات البيئة

كل الإعدادات في `.env`. الأهم:

```ini
JWT_SECRET=...      # 32+ حرف، يُولّد تلقائياً عند install.sh
PORT=3000
HTTPS_PORT=3443
DEBUG=0             # 1 لتفعيل /docs و /redoc
```

## 🔒 ملاحظات أمان

- `JWT_SECRET` يُولّد عشوائياً عند التثبيت (32 byte hex)
- `Vault` يرفض أي IP خارج RFC1918
- CORS مقيّد لـ `localhost` و `app://.`
- لا حاجة لشهادة SSL خارجية — السيرفر داخلي

## 🛠️ إلغاء التثبيت

```bash
launchctl unload ~/Library/LaunchAgents/local.helen.server.plist 2>/dev/null
rm -f ~/Library/LaunchAgents/local.helen.server.plist
rm -rf /path/to/helen-server-macos-1.0.0
```
