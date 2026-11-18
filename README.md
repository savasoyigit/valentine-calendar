# Aşkımız · yigit-calendar 💕

İki kişilik, kendi sunucunda çalışan ortak takvim. Buluşma planlarını öner, karşı taraf tek tıkla kabul etsin ya da başka gün önersin. Bunun yanında:

- 📅 **Ortak takvim:** Öneri, kabul ya da başka gün önerme akışı ve e-posta bildirimleri.
- 📝 **Notlar:** Güne not bırakma ve ortak notlar.
- 🛒 **Listeler:** Alışveriş listeleri ve dilek listesi.
- 🎬 **İzleme listesi:** Birlikte izlenecek film ve diziler.
- 🎉 **Özel günler ve anılar:** Anılara isteğe bağlı olarak [Immich](https://immich.app) fotoğrafları eklenebilir.
- 📸 **Anlık fotoğraf:** Immich üzerinden birbirinize anlık fotoğraf gönderme.
- 🔔 **Bildirimler:** Telefona eklenebilen web uygulaması (PWA) ve anlık bildirimler.
- ⏳ **Sayaç:** “Birlikte geçen zamanımız” sayacı.

> Bu README Türkçedir. **English:** run `pip install -r requirements.txt && uvicorn app:app`, open the app in your browser and follow the first‑run setup wizard. The setup code is printed in the server console.

---

## Hızlı kurulum

Gerekenler: **Python 3.10 veya üzeri**.

```bash
git clone https://github.com/savasoyigit/yigit-calendar.git
cd yigit-calendar
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
uvicorn app:app --host 0.0.0.0 --port 8000
```

Tarayıcıda `http://SUNUCU_IP:8000` adresini aç. **İlk kurulum sihirbazı** seni karşılayacak.

### İlk açılış sihirbazı

Uygulama ilk kez başlatıldığında terminale şöyle bir kurulum kodu yazar:

```
============================================================
  İLK KURULUM GEREKİYOR
  Tarayıcıda uygulamayı aç ve bu kurulum kodunu gir:

      82A1-9415-B0A3
============================================================
```

Bu kod, sunucun internete açıksa başkasının kurulumu senden önce yapmasını engeller. Kodu terminalde kaçırırsan `data/.setup-token` dosyasında da bulabilirsin.

Sihirbaz sırasıyla şunları sorar:

| Adım | Ne soruluyor | Zorunlu mu? |
|---|---|---|
| 1. Temel bilgiler | Uygulamanın adresi, birlikteliğin başladığı gün | Adres zorunlu |
| 2–3. Kişiler | Ad (giriş adı da bu olur), e-posta, şifre, profil fotoğrafı | Ad ve şifre zorunlu |
| 4. Immich | Immich adresi, API anahtarı, albüm adları. Bağlantıyı test etme butonu var | İsteğe bağlı |
| 5. E-posta | SMTP sunucusu, kullanıcı adı ve şifre. Test e-postası gönderme butonu var | İsteğe bağlı |

Şunları sihirbaz **kendisi üretir**, senin bir şey yapman gerekmez:
- Oturum imzalama anahtarı (`SITE_SECRET`)
- Anlık bildirim anahtarları (VAPID)
- Şifre özetleri (PBKDF2-SHA256, 310.000 tur)

“Kurulumu tamamla”ya bastığında ayarlar `data/config.json` dosyasına kaydedilir. Dosyayı yalnızca sunucu kullanıcısı okuyabilir (izinleri `600`). Uygulama **yeniden başlatmaya gerek kalmadan** açılır.

---

## Nereden ne alınır?

**Immich API anahtarı**
Immich’i aç ve şu yolu izle: Profil resmi → **Hesap Ayarları** → **API Anahtarları** → **Yeni API Anahtarı**.

**Gmail ile e-posta göndermek**
Normal Gmail şifren burada çalışmaz. Önce Google hesabında 2 adımlı doğrulamayı aç. Sonra [uygulama şifresi](https://myaccount.google.com/apppasswords) oluştur ve sihirbaza o şifreyi yaz. Sunucu: `smtp.gmail.com`, port: `587`.

**Anlık bildirimler ve telefona kurulum**
Tarayıcılar bu iki özellik için **https** ister. Uygulamayı Caddy, Nginx ya da Cloudflare Tunnel gibi bir ters vekil (reverse proxy) arkasında https ile yayınlaman önerilir. En kısa örnek Caddy ile:

```
takvim.ornek.com {
    reverse_proxy 127.0.0.1:8000
}
```

---

## Sürekli çalıştırma (systemd)

`/etc/systemd/system/yigit-calendar.service`:

```ini
[Unit]
Description=Aşkımız takvim
After=network.target

[Service]
User=KULLANICI
WorkingDirectory=/home/KULLANICI/yigit-calendar
ExecStart=/home/KULLANICI/yigit-calendar/.venv/bin/uvicorn app:app --host 127.0.0.1 --port 8000
Restart=on-failure

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now yigit-calendar
journalctl -u yigit-calendar -f    # ilk açılıştaki kurulum kodu burada görünür
```

---

## Ayarları değiştirme

- **Tek bir ayarı değiştirmek için:** `data/config.json` dosyasını düzenle ve servisi yeniden başlat. Bunlar Immich anahtarı, SMTP şifresi, adres gibi ayarlar.
- **Kurulumu baştan yapmak için:** `data/config.json` dosyasını sil ve servisi yeniden başlat. Sihirbaz tekrar açılır.
  - Veritabanı (`data/askimiz.db`) silinmez, takvim ve notlar yerinde kalır.
  - Yeni bir oturum anahtarı üretildiği için herkes bir kez yeniden giriş yapar.
  - Kişi adlarını değiştirirsen eski kayıtlarda eski adlar görünmeye devam eder.

### Sihirbaz yerine ortam değişkenleri

Docker veya otomatik kurulum gibi durumlarda ayarları ortam değişkenleriyle de verebilirsin:

```bash
cp .env.example .env      # doldur
uvicorn app:app --env-file .env
```

Ortam değişkenleri her zaman `data/config.json` dosyasından önceliklidir. Zorunlu değişkenler (`SITE_SECRET`, `PEOPLE`, `LOGIN_CREDENTIALS`) tanımlıysa sihirbaz hiç açılmaz. `LOGIN_CREDENTIALS` satırını üretmek için `python3 create_login_credentials.py` komutunu kullanabilirsin.

Anlık bildirim (VAPID) anahtarlarını elle üretmek için:

```bash
python3 -c "from setup_wizard import generate_vapid_keys as g; p, k = g(); print(f'VAPID_PUBLIC_KEY={p}\nVAPID_PRIVATE_KEY={k}')"
```

---

## Dosya yapısı

```
app.py                       Giriş noktası: ayarlar yoksa sihirbazı, varsa uygulamayı çalıştırır
core.py                      Asıl uygulama (FastAPI)
index.html                   Arayüz
setup_wizard.py, setup.html  İlk açılış kurulum sihirbazı
config_store.py              Ayarların okunması ve yazılması
create_login_credentials.py  .env ile kurulum için yardımcı betik
data/                        (git'e girmez) config.json, veritabanı, profil fotoğrafları
```

## Güvenlik notları

- **Gizli dosyalar:** `data/` ve `.env` asla commit edilmemeli, `.gitignore` bunları zaten dışarıda bırakıyor.
- **Kurulum ekranı:** Kurulum bitince kurulum kodu silinir ve sihirbazın uç noktaları tamamen kapanır.
- **Giriş denemeleri:** Aynı IP ya da kullanıcı adıyla 3 hatalı denemede giriş 30 dakika kilitlenir.

## Lisans

[MIT](LICENSE)
