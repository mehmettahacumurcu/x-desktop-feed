# X Desktop Feed

[English](README.md) | [Türkçe](README.tr.md)

X Desktop Feed, X gönderilerini kaydetmek, seçilen profillerden gönderi toplamak ve kullanıcının
**For You (Sana özel)** ile **Following (Takip edilenler)** akışlarının sınırlı anlık görüntülerini
saklamak için geliştirilmiş bir masaüstü uygulamasıdır. Gönderiler, oturum geçmişi, konu analizi
sonuçları ve indirilen fotoğraflar bilgisayarınızda tutulur.

Uygulama Python ve PySide6 ile geliştirilmiştir. Opera GX eklentisi tarayıcı bağlantısını,
SQLite kalıcı depolamayı, scikit-learn ise yerel konu analizini sağlar.

Bu depo `0.1.0` sürümünün kaynak kodunu içerir. Çalıştırmak için depoyu indirmeniz ve Python
ortamına kurmanız gerekir; hazır bir Windows kurulum paketi veya bağımsız çalıştırılabilir dosya
sunulmaz. Depolama ve analiz yereldir; X sayfalarına erişim, oEmbed ve çevrimiçi medya internet
bağlantısı gerektirir.

## Kurulum ve çalıştırma

Gereksinimler:

- Python 3.12 veya üzeri.
- Tarayıcı üzerinden toplama için Opera GX ve aşağıda anlatılan eklenti kurulumu.
- Eklenti testlerini çalıştırmak için Node.js.

Depoyu indirdikten sonra proje klasöründe PowerShell açın:

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e ".[dev]"
.\.venv\Scripts\python.exe -m xfeed
```

İlk kurulumdan sonra proje klasöründeki **Start X Desktop Feed.cmd** dosyasına çift tıklayarak
uygulamayı açabilirsiniz. Başlatıcı, projenin `.venv` ortamını kullanır ve ayrı bir konsol penceresi
açık bırakmaz. Sanal ortam yoksa kurulması gerektiğini belirten bir mesaj gösterir.

## Dil seçimi

Arayüz **Türkçe** ve **İngilizce** olarak kullanılabilir. Yan menüdeki dil listesinden seçim
yapabilirsiniz. Tercihiniz sonraki açılışlar için kaydedilir ve akış sayfalarına uygulanır.
Aşağıdaki açıklamalarda kontrolleri bulmayı kolaylaştırmak için İngilizce arayüz adları da verilmiştir.

## Opera GX bağlantısı

1. Masaüstü uygulamasını açın ve **Sources** ekranından **Connect Opera GX** seçeneğine tıklayın.
2. Opera GX içinde `opera://extensions` sayfasını açın ve geliştirici modunu etkinleştirin.
3. **Load unpacked / Paketlenmemiş öğe yükle** seçeneğiyle projenin `extension/opera-xfeed`
   klasörünü seçin. Bağlantı penceresinde bu klasörün tam yolu da gösterilir.
4. Masaüstündeki eşleştirme kodunu eklenti penceresine girip **Pair** seçeneğine tıklayın.
5. Uygulamanın Opera bağlantısını ve X oturumunu algıladığını doğrulayın. Giriş yapılmamışsa
   **Open X in Opera** ile X'i açın, hesabınıza kendiniz giriş yapın ve **Check connection** ile
   bağlantıyı kontrol edin.
6. **My Feed** üzerinden bir akışı veya **Sources** üzerinden bir profili toplayın.

**Uygulamayı güncellediğinizde eklentiyi de uzantılar sayfasından yeniden yükleyin.** Masaüstü ve
eklenti arasındaki iletişim protokolü sürüm 4'tür. Eski eklenti dosyaları yeni uygulamayla uyumsuz olabilir.

Toplama sırasında eklenti, kendisine ait X sekmesini kısa süreliğine öne getirir. İşlem bitene kadar
bu sekmeye müdahale etmeyin. **Disconnect Opera**, X hesabından çıkış yapmadan masaüstü eşleştirmesini
iptal eder. Normal bir eklenti yeniden yüklemesinden sonra eşleştirme korunur; eklentinin yerel
depolaması temizlenmişse yeniden eşleştirme gerekir.

## Akış toplama ve oturum geçmişi

**My Feed** ekranında For You ve Following için ayrı sekmeler bulunur. Her sekmenin gönderi sayısı,
otomatik toplama aralığı, işlem durumu ve geçmiş görünümü ayrıdır. `10`, `20`, `30` veya `50`
gönderi seçip **Collect now** ile toplama başlatabilirsiniz. Eklenti önce X üzerinde istenen
akış sekmesini seçip doğrular, ardından gönderileri çıkarır.

Her uygulama açılışında yeni bir oturum başlar. İki akışın güncel oturum okuyucusu başlangıçta
boştur; bu durum eski kayıtların silindiği anlamına gelmez. Uygulama açıkken toplanan gönderiler
ilgili sekmenin **This session** bölümünde görünür. **Show older posts** eski oturumları ekler;
**Jump to older sessions** bu bölüme geçiş sağlar. İki akışın geçmişi birbirine karıştırılmaz.

**Automatic** menüsünde kapalı durum ile 5, 10, 30 ve 60 dakikalık aralıklar bulunur. Otomatik
toplama yalnızca masaüstü uygulaması açıkken çalışır ve ilk işlemden önce seçilen sürenin dolmasını
bekler. Başka bir toplama sürüyorsa sıraya alınır. Toplama işleri ortak koordinatörle sırayla yürütülür.

## Profil kaynakları

**Sources** ekranına `@openai` gibi bir kullanıcı adı girip **Add profile** ile kaynak ekleyin.
Her profil için 5, 10, 20 veya 30 gönderi seçebilir ve **Collect** ile toplama başlatabilirsiniz.
**Collect all**, etkin profilleri ekrandaki sırayla işler; kendi genel gönderi sayısı ayarını kullanır.
Devre dışı bir profil listede kalır ancak toplu işlemde atlanır.

Profil kartındaki seçim kutusu yalnızca görüntülemeyi kontrol eder. Bir veya birden fazla profil
seçildiğinde kayıtları sağdaki okuyucuda, tekrarlar ayıklanarak yeniden eskiye gösterilir. Bu seçim,
profilin toplama için etkin olup olmadığını değiştirmez. İki alan arasındaki ayırıcı sürüklenebilir
ve düzen kaydedilir. **Cancel**, aktif işlemi durdurur ve sıradaki toplu toplama işlerini temizler.

## Manuel kayıt ve dışa aktarma

**Save URL**, bağlantıyla kayıt panelini açar. Desteklenen bir `x.com` veya `twitter.com` gönderi
bağlantısını yapıştırıp **Save** seçeneğine tıklayın. Gönderi bilgileri oEmbed üzerinden alınır.
Önceden kaydedilmiş bir adres ikinci bir gönderi oluşturmaz; manuel kayıt bilgisi korunur ve mevcut
kayıt gösterilir. Elle kaydedilen gönderiler **Saved** sekmesinden de okunabilir.

**Export** ekranında boş olmayan uygulama oturumlarını seçerek CSV veya JSON dosyası oluşturabilirsiniz.
Çıktı `url`, `author`, `text` ve `date` alanlarını içerir. Birden fazla seçili oturumda görülen aynı
gönderi tekilleştirilir. Toplanan gönderiler doğrudan kayıtlı oturum kimliğiyle, manuel kayıtlar ise
kayıt zamanlarının oturum aralığına denk gelmesiyle ilişkilendirilir. Bu çıktı tüm veritabanını ve
fotoğrafları kapsayan bir yedek değildir.

Dışa aktarma özgün gönderi metnini korur. Elektronik tablo yazılımları bazı CSV hücrelerini formül
olarak yorumlayabilir; güvenilmeyen içerik için JSON kullanın veya CSV sütunlarını açıkça metin
olarak içe aktarın. Çıktı dosyasının yazılması atomik değildir: yazma hatası eski dosyanın korunmasını
garanti etmez. Dosya uzantısıyla iletişim kutusunda seçilen formatın aynı olduğundan emin olun.
Bu sürümde veri içe aktarma ve eğitilmiş istenmeyen içerik sınıflandırması tamamlanmış kullanıcı
iş akışları olarak sunulmaz.

## Yerel kayıt konumu ve fotoğraflar

Windows üzerinde uygulamanın veri klasörü:

```text
%LOCALAPPDATA%\XDesktopFeed
```

Fotoğraflar bu klasörün altında `media/<post-id>/photo-<position>.<ext>` düzeninde saklanır.
Veritabanı, arayüz ayarları ve tarayıcı bağlantı verileri de uygulamanın yerel veri klasöründe tutulur.

Yeni sürüm ilk açıldığında, yeni klasör henüz yoksa eski konumdaki veriler önce geçici bir klasöre
kopyalanır, ardından yeni konum etkinleştirilir. Veritabanı, fotoğraflar, ayarlar ve eşleştirme bilgileri
birlikte korunur. Eski klasör yedek olarak bırakılır. Yeni konumda zaten veri varsa eski verilerle
birleştirilmez veya üzerine yazılmaz. Güncellemeden önce eski uygulama örneklerini kapatın. Geçiş
başarısız olursa uygulama boş bir kütüphane açmak yerine hata göstererek başlangıcı durdurur.

Uygulama, My Feed, Sources veya manuel kayıt yoluyla kütüphaneye yeni giren gönderilerin statik
fotoğraflarını otomatik indirir. Fotoğraf sırası korunur; görüntülerin uzun kenarı en fazla 1600 piksel
olacak şekilde boyutlandırılır. En az bir yerel fotoğraf hazır olduğunda okuyucu yerel galeriyi
tercih eder ve aynı medyanın çevrimiçi gösterimini yinelenmesini önlemek için kaldırır.

**Saving photos…**, gönderinin kaydedildiğini ancak fotoğraf işleminin sürdüğünü belirtir. Fotoğraf
indirme hatası gönderiyi silmez. Yerel fotoğraf yoksa çevrimiçi gösterim yedek seçenek olarak kullanılır.
Uygulama yeniden açıldığında yarım kalan fotoğraf çözümleme ve indirme işleri sürdürülür.

Bu özellik geçmişte kaydedilmiş tüm gönderileri tarayıp eksik fotoğraflarını tamamlamaz. Video ve
hareketli GIF indirme, tam çözünürlüklü orijinalleri saklama ve fotoğraf temizleme arayüzü sunulmaz.

## Analiz ekranı

**Insights**, yerel kayıtları incelemek için kullanılır. **Dataset** filtresi güncel oturumu,
geçmişteki dolu bir akış oturumunu, tüm akış oturumlarını veya tüm kayıtlı gönderileri seçer.
Tüm kayıtlı gönderiler kapsamına Sources ve manuel kayıtlar da girer. **Feed** filtresi Combined,
For You veya Following seçimini yapar; tek akış seçildiğinde yalnızca diğer kaynaklarda görülen
gönderiler kapsam dışı kalır.

| Gösterge | Anlamı |
|---|---|
| Unique posts | Seçilen veri kümesindeki benzersiz gönderiler |
| Appearances | Toplanan akışlarda görülme sayısı; aynı gönderi birden fazla sayılabilir |
| Unique authors | Farklı yazarların sayısı |
| With media | Medyası bilinen veya yerel medyası bulunan benzersiz gönderilerin oranı |
| New posts / Already saved | Tek oturumun toplama işlemlerindeki yeni ve önceden kayıtlı gönderiler |

Diğer bölümler konuları, gönderi türlerini, en sık görülen yazarları, akış dağılımını ve oturumlar
arasındaki eğilimleri gösterir. Konu analizi, kayıtlı metinler üzerinden tamamen bilgisayarınızda
çalışır. En az 10 kullanılabilir metin gerekir; yeterli veri yoksa bu durum açıklanır. **Reanalyze**,
metin kümesi değişmemiş olsa bile analizi yeniden çalıştırır.

## Bağlantı sorunları

- **Uygulama kapalı:** Eşleştirme veya toplamadan önce masaüstü uygulamasını açın.
- **Eklenti devre dışı:** `opera://extensions` üzerinden etkinleştirin ve bağlantıyı kontrol edin.
- **X oturumu kapalı:** **Open X in Opera** ile giriş yapıp **Check connection** seçeneğini kullanın.
- **47831 portu kullanımda:** Bu portu kullanan diğer işlemi kapatıp uygulamayı yeniden başlatın.
  Yerel köprü yalnızca `127.0.0.1:47831` adresini dinler.
- **Eklenti bağlı, uygulama bağlantısız görünüyor:** Eklentiyi yeniden yükleyin. Bağlantı, eklentinin
  düzenli yenilediği 60 saniyelik bir canlılık süresiyle izlenir. Sorun sürerse masaüstü uygulamasını
  yeniden başlatın ve yalnızca bir örneğinin çalıştığından emin olun.
- **Doğrulama ekranı veya hız sınırı:** Doğrulamayı Opera GX içinde kendiniz tamamlayın ya da sınırın
  kalkmasını bekleyin. Uygulama bu engelleri aşmaya çalışmaz.

## Toplama sınırları

- X parolanız uygulama tarafından okunmaz veya saklanmaz. Giriş işlemini Opera GX içinde siz yaparsınız.
- Profil toplama, seçilen hesabın özgün gönderileriyle sınırlıdır. Sabitlenmiş gönderiler, yanıtlar,
  yeniden paylaşımlar, alıntılar, başka yazarlara ait kartlar ve geçersiz bağlantılar elenir.
- Home akışları seçilen 10, 20, 30 veya 50 öğeyle sınırlıdır. Reklamlar ve gönderi olmayan modüller
  elenir; desteklenen yanıtlar kabul edilir. Alıntı ve yanıt üst gönderisi tespiti temkinli yapılır.
- Bir gönderinin kütüphanede bulunması, sonraki toplama işleminde gözlem olarak kaydedilmesini engellemez.
  Sonuç üretmeyen başarısız işlem önceki kullanılabilir akış görüntüsünün yerine geçmez.
- X sayfa yapısını değiştirebilir. Giriş gereksinimi, hız sınırı, zaman aşımı veya ilerleme olmaması
  durumunda işlem açıklayıcı bir durumla sonlandırılır.
- CAPTCHA veya giriş engeli aşma, otomatik hesap girişi ve özel API erişimi uygulanmaz.

## Testler ve doğrulama

Otomatik testler deterministik sahte toplayıcılar ve sağlayıcılar kullanır; canlı X'e bağlanmaz.
Proje kontrollerini tek komutla çalıştırabilirsiniz:

```powershell
.\Verify.ps1
```

Kontrolleri ayrı ayrı çalıştırmak için:

```powershell
.\.venv\Scripts\python.exe -m pytest
node --test extension/opera-xfeed/tests/*.test.mjs
.\.venv\Scripts\python.exe -m ruff check .
.\.venv\Scripts\python.exe -m ruff format --check .
.\.venv\Scripts\python.exe -m mypy src
git diff --check
```

İsteğe bağlı canlı kontrol için uygulamayı açıp güncel eklentiyi yeniden yükleyin. Opera bağlantısını
doğrulayın; 10 For You ve 10 Following gönderisi toplayıp ayrı sekmelerde göründüklerini kontrol edin.
Insights sayılarını akışlarla karşılaştırın, bir toplama işlemini iptal edin ve profil toplamasını
deneyin. Fotoğraflı yeni bir gönderinin yerel galeride açıldığını kontrol edin. Uygulamayı yeniden
açarak eski oturumların korunduğunu ve yeni oturum okuyucularının boş başladığını doğrulayın.

Canlı kontrol kullanıcı etkileşimi gerektirir ve otomatik testlerin parçası değildir. X bir doğrulama
ekranı, hız sınırı veya değişmiş sayfa hatası gösterirse görünen açıklamayı kaydedip işlemi durdurun.
