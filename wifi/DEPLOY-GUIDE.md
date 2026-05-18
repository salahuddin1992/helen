# Helen — دليل النشر الشامل (LAN-only، بدون إنترنت)

> **القاعدة الذهبية:** كل شيء يعمل داخل شبكتك الخاصة (LAN/intranet/private fiber). لا إنترنت، لا cloud، لا خدمات خارجية.

## 📋 المتطلبات

- جهاز سيرفر واحد على الأقل (Linux أو Windows)
- أجهزة عملاء (Windows / Linux / Mac / Android / iPhone)
- شبكة محلية واحدة أو شبكات متعددة (مع Rendezvous للـ NAT traversal)

---

## 🏗️ معمارية النشر

```
┌─────────────────────────────────────────────────────────┐
│             الشبكة الداخلية (RFC1918)                  │
│                                                         │
│   ┌───────────────┐         ┌──────────────┐          │
│   │ Helen-Server  │◄───────►│   Clients    │          │
│   │  (TCP 3000)   │  WebRTC │ (any device) │          │
│   │  (TCP 3443)   │         └──────────────┘          │
│   │  (UDP 41234)  │                                    │
│   │  (mDNS 5353)  │         ┌──────────────┐          │
│   └───────┬───────┘         │ Helen-Mobile │          │
│           │                 │   (APK)      │          │
│           │                 └──────────────┘          │
│           │                                            │
│           ▼                                            │
│   ┌───────────────┐                                   │
│   │Helen-Rendezvous│ ◄── يربط subnets                  │
│   │  (TCP 9090)    │     داخلية متعددة                 │
│   │  (TCP 9101/02) │                                   │
│   └────────────────┘                                   │
│                                                         │
└─────────────────────────────────────────────────────────┘
```

**خيارات السيرفر:**
- جهاز Linux واحد (الأفضل — ELF binary أو Docker)
- جهاز Windows (`.exe` installer)
- VM داخلي على Hyper-V / Proxmox / ESXi
- NAS (Synology / QNAP / TrueNAS عبر Docker)

---

## 🚀 السيناريو 1: شبكة LAN واحدة (الأبسط)

### الخطوة 1: تثبيت السيرفر

#### على Windows
```
1. شغّل: Helen-Server-Setup-1.0.0.exe (كـ Administrator)
2. اختر components:
   ☑ Helen-Server (required)
   ☑ Install as Windows service
   ☑ Add Windows Firewall rules
3. التثبيت ينشئ:
   - C:\Program Files\Helen-Server\
   - Service "HelenServer" تبدأ تلقائياً
   - Firewall rules لـ TCP 3000/3443 + UDP 41234 + mDNS
   - JWT_SECRET عشوائي
```

#### على Linux (أي توزيعة)
```bash
sudo bash deploy/linux/scripts/install-server.sh helen-server-linux-1.0.0.tar.gz
```

السكربت يعمل تلقائياً:
- إنشاء user `helen` (system، بدون login)
- نشر إلى `/opt/helen-server`
- توليد JWT_SECRET عشوائي
- تثبيت systemd unit
- تكوين firewall (ufw / firewalld / iptables)
- enable + start

#### على Linux عبر Docker
```bash
docker load -i helen-server-1.0.0.docker.tar
docker run -d \
  --name helen \
  --restart=unless-stopped \
  -p 3000:3000 -p 3443:3443 -p 41234:41234/udp \
  -e JWT_SECRET="$(openssl rand -hex 32)" \
  -v helen-data:/app/data \
  helen-server:1.0.0
```

#### على macOS
```bash
tar xzf helen-server-macos-1.0.0.tar.gz
cd helen-server-macos-1.0.0
./install.sh
./install-service.sh   # launchd auto-start
```

### الخطوة 2: التحقق
```bash
# على السيرفر:
curl http://localhost:3000/api/health
# → {"status":"ok","service":"Helen Server","version":"1.0.0"}

# health check شامل:
bash deploy/linux/scripts/health-check.sh
# (أو على Windows: deploy/linux/scripts/health-check.ps1)
```

### الخطوة 3: تثبيت العملاء

#### Windows / Linux Desktop
```
نشر "Helen Desktop Setup 1.0.0.exe" (Win) أو AppImage (Linux)
عند أول تشغيل، التطبيق يكتشف السيرفر تلقائياً عبر:
- mDNS (_helen-server._tcp.local)
- UDP broadcast على 41234
أو يمكنك إدخال IP يدوياً.
```

#### Android
```
adb install Helen-Mobile-1.0.0-release.apk
# أو انقل APK للهاتف وثبّته
```

#### iPhone / iPad / Mac (عبر PWA)
```
1. على السيرفر، شغّل (إذا لم يكن مدمجاً):
   cd CommClient-Web/dist && python3 -m http.server 8080

2. على iPhone:
   Safari → http://<server-ip>:8080
   → زر Share → "Add to Home Screen"

3. على Mac:
   Safari → http://<server-ip>:8080
   → File → Add to Dock
```

---

## 🌐 السيناريو 2: شبكات متعددة (Rendezvous)

عندك أكثر من LAN داخلي (فروع، طوابق، VLANs)؟ ضيف Rendezvous لـ NAT traversal بين الـ subnets.

### الخطوة 1: تثبيت Rendezvous

اختر جهازاً واحداً يكون مرئياً من جميع الـ subnets الداخلية (مثلاً جهاز في DMZ داخلي أو على الـ trunk).

#### Windows
```
شغّل Helen-Rendezvous-Setup-1.0.0.exe (Admin)
- ينشئ Service "HelenRendezvous"
- Firewall rules لـ TCP 9090/9101/9102 (RFC1918 فقط)
- HELEN_RENDEZVOUS_TOKEN عشوائي
```

#### Linux
```bash
sudo bash deploy/linux/scripts/install-rendezvous.sh helen-rendezvous-linux-1.0.2.tar.gz
```

### الخطوة 2: ربط Helen-Server بـ Rendezvous

في `.env` لكل Helen-Server:
```ini
HELEN_RENDEZVOUS_URL=http://<rendezvous-ip>:9090
HELEN_RENDEZVOUS_TOKEN=<نفس-التوكن-من-rendezvous>
```

أعد تشغيل السيرفر. سيسجل نفسه تلقائياً عبر `/tunnel/register`.

---

## 🔐 الأمان

### Self-signing (مرة واحدة لكل شبكة)
```powershell
# على جهاز إداري Windows واحد:
.\tools\self-sign-helen.ps1 -ImportToTrustedRoot $true

# الـ cert يصبح موقّع. صدّره ووزّعه على باقي الأجهزة:
Export-Certificate -Cert (Get-ChildItem Cert:\CurrentUser\My -CodeSigningCert | ?{$_.Subject -like "*Helen*"} | Select -First 1) -FilePath helen.cer
```

ثم على أجهزة الشبكة الأخرى:
```powershell
Import-Certificate -FilePath helen.cer -CertStoreLocation Cert:\LocalMachine\Root
Import-Certificate -FilePath helen.cer -CertStoreLocation Cert:\LocalMachine\TrustedPublisher
```

أو via Group Policy للنشر التلقائي.

### Vault للأسرار الحساسة
- `/vault/` يقدّمه السيرفر تلقائياً
- يرفض **HTTP 403** أي IP خارج RFC1918 + loopback
- master code يُولَّد عند أول boot في `data/vault_master_code.txt`

### Firewall ضيق
كل installers تضيف قواعد تقصر الوصول على RFC1918:
- TCP 3000/3443/9090/9101/9102 → فقط من 10.0.0.0/8 + 172.16/12 + 192.168/16
- UDP 41234/5353 → نفس القيود

---

## 📦 الصيانة

### Backup يومي (cron)
```bash
# /etc/cron.daily/helen-backup:
#!/bin/bash
/path/to/deploy/linux/scripts/backup.sh /var/backups/helen

# Windows (Task Scheduler):
# Daily 02:00 → powershell -File backup.ps1 -OutDir D:\backups\helen
```

### Restore
```bash
sudo bash deploy/linux/scripts/restore.sh /var/backups/helen/helen-backup-20260504-020000.tar.gz
```

### Health monitoring
```bash
# شغّله كل 5 دقائق عبر cron / monitoring system
bash deploy/linux/scripts/health-check.sh
# Exit code 0 = صحي، non-zero = هناك failures
```

### Logs
```bash
# Linux
journalctl -u helen-server -f
journalctl -u helen-rendezvous -f

# Windows
Get-EventLog -LogName Application -Source HelenServer -Newest 50
# أو في:
C:\Program Files\Helen-Server\_internal\data\service.out.log
```

---

## 🔄 Updates

عند صدور إصدار جديد:

### Linux
```bash
sudo systemctl stop helen-server
sudo bash deploy/linux/scripts/install-server.sh helen-server-linux-1.0.X.tar.gz
# .env و data/ محفوظان تلقائياً
```

### Windows
```
شغّل installer الجديد — يلغي تثبيت القديم ويحافظ على .env و data/.
```

---

## 🆘 استكشاف الأخطاء

| المشكلة | الحل |
|---|---|
| العميل لا يكتشف السيرفر | تأكد UDP 41234 مفتوح، شغّل `avahi-browse _helen-server._tcp` |
| `Insecure JWT_SECRET` | عدّل `.env` → `JWT_SECRET=$(openssl rand -hex 32)` |
| 403 على `/vault/` | الـ IP خارج RFC1918، استخدم VPN أو host داخلي |
| Service لا يبدأ على Windows | `sc qc HelenServer` و `nssm get HelenServer Application` |
| mDNS فشل على Windows | افتح Windows Defender Firewall لـ Bonjour/UDP 5353 |
| Battery drain على Android | تأكد `CallForegroundService` نشط (موصى للمكالمات الطويلة) |

---

## 📚 الملفات المرجعية

- `DELIVERY-MANIFEST.md` — قائمة كل المنتجات المبنية
- `SECURITY-ARCHITECTURE.md` — المعمارية الأمنية
- `THREAT-MODEL.md` — تحليل التهديدات
- `RUNBOOKS.md` — operations playbooks
- `API_REFERENCE.md` — كل الـ 194 endpoint

---

## ✅ القائمة النهائية للنشر

- [ ] Helen-Server مثبّت ويرد على `/api/health`
- [ ] JWT_SECRET قوي (32+ char)
- [ ] Firewall مكوّن (RFC1918 only)
- [ ] Service مفعّل ويبدأ تلقائياً
- [ ] Helen-Rendezvous (إن لزم — للشبكات المتعددة)
- [ ] Backup مجدوَل يومياً
- [ ] Health check مجدوَل كل 5 دقائق
- [ ] Self-signed cert موزّع على Trusted Root
- [ ] العملاء (Desktop + Mobile + PWA) يكتشفون السيرفر
- [ ] Vault accessible عبر `/vault/`
- [ ] Admin panel accessible عبر `/admin/` بـ JWT أو X-Secret-Admin-Token

**عند إكمال القائمة: المشروع 100% LIVE داخل شبكتك.**
