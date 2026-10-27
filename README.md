# VFS Global Randevu Takip Botu (Çekya)

Bu bot VFS Global'e senin hesabınla belirli aralıklarla giriş yapar. Seçtiğin merkez, kategori ve alt kategori için uygun randevu olup olmadığına bakar. **Randevu açıldığında Telegram'dan mesaj atar.**

- Randevuyu senin yerine **almaz**; sadece haber verir. Mesaj gelince hemen girip randevuyu kendin alırsın.
- Giriş bilgilerin sadece kendi bilgisayarındaki `.env` dosyasında durur.
- VFS e-postana tek kullanımlık kod (OTP) gönderirse bot kodu **Outlook'tan kendisi okur**. Bunun için bir kez `python vfs_bot.py --outlook-login` çalıştırıp Outlook'a "oturumu açık tut" diyerek giriş yapman yeterli. Outlook okunamazsa bot kodu Telegram'dan ister.
- Cloudflare doğrulaması çıkarsa açık tarayıcı penceresinden bir kez tıklayıp geçebilirsin. Oturum `browser-profile/` klasöründe saklanır.

## 1. Telegram botu oluştur

1. Telegram'da **@BotFather**'a `/newbot` yaz ve adımları izle. Verdiği **token**'ı kopyala.
2. Yeni botuna herhangi bir mesaj yaz (örneğin "merhaba").
3. Aşağıdaki kurulumdan sonra `python vfs_bot.py --telegram-chat-id` komutunu çalıştır. Çıkan `TELEGRAM_CHAT_ID` değerini `.env` dosyasına yaz.

## 2. Kurulum

Python 3.10 veya üstü gerekir.

```bash
git clone <bu repo>
cd ping-releases
python -m venv .venv
# Windows: .venv\Scripts\activate    macOS/Linux: source .venv/bin/activate
pip install -r requirements.txt
python -m playwright install chromium
cp .env.example .env      # Windows: copy .env.example .env
```

`.env` dosyasını açıp e-postanı, şifreni, merkezleri, kategoriyi ve Telegram bilgilerini doldur.

> `VFS_CENTRES`, `VFS_CATEGORY` ve `VFS_SUBCATEGORY` alanlarına, sitedeki açılır listelerde gördüğün metnin bir parçasını yazman yeterli. Yanlış yazarsan bot hata mesajında listedeki seçeneklerin tamamını gösterir; oradan doğrusunu kopyalayabilirsin.

## 3. Çalıştır

```bash
python vfs_bot.py --telegram-test   # Telegram'a test mesajı gelmeli
python vfs_bot.py --once            # tek kontrol yap, sonucu gör
python vfs_bot.py                   # sürekli çalış
```

İlk çalıştırmada tarayıcı penceresi açılır. Captcha veya OTP çıkarsa geç. Sonraki kontrollerde oturum hatırlanır.

Bot ekrana ve `vfs_bot.log` dosyasına log yazar. Bir şeyler ters giderse `debug/` klasörüne ekran görüntüsü kaydeder. Üst üste 3 kez hata alırsa Telegram'dan haber verir.

### Bilgisayar açıkken arka planda çalıştırmak

- **Windows:** Bilgisayar her açıldığında başlaması için Görev Zamanlayıcı'da (Task Scheduler) "oturum açıldığında" tetiklenen bir görev oluştur. Program olarak `.venv\Scripts\python.exe`, argüman olarak `vfs_bot.py` gir. "Başlangıç yeri" alanına proje klasörünü yaz.
- **macOS / Linux:** `nohup python vfs_bot.py &` komutunu kullanabilir ya da botu `tmux` veya `screen` içinde çalıştırabilirsin.
- **Sunucu (VPS):** Ekran olmadığı için `xvfb-run python vfs_bot.py` ile çalıştır. Veri merkezi IP'leri Cloudflare'e daha çok takılır, bu yüzden ev bağlantısı genelde daha iyi sonuç verir.

## Önemli notlar

- **Sorgu aralığını çok düşürme.** VFS çok sık istek atan hesapları veya IP'leri saatlerce engelliyor. 5–10 dakika makul bir aralık. Bot engellendiğini anlarsa kendiliğinden 60 dakika bekler.
- Otomatik sorgu VFS'in kullanım koşullarına aykırı sayılabilir. Bu aracı kendi başvurun için ve makul sıklıkta kullan.
- VFS sitesinin yapısı zaman zaman değişiyor. Bot bir gün "seçenek bulunamadı" ya da "panel yüklenmedi" gibi hatalar vermeye başlarsa `debug/` klasöründeki ekran görüntülerine bak. `vfs_bot.py` dosyasının başındaki seçicilerin güncellenmesi gerekebilir.
