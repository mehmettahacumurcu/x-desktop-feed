"""Lightweight application localization.

The desktop app keeps a module-level language selector. ``tr`` looks up an
English source string in the catalog and returns the current-language
translation, falling back to the source when no translation exists. Calls may
pass ``str.format`` arguments so dynamic messages keep their placeholders.
"""

from __future__ import annotations

from typing import Final

LANGUAGES: Final = ("en", "tr")
_DEFAULT_LANGUAGE: Final = "en"

_current = _DEFAULT_LANGUAGE

_LANGUAGE_NAMES: Final = {"en": "English", "tr": "Türkçe"}

_CATALOG: Final[dict[str, str]] = {
    # Main window / navigation
    "Your focused reading space": "Odaklı okuma alanınız",
    "My Feed": "Akışım",
    "Sources": "Kaynaklar",
    "Insights": "İçgörüler",
    "Insights will arrive after the collection experience is settled.": (
        "İçgörüler, toplama deneyimi oturduktan sonra eklenecek."
    ),
    "For You": "Sana Özel",
    "Following": "Takip Edilenler",
    "Language": "Dil",
    # Shared widgets
    "Ready": "Hazır",
    "Connect": "Bağlan",
    "Disconnect": "Bağlantıyı kes",
    "Checking Opera connection…": "Opera bağlantısı kontrol ediliyor…",
    "Opera connected · X signed out": "Opera bağlı · X oturumu kapalı",
    "Connecting to Opera…": "Opera'ya bağlanılıyor…",
    "Opera connected · X signed in": "Opera bağlı · X oturumu açık",
    "Opera disconnected": "Opera bağlantısı kesik",
    "X feed channels": "X akış kanalları",
    # My Feed
    "Save URL": "URL Kaydet",
    "Switch X timelines, collect deliberately, and read each channel without mixing.": (
        "X akışlarını değiştirin, bilinçli toplayın ve her kanalı karıştırmadan okuyun."
    ),
    "Posts": "Gönderiler",
    "Automatic": "Otomatik",
    "Never": "Asla",
    "5 min": "5 dk",
    "10 min": "10 dk",
    "30 min": "30 dk",
    "60 min": "60 dk",
    "Collect {feed}": "{feed} Topla",
    "Ready to collect a fresh {feed} feed": "Taze bir {feed} akışı toplamaya hazır",
    "Show older posts": "Eski gönderileri göster",
    "Jump to older sessions": "Eski oturumlara git",
    "This session": "Bu oturum",
    "Older app sessions": "Eski uygulama oturumları",
    "Save a post by URL to keep it here.": ("Burada saklamak için URL ile bir gönderi kaydedin."),
    "Your fresh feed starts here": "Taze akışınız burada başlıyor",
    "Your Following feed starts here": "Takip ettiğiniz akış burada başlıyor",
    "Choose a post count and collect from your X {feed} timeline.": (
        "Gönderi sayısı seçin ve X {feed} zaman akışınızdan toplayın."
    ),
    "Automatic collection is off": "Otomatik toplama kapalı",
    "Automatic collection scheduled in {minutes} minutes": (
        "Otomatik toplama {minutes} dakika sonra planlandı"
    ),
    "Automatic {feed} collection queued": "{feed} otomatik toplaması kuyruğa alındı",
    "Next capture in {minutes}:{seconds:02d}": "Sonraki alım {minutes}:{seconds:02d} sonra",
    "collection cancelled": "toplama iptal edildi",
    "collection failed": "toplama başarısız",
    "collection complete": "toplama tamamlandı",
    "{feed} {outcome}": "{feed} {outcome}",
    # Sources
    "Build a small library of profiles and read their saved posts together.": (
        "Profillerden küçük bir arşiv oluşturun ve kaydedilen gönderileri birlikte okuyun."
    ),
    "@handle or X profile URL": "@kullanıcıadı veya X profil bağlantısı",
    "Add profile": "Profil ekle",
    "Collect all": "Tümünü topla",
    "Cancel": "İptal",
    "Each profile": "Her profil",
    "Choose profiles to inspect or collect them all": (
        "İncelemek için profiller seçin veya hepsini toplayın"
    ),
    "Saved posts": "Kaydedilen gönderiler",
    "Add your first profile": "İlk profilinizi ekleyin",
    "Use the field above to start a focused source library.": (
        "Odaklı bir kaynak arşivi başlatmak için yukarıdaki alanı kullanın."
    ),
    "Select one or more profiles": "Bir veya daha fazla profil seçin",
    "Checked profiles are combined here from newest to oldest.": (
        "İşaretli profiller burada yeniden eskiye birleştirilir."
    ),
    "Saved profile posts": "Kaydedilen profil gönderileri",
    "No saved posts for this selection": "Bu seçim için kayıtlı gönderi yok",
    "Collect posts from a selected profile to populate this reader.": (
        "Bu okuyucuyu doldurmak için seçili bir profilden gönderi toplayın."
    ),
    "Enabled": "Etkin",
    "Disabled": "Devre dışı",
    "Last collected {time}": "Son toplama: {time}",
    "No collection yet": "Henüz toplama yok",
    "Collect": "Topla",
    "Latest": "Son",
    "Disable": "Devre dışı bırak",
    "Enable": "Etkinleştir",
    "Remove": "Kaldır",
    # Insights
    "Current session": "Geçerli oturum",
    "Past session": "Geçmiş oturum",
    "All feed sessions": "Tüm akış oturumları",
    "All saved posts": "Tüm kaydedilen gönderiler",
    "Combined": "Birleşik",
    "A local audit of what your feeds contained.": ("Akışlarınızın içeriğinin yerel denetimi."),
    "Dataset": "Veri kümesi",
    "Session": "Oturum",
    "Feed": "Akış",
    "Topic analysis": "Konu analizi",
    "Reanalyze": "Yeniden analiz et",
    "Run local topic analysis again, even when saved posts have not changed": (
        "Kaydedilen gönderiler değişmese bile yerel konu analizini yeniden çalıştır"
    ),
    "No posts match these filters": "Bu filtrelerle eşleşen gönderi yok",
    "Unique posts": "Benzersiz gönderiler",
    "Appearances": "Görünümler",
    "Unique authors": "Benzersiz yazarlar",
    "With media": "Medyalı",
    "New posts": "Yeni gönderiler",
    "Already saved": "Zaten kayıtlı",
    "Primary topics": "Birincil konular",
    "Primary assignment per unique post": "Benzersiz gönderi başına birincil atama",
    "Post kinds": "Gönderi türleri",
    "Latest observation per unique post": "Benzersiz gönderi başına son gözlem",
    "Top authors": "En çok yazar",
    "Unique posts by author": "Yazara göre benzersiz gönderiler",
    "Feed balance": "Akış dengesi",
    "Where unique posts appeared": "Benzersiz gönderilerin göründüğü yerler",
    "No distribution data for this selection": "Bu seçim için dağılım verisi yok",
    "Session trend": "Oturum eğilimi",
    "Unique posts in violet; captured appearances in gray": (
        "Benzersiz gönderiler mor; yakalanan görünümler gri"
    ),
    "Posts per session trend": "Oturum başına gönderi eğilimi",
    "Session {session}: {posts} unique posts, {appearances} appearances": (
        "Oturum {session}: {posts} benzersiz gönderi, {appearances} görünüm"
    ),
    "Topics have not been analyzed yet": "Konular henüz analiz edilmedi",
    "Analyzing saved posts locally…": "Kaydedilen gönderiler yerel olarak analiz ediliyor…",
    "Not enough text for reliable topics": "Güvenilir konular için yeterli metin yok",
    "No diagnostic was provided": "Teşhis sağlanmadı",
    "Topic analysis failed: {detail}": "Konu analizi başarısız: {detail}",
    "Topics updated {time}": "Konular güncellendi: {time}",
    "recently": "yakın zamanda",
    "Topic analysis was interrupted": "Konu analizi kesintiye uğradı",
    "No completed analysis yet": "Henüz tamamlanmış analiz yok",
    "Last completed {time}": "Son tamamlanma: {time}",
    "No past feed sessions are available": "Mevcut geçmiş akış oturumu yok",
    "Insights could not be loaded": "İçgörüler yüklenemedi",
    # Export
    "Export": "Dışa Aktar",
    "Select collected sessions and download them as a dataset.": (
        "Toplanmış oturumları seçin ve bunları veri kümesi olarak indirin."
    ),
    "Select all": "Tümünü seç",
    "Select none": "Seçimi temizle",
    "Export…": "Dışa aktar…",
    "No collected sessions yet": "Henüz toplanmış oturum yok",
    "Collect a feed or save a post, then return here to export.": (
        "Bir akış toplayın veya gönderi kaydedin, sonra dışa aktarmak için buraya dönün."
    ),
    "Session {session}: {posts} posts": "Oturum {session}: {posts} gönderi",
    "Select at least one session": "En az bir oturum seçin",
    "Export dataset": "Veri kümesini dışa aktar",
    "Export failed: {detail}": "Dışa aktarma başarısız: {detail}",
    "Exported {count} posts to {name}": "{name} dosyasına {count} gönderi aktarıldı",
    # Manual saves
    "Manual Saves": "Manuel Kayıtlar",
    "Close": "Kapat",
    "Paste an X post URL": "Bir X gönderi bağlantısı yapıştırın",
    "Save": "Kaydet",
    "Saving…": "Kaydediliyor…",
    "No manual saves yet": "Henüz manuel kayıt yok",
    "Paste a post URL above to keep it in this separate collection.": (
        "Bu ayrı koleksiyonda tutmak için yukarıya bir gönderi bağlantısı yapıştırın."
    ),
    "The provider did not return a saved post": ("Sağlayıcı kaydedilmiş bir gönderi döndürmedi"),
    "Saved · photo lookup queued": "Kaydedildi · fotoğraf sorgusu sıraya alındı",
    "Already saved · photos already resolved": "Zaten kayıtlı · fotoğraflar çözümlendi",
    "Already saved · photo lookup failed": "Zaten kayıtlı · fotoğraf sorgusu başarısız",
    "Unknown error": "Bilinmeyen hata",
    "Could not save the post: {detail}. Please try again.": (
        "Gönderi kaydedilemedi: {detail}. Lütfen tekrar deneyin."
    ),
    "Saved": "Kaydedildi",
    # Opera connect dialog
    "Connect Opera GX": "Opera GX'e Bağlan",
    "Load the X Desktop Feed extension in Opera GX, then pair it with the code "
    "shown here. Sign in to X directly in Opera if needed.": (
        "Opera GX'te X Desktop Feed uzantısını yükleyin, ardından burada gösterilen "
        "kodla eşleştirin. Gerekirse Opera'da doğrudan X'e giriş yapın."
    ),
    "Copy extension path": "Uzantı yolunu kopyala",
    "The pairing code expires 2 minutes after it is generated.": (
        "Eşleştirme kodu oluşturulduktan 2 dakika sonra geçerliliğini yitirir."
    ),
    "The pairing code expires 2 minutes after it is generated. "
    "Enter it in the extension popup and select Pair.": (
        "Eşleştirme kodu oluşturulduktan 2 dakika sonra geçerliliğini yitirir. "
        "Uzantı açılır penceresine girin ve Eşleştir'i seçin."
    ),
    "Copy pairing code": "Eşleştirme kodunu kopyala",
    "Generate new code": "Yeni kod oluştur",
    "Open X in Opera": "X'i Opera'da Aç",
    "Check connection": "Bağlantıyı kontrol et",
    "Unpacked extension directory:": "Paketlenmemiş uzantı dizini:",
    "Pairing code:": "Eşleştirme kodu:",
    "Extension directory not found: {path}. Restore extension/opera-xfeed "
    "before loading it in Opera GX.": (
        "Uzantı dizini bulunamadı: {path}. Opera GX'e yüklemeden önce "
        "extension/opera-xfeed dizinini geri yükleyin."
    ),
    "Could not generate a pairing code: {error}": ("Eşleştirme kodu oluşturulamadı: {error}"),
    "Checking connection to the Opera extension...": (
        "Opera uzantısına bağlantı kontrol ediliyor..."
    ),
    "X opened in Opera. Complete sign-in there if needed.": (
        "X, Opera'da açıldı. Gerekirse orada girişi tamamlayın."
    ),
    "Opera connected / X signed in": "Opera bağlı / X oturumu açık",
    "Opera connected / X signed out. Open X in Opera, sign in, then check again.": (
        "Opera bağlı / X oturumu kapalı. X'i Opera'da açın, giriş yapın, sonra yeniden "
        "kontrol edin."
    ),
    "Opera disconnected. Load and pair the extension, then check again.": (
        "Opera bağlantısı kesik. Uzantıyı yükleyip eşleştirin, sonra yeniden kontrol edin."
    ),
    # X login dialog (Edge)
    "Sign in to X": "X'e Giriş Yap",
    "Sign in manually in the dedicated Edge window. Return here when X is ready, "
    "then check the login status.": (
        "Ayrılmış Edge penceresinde manuel olarak giriş yapın. X hazır olunca buraya "
        "dönün ve giriş durumunu kontrol edin."
    ),
    "Complete sign-in in the dedicated Edge window, then check again here.": (
        "Ayrılmış Edge penceresinde girişi tamamlayın, sonra burada yeniden kontrol edin."
    ),
    "Open X in Edge": "X'i Edge'de Aç",
    "Check login status": "Giriş durumunu kontrol et",
    "Checking the login status in the dedicated Edge window...": (
        "Ayrılmış Edge penceresinde giriş durumu kontrol ediliyor..."
    ),
    "X is not signed in in the dedicated Edge window. Finish sign-in there, then check again.": (
        "Ayrılmış Edge penceresinde X'e giriş yapılmamış. Orada girişi tamamlayın, "
        "sonra yeniden kontrol edin."
    ),
    "The dedicated Edge window is signed in to X.": (
        "Ayrılmış Edge penceresi X'e giriş yapılmış durumda."
    ),
    "Microsoft Edge is unavailable. Install or reopen Edge, then try again.": (
        "Microsoft Edge kullanılamıyor. Edge'i kurun veya yeniden açın, sonra tekrar deneyin."
    ),
    # Feed HTML
    "No saved posts yet": "Henüz kayıtlı gönderi yok",
    "Posts you collect will appear here.": "Topladığınız gönderiler burada görünecek.",
    "Nothing here yet": "Henüz bir şey yok",
    "Collected posts will appear here.": "Toplanan gönderiler burada görünecek.",
    "Unknown author": "Bilinmeyen yazar",
    "Unknown date": "Bilinmeyen tarih",
    "Saved source URL unavailable": "Kaydedilen kaynak bağlantısı kullanılamıyor",
    "Open the saved post on X": "Kaydedilen gönderiyi X'te aç",
    "Saved photo {position} from @{handle}": "@{handle} kullanıcısından kaydedilen fotoğraf {position}",
    "Preview photo": "Fotoğrafı önizle",
    "Close preview": "Önizlemeyi kapat",
    "Photo preview": "Fotoğraf önizlemesi",
    "Saving photos&hellip;": "Fotoğraflar kaydediliyor&hellip;",
    "{count} photo couldn&#x27;t be saved.": "{count} fotoğraf kaydedilemedi.",
    "{count} photos couldn&#x27;t be saved.": "{count} fotoğraf kaydedilemedi.",
    # Service result messages
    "Use a public https://x.com/.../status/... URL": (
        "Genel bir https://x.com/.../status/... bağlantısı kullanın"
    ),
    "The URL is not an X Post URL": "Bağlantı bir X gönderi bağlantısı değil",
    "Unsupported profile host": "Desteklenmeyen profil sunucusu",
    "Invalid X handle": "Geçersiz X kullanıcı adı",
    # Coordinator messages
    "Waiting for Opera/X connection": "Opera/X bağlantısı bekleniyor",
    "No enabled profiles to collect": "Toplanacak etkin profil yok",
    "Another collection is already running": "Başka bir toplama zaten çalışıyor",
    "Cancelling collection...": "Toplama iptal ediliyor...",
    "Cancelling photo lookup...": "Fotoğraf sorgusu iptal ediliyor...",
    "Collection cancelled": "Toplama iptal edildi",
    "Collection complete": "Toplama tamamlandı",
    "Opera connected, but X is not signed in": "Opera bağlı, ancak X oturumu açık değil",
    "Connection cancelled": "Bağlantı iptal edildi",
    "X is signed out; reconnect": "X oturumu kapalı; yeniden bağlanın",
    "Saved post is unavailable for photo ingestion": (
        "Fotoğraf alımı için kayıtlı gönderi kullanılamıyor"
    ),
    "Photo lookup complete": "Fotoğraf sorgusu tamamlandı",
    "Photo resolution failed": "Fotoğraf çözümlemesi başarısız",
    "Photo lookup cancelled": "Fotoğraf sorgusu iptal edildi",
    "Looking up saved-post photos": "Kaydedilen gönderilerin fotoğrafları aranıyor",
    "Collecting For You": "Sana Özel toplanıyor",
    "Collecting Following": "Takip Edilenler toplanıyor",
    "Collecting @{handle}": "@{handle} profili toplanıyor",
    "Collecting {label}": "{label} toplanıyor",
    " · {index} of {total}": " · {index} / {total}",
    "Automatic collection skipped because Opera/X is not signed in": (
        "Opera/X oturumu açık olmadığı için otomatik toplama atlandı"
    ),
    "Collection failed": "Toplama başarısız",
    "Collection is running in the dedicated Opera GX tab.": (
        "Toplama, ayrılmış Opera GX sekmesinde çalışıyor."
    ),
    "Collection is running in the dedicated Microsoft Edge window.": (
        "Toplama, ayrılmış Microsoft Edge penceresinde çalışıyor."
    ),
    "X Desktop Feed is already running": "X Desktop Feed zaten çalışıyor",
}


def current_language() -> str:
    """Return the active language code."""
    return _current


def language_name(code: str) -> str:
    """Return the human-readable display name for a language code."""
    return _LANGUAGE_NAMES.get(code, code)


def set_language(code: str) -> None:
    """Activate the given language code. Unknown codes fall back to English."""
    global _current
    if code not in LANGUAGES:
        raise ValueError(f"Unsupported language: {code}")
    _current = code


def tr(text: str, *args: object, **kwargs: object) -> str:
    """Translate an English source string to the active language.

    Arguments are forwarded to ``str.format`` when supplied. Untranslated
    strings return the original English text.
    """
    if not isinstance(text, str):
        return text
    if _current == "tr":
        rendered = _CATALOG.get(text, text)
    else:
        rendered = text
    if args or kwargs:
        try:
            return str(rendered).format(*args, **kwargs)
        except (KeyError, IndexError, ValueError):
            return str(rendered)
    return str(rendered)
