# Aşkımız 💕

**İki kişilik, kendi sunucunuzda çalışan ortak takvim.**

Buluşma planlarını öner, karşı taraf tek tıkla kabul etsin ya da başka gün önersin. Notlar, listeler, mekanlar, anılar ve mektuplar da aynı yerde.

<p align="center">
  <img src="docs/screenshots/setup.jpg" width="250" alt="Kurulum sihirbazı">
  <img src="docs/screenshots/home.jpg" width="250" alt="Ana sayfa">
  <img src="docs/screenshots/settings.jpg" width="250" alt="Ayarlar">
</p>

## Özellikler

- 📅 **Takvim:** Buluşma öner, kabul et ya da başka gün öner.
- 📝 **Notlar ve listeler:** Ortak notlar, alışveriş ve dilek listesi.
- 📍 **Mekanlar:** Gittiğiniz ve gidilecek yerler. İkiniz ayrı puan verirsiniz, bir de “🎲 Bugün nereye?” butonu var.
- 🎬 **İzlenecekler:** Birlikte izlenecek film ve diziler.
- ✉️ **Mektuplar:** Seçtiğin anda açılan zaman kapsülü mektuplar.
- 🎉 **Özel günler, anılar ve sayaç:** Yıldönümlerini kutlar. Anılara [Immich](https://immich.app) fotoğrafları eklenebilir.
- 🌙 **Kendi alanım:** Tek dokunuşla bildirimleri sessize al, karşı taraf görsün.
- 🔔 **Bildirimler:** Telefona uygulama gibi kurulur (PWA). Anlık bildirim ve e-posta gönderir.

> **English:** Self-hosted shared calendar for couples. Run `docker compose up -d`, open it in your browser and follow the setup wizard. The setup code is printed in the server log. The UI is in Turkish.

## Kurulum

**Docker ile:**

```bash
git clone https://github.com/savasoyigit/valentine-calendar.git
cd valentine-calendar
docker compose up -d
docker compose logs askimiz      # kurulum kodu burada
```

**Python ile** (3.10 veya üzeri):

```bash
git clone https://github.com/savasoyigit/valentine-calendar.git
cd valentine-calendar
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
uvicorn app:app --host 0.0.0.0 --port 8000
```

Tarayıcıda `http://SUNUCU_IP:8000` adresini aç. Konsolda yazan kurulum kodunu gir ve sihirbazı takip et. Sihirbaz şunları sorar:
- **Adres ve başlangıç tarihi:** Uygulamanın adresi ve birlikteliğin başladığı gün.
- **İki kişi:** Ad, e-posta, şifre ve profil fotoğrafı.
- **İsteğe bağlı:** Immich ve e-posta ayarları.

Kodu kaçırırsan `data/.setup-token` dosyasında bulabilirsin. Kurulumdan sonra tüm ayarlar uygulama içindeki **⚙️ Ayarlar** sayfasından değiştirilebilir.

## https

Anlık bildirim ve “Ana ekrana ekle” için https gerekir. En kolay yol [Tailscale](https://tailscale.com):

```bash
curl -fsSL https://tailscale.com/install.sh | sh
sudo tailscale up
sudo tailscale funnel --bg 8000      # internete açar
# ya da: sudo tailscale serve --bg 8000   (sadece Tailscale ağındaki cihazlar)
```

Çıkan `https://….ts.net` adresini **Ayarlar → Genel → Uygulama adresi** alanına yaz. Uygulamayı `--host 127.0.0.1 --proxy-headers` ile çalıştır.

Kendi alan adın varsa Caddy de olur:

```
takvim.ornek.com {
    reverse_proxy 127.0.0.1:8000
}
```

## Sürekli çalıştırma (systemd)

`/etc/systemd/system/askimiz.service`:

```ini
[Unit]
Description=Aşkımız takvim
After=network.target

[Service]
User=KULLANICI
WorkingDirectory=/home/KULLANICI/valentine-calendar
ExecStart=/home/KULLANICI/valentine-calendar/.venv/bin/uvicorn app:app --host 127.0.0.1 --port 8000 --proxy-headers
Restart=on-failure

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl enable --now askimiz
```

## Güncelleme ve yedek

```bash
git pull && docker compose up -d --build                              # Docker
git pull && pip install -r requirements.txt && sudo systemctl restart askimiz   # Python
```

Tüm veri `data/` klasöründe (Docker'da `askimiz-data` volume'ünde) durur. Yedek için o klasörü kopyalaman yeterli.

## .env ile kurulum

Sihirbaz yerine `.env.example` dosyasını `.env` olarak kopyalayıp doldurabilir ve `uvicorn app:app --env-file .env` ile başlatabilirsin. `.env`'deki ayarlar uygulamadan değiştirilemez.

Sonradan uygulamadan yönetmek istersen ayarları bir kez `data/config.json`'a taşı:

```bash
.venv/bin/python -c "
from dotenv import dotenv_values; import config_store
config_store.save_config({k: v for k, v in dotenv_values('.env').items() if k in config_store.KNOWN_KEYS and v})"
mv .env .env.eski
```

Ardından servisten `--env-file` satırını kaldırıp yeniden başlat.

## Geliştirme

```bash
pip install -r requirements-dev.txt
pytest -q
```

## Lisans

[MIT](LICENSE)
