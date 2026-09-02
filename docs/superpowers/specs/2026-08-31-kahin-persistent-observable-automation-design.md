# Kahin Persistent Observable Automation Design

## Goal

Kahin'in mevcut Mirage/Juggler yüzeyini geriletmeden, tekrar giriş gerektirmeyen kalıcı browser oturumu, gerçek zamanlı crawl ilerlemesi, kullanılabilir veri görselleştirmesi ve desteklenen WebExtension yükleme akışını üretim sözleşmesi olarak tamamlamak.

## Scope and boundaries

- Varsayılan Mirage profili kalıcı olacak; kullanıcı ayrıca bir ayar açmadan cookie, web storage ve tarayıcının kendi profil verileri yeniden başlatma sonrasında korunacak.
- İzole `Browser.createBrowserContext` context'leri açıkça oluşturulan geçici context'ler olarak kalacak; varsayılan context kalıcı profilin context'i olacak.
- Crawl tek Mirage engine slotunu ve mevcut tab sözleşmesini koruyacak. Paralel sayfa navigasyonu ile aynı tabı yarıştırmak yerine, ilerleme olayları monotonic cursor ve bounded long-poll ile okunacak.
- Görselleştirme harici servis veya sahte veri kullanmayacak; verilen bounded satırlardan deterministik bar/line/scatter/pie SVG ve makinece okunabilir özet üretecek.
- WebExtension yolu, gerçek manifest ve gerçek addon path'i ile çalışacak. Desteklenmeyen Chrome-only yetenekleri sessizce taklit etmek yerine yapılandırılmış uyumsuzluk raporu verecek.
- CAPTCHA, access-denied veya rate-limit bypass edilmeyecek. CAPTCHA için mevcut explicit pause/resume, rate-limit için Retry-After/backoff sözleşmesi korunacak.

## Architecture

### 1. Persistent Mirage profile

`Mirage.start` her seferinde rastgele geçici dizin üretmek yerine varsayılan olarak `~/.local/share/kahin/profile` altında tek bir profile directory kullanır. `KAHIN_PROFILE_DIR` ile açık bir mutlak yol seçilebilir; `persistent_profile=False` ile yalnızca açıkça istenen test/geçici çalışmada mevcut temporary-profile davranışı korunur. Browser lock zaten process'ler arası tek sahibi garanti ettiği için profile paylaşımı yarışa açılmaz. `stop()` kalıcı profile dokunmaz; geçici profil silinir.

Her launch'ta Camoufox `launch_options()` yine çağrılır, `user.js` yeniden yazılır ve identity fingerprint politikası korunur. Cookie/web storage kalıcılığı profilin native Firefox verisiyle sağlanır; mevcut manuel `kahin_mirage_state_save/load` geriye dönük olarak kalır.

### 2. Crawl event ledger

`_CrawlJob` bounded bir event deque, monotonic event index ve wake event taşır. State transition, URL dequeue, success/failure, backoff, challenge, rotation ve terminal durumlar olay üretir. Yeni `kahin_crawl_events` aracı cursor ile delta döndürür ve bounded `waitMs` ile bir sonraki olayı bekleyebilir. Mevcut `kahin_crawl_status/results` değişmez; event stream yalnızca gözlemlenebilirliği tamamlar.

### 3. Deterministic visualization

Yeni saf `kahin/visualization.py` modülü rows/spec doğrulaması, numeric domain hesaplama, renk seçimi ve SVG üretimini üstlenir. `kahin_visualize_data` aracı bu modülü çağırır ve `{type, rows, fields, svg, summary}` döndürür. Satır, alan, string uzunluğu ve SVG payload sınırları zorunludur; veri kaynağı olarak yalnızca çağrıdaki rows kullanılır.

### 4. WebExtension preparation and launch

Yeni extension modülü gerçek bir directory/zip kaynağını sınırlar içinde okur, manifest'i doğrular ve Firefox'un desteklediği alanları çıkarılmış bir staged addon directory üretir. Chrome MV2/MV3 content scripts, permissions ve browser action metadata desteklenir; service worker/background page, native messaging, Chrome-only API veya bilinmeyen zorunlu alanlar yapılandırılmış `unsupported` sonucu üretir. `kahin_browser_start(addons=[...])` gerçek staged addon yollarını Camoufox `launch_options(addons=...)` içine geçirir. Kullanıcı scriptleri yalnızca gerçek content-script addon olarak yüklenir; gizli runtime injection yapılmaz.

## Error handling

- Profile yolu relative, root veya dosya ise structured `invalid_argument` döner; otomatik profile yolu oluşturulabilir.
- Native launch addon yükleyemezse browser start başarısızlığı addon path'i ve güvenli hata koduyla döner; sessiz fallback yoktur.
- Event cursor evicted bir aralığa işaret ederse `cursorReset=true` ve `oldestCursor` döner.
- Visualization invalid field/type/row için hangi alanın hatalı olduğunu ve sınırı döndürür.
- Hiçbir status, event veya visualization payload cookie değeri, proxy credential veya fingerprint raw config taşımaz.

## Verification

- Unit tests profile path/lifecycle policy, event cursor eviction/long-poll, visualization geometry/escaping/validation ve manifest compatibility için yazılır.
- Mevcut reliability/crawler/e2e suite korunur; real Camoufox testleri yalnızca runtime mevcutsa çalışır.
- Package docs, tools/list ve README yeni public araçların gerçek sözleşmesini yansıtır.
