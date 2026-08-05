# Kahin Juggler: AI-native kullanım kılavuzu

Bu belge, bir ajanın Camoufox/Mirage tarayıcısını kaynak kodu okumadan
kullanabilmesi için sözleşmeyi anlatır. Mirage, Chrome CDP'si değildir:
Kahin'in Zig sidecar'ı gerçek Juggler mesajlarını JSONL olarak taşır ve MCP
tool'ları bu yüzeyi ajana güvenli, yapılandırılmış bir biçimde sunar.

## 1. Zihinsel model

```text
MCP tool
  -> Kahin Mirage adapter
    -> Zig sidecar (Juggler JSONL)
      -> Camoufox Page/Browser
```

- `engine="mirage"` bir Camoufox süreci ve tek bir sidecar başlatır.
- İlk sayfa ilk sayfa tool'u çağrıldığında tembel olarak oluşturulur.
- Aynı browser'ı ve aktif sekmeyi yeniden kullan. Başka sayfa gerektiğinde
  yeni browser açmak yerine `kahin_mirage_tab_new` ve
  `kahin_mirage_tab_switch` kullan.
- Juggler'da `Browser.*` browser köküne, `Page.*` ise aktif target/session'a
  aittir. MCP araçları session routing'i ajandan saklar.
- `kahin_execute_cdp`, Mirage aktifken desteklenen CDP çağrılarını Juggler
  eşdeğerine yönlendirir; Juggler'da bulunmayan her CDP domain'i varmış gibi
  tahmin etme.

## 2. Ajanın temel protokolü

Dinamik bir sayfada güvenilir akış şöyledir:

```text
browser_start(mirage)
  -> navigate
  -> dom_start
  -> dom_snapshot
  -> [dom_events(after_seq, stream_id, wait_ms)]*
  -> dom_action(live_node_id)
  -> dom_snapshot veya evaluate ile doğrulama
```

Zorunlu kurallar:

1. Bilmediğin bir CDP methodunu göndermeden önce
   `kahin_validate_command` kullan. Hata sonrası
   `kahin_error_decode` ile düzeltmeyi öğren.
2. CSS selector'ı eylem kimliği olarak uzun süre saklama. Snapshot'taki
   `nodeId`, o document/frame içindeki canlı node'a bağlıdır.
3. `reset`, `dropped` veya `requiresSnapshot` görüldüğünde eski node/cursor
   bilgisine güvenme; yeni snapshot al.
4. `truncated=true` tam sayfa anlamına gelmez. Selector, `max_nodes` veya
   `max_depth` ile alanı daralt.
5. Bir input'a yazdıktan, tıkladıktan veya navigation yaptıktan sonra sonucu
   gözlemle. Eylem yanıtı tek başına uygulama başarısı değildir.

## 3. Gerçek zamanlı DOM vericisi

DOM stream sayfanın kendi `MutationObserver`'ını ve DOM event listener'larını
kullanır. Sayfa tarafında bounded bir ring buffer tutulur; sidecar yalnızca
değişiklik sinyali taşır. Böylece hızlı bir sayfa, sidecar reader'ını büyük
DOM payload'ları ile bloke etmez.

### 3.1 Tool sözleşmesi

| Tool | Amaç | Önemli parametreler |
|---|---|---|
| `kahin_mirage_dom_start` | Observer'ı mevcut ve sonraki document'lara kurar | `max_events`, `frame_id` |
| `kahin_mirage_dom_snapshot` | Sınırlandırılmış, anlamsal canlı ağaç döndürür | `selector`, `max_nodes`, `max_depth`, `include_hidden`, `text_limit`, `frame_id` |
| `kahin_mirage_dom_events` | Cursor sonrasındaki delta/event'leri okur | `after_seq`, `stream_id`, `limit`, `wait_ms`, `frame_id` |
| `kahin_mirage_dom_action` | Snapshot nodeId'si üzerinde allow-list eylemi yapar | `node_id`, `action`, `text`, `frame_id` |
| `kahin_mirage_dom_stop` | Mevcut frame observer'ını ve ring'i kapatır | `frame_id` |

`dom_start` sonucu ajanın saklaması gereken kimlik:

```json
{
  "status": "started",
  "stream": {
    "streamId": "mdd1-abc123",
    "cursor": 0,
    "revision": 0,
    "pending": 0,
    "active": true,
    "url": "https://example.test/app",
    "readyState": "complete"
  }
}
```

Snapshot sonucu, action için gereken `nodeId` ile birlikte anlamsal bilgi
verir:

```json
{
  "streamId": "mdd1-abc123",
  "cursor": 4,
  "revision": 4,
  "url": "https://example.test/app",
  "readyState": "complete",
  "nodeCount": 8,
  "truncated": false,
  "focused": null,
  "root": {
    "nodeId": "n1",
    "tag": "main",
    "role": "main",
    "name": "Account",
    "text": "Name Save",
    "visible": true,
    "rect": {"x": 0, "y": 0, "width": 800, "height": 180},
    "attributes": {"id": "account"},
    "actions": [],
    "children": [
      {
        "nodeId": "n4",
        "tag": "input",
        "role": "textbox",
        "name": "Name",
        "value": "",
        "actions": ["focus", "type"]
      }
    ]
  }
}
```

Snapshot alanlarının anlamı:

- `role`, `name`, `text`: ajanın selector tahmini yerine kullanıcıya görünen
  hedefi anlamasına yardım eder.
- `visible`, `rect`: görünürlük ve gerçek viewport geometrisidir.
- `attributes`: yalnızca güvenli tanımlayıcı/erişilebilirlik özniteliklerinin
  bounded alt kümesidir.
- `value`: input/textarea/select/contenteditable için gelir; password input
  değeri `[redacted]` olur.
- `actions`: node'un doğrudan desteklediği `click`, `focus`, `type`, `select`
  ipuçlarını gösterir. `select` bu ilk action yüzeyinde uygulanmaz; select
  için mevcut DOM/evaluate veya daha özel tool sözleşmesini kullan.
- `cursor`: o snapshot anındaki son sequence numarasıdır. Delta okumaya bu
  cursor'dan devam edilir.

### 3.2 Delta ve event akışı

```json
{
  "streamId": "mdd1-abc123",
  "cursor": 7,
  "revision": 7,
  "reset": false,
  "dropped": false,
  "pending": 2,
  "url": "https://example.test/app",
  "events": [
    {
      "seq": 6,
      "timestamp": 1780000000000,
      "type": "childList",
      "target": {"nodeId": "n1", "tag": "main"},
      "added": [{"nodeId": "n8", "tag": "button", "role": "button", "name": "Save"}],
      "removed": [],
      "addedCount": 1,
      "removedCount": 0
    },
    {
      "seq": 7,
      "timestamp": 1780000000010,
      "type": "event",
      "event": "input",
      "target": {"nodeId": "n4", "tag": "input", "role": "textbox", "value": "Ada"}
    }
  ]
}
```

`MutationObserver` kaynaklı `attributes`, `characterData` ve `childList`
event'lerine ek olarak `input`, `change`, `focusin`, `focusout` ve `click`
sayfa event'leri verilir. Bu event'ler neden-sonuç sinyalidir; tam güncel
durum için snapshot yetkilidir.

`dom_events` için önerilen çağrı:

```json
{
  "after_seq": 4,
  "stream_id": "mdd1-abc123",
  "limit": 100,
  "wait_ms": 30000
}
```

`wait_ms` en fazla 30 saniyeye clamp edilir. Yeni event gelirse veya timeout
olursa tool döner; ajan bunu kendi gözlem döngüsünde tekrar çağırabilir.

### 3.3 Cursor güvenliği

| Durum | Anlam | Ajanın yapacağı |
|---|---|---|
| `reset=false`, `dropped=false` | Cursor hâlâ geçerli | Event'leri işle, cursor'u ilerlet |
| `reset=true` | Farklı document/streamId ile okuma yapıldı | Snapshot al, yeni `streamId`/cursor sakla |
| `dropped=true` | Eski delta ring buffer'dan düştü veya stream değişti | Snapshot al; eski event'leri birleştirmeye çalışma |
| `truncated=true` | Snapshot cap'i ağacın tamamını içermedi | Selector/cap ile daha dar snapshot al |
| `requiresSnapshot=true` | Node silindi, navigation oldu veya action stale | Yeni snapshot al, yeni nodeId seç |

Navigation document'ı yeniler ve nodeId'leri geçersiz kılar. Her frame'in
document'ı kendi `streamId`'sine sahiptir; `frame_id` verilirse snapshot,
event ve action aynı frame context'inde çalışır.

### 3.4 Live action sözleşmesi

İzin verilen action'lar: `click`, `hover`, `focus`, `type`, `scroll`.

- `click` ve `hover`, node'un gerçek viewport koordinatını ölçer ve
  `Page.dispatchMouseEvent` gönderir.
- `type`, canlı node'u focus eder ve gerçek `Page.insertText` çağrısı yapar.
  `text` zorunludur.
- `focus`, canlı node'u focus eder.
- `scroll`, node'u görünür alana `scrollIntoView` ile getirir.
- Node bağlı değilse hiçbir replacement node'a fallback yapılmaz; yapılandırılmış
  `stale_node`/`requiresSnapshot` döner.

Örnek:

```json
{
  "node_id": "n4",
  "action": "type",
  "text": "Ada"
}
```

Başarılı action sonrası en az bir doğrulama yap:

```text
dom_snapshot(selector="#name")
veya
kahin_evaluate("document.querySelector('#name').value")
```

## 4. Frame ve sekme kullanımı

1. `kahin_mirage_tab_list` ile target'ları gör.
2. `kahin_mirage_tab_new` ile sayfa aç veya
   `kahin_mirage_tab_switch(target_id)` ile geç.
3. `kahin_mirage_frame_tree` ile frame hiyerarşisini al.
4. Frame içindeki DOM tool'larına `frame_id` ver.

Frame tool'ları `frame_id` için aynı aktif target'ın main-world execution
context'ini kullanır. Frame ayrıldığında context ve nodeId artık geçersizdir;
yeniden frame tree + snapshot gerekir.

## 5. Juggler gerçeği: hangi primitive ne yapıyor?

DOM stream sahte CDP `DOM.*` event'leri üretmez. Camoufox Juggler'da mevcut
olmayan bir domain'i taklit etmek yerine gerçek browser primitives birleştirilir:

| Primitive | Rol |
|---|---|
| `Browser.addBinding` | Sayfa ile sidecar arasında düşük hacimli notify köprüsü |
| `Browser.setInitScripts` | Yeni document/frame'lerde stream init script'ini çalıştırır |
| `Page.bindingCalled` | Python tarafını uyandıran sinyal; DOM payload'ı taşımaz |
| `Runtime.evaluate` | Snapshot, delta drain ve action state'i sayfadan ister |
| `MutationObserver` | Gerçek DOM mutation kayıtlarını üretir |
| `Page.dispatchMouseEvent` | Click/hover için gerçek Juggler input |
| `Page.insertText` | Focus edilmiş input'a gerçek text insertion |

Init script gelecekteki document'ları kapsar; mevcut document için Kahin
script'i ayrıca evaluate eder. Observer sayfa tarafında bounded olduğu için
`dom_events` cursor olmadan geçmişi sınırsız saklamaz.

## 6. Juggler tool kataloğu (A-Z)

Aşağıdaki liste Mirage'ın 77 Juggler-native tool'unun tamamıdır. `MIRAGE`
tool'ları `engine="mirage"` aktifken kullanılır.

### DOM gözlem ve adaptif action (5)

- `kahin_mirage_dom_start`
- `kahin_mirage_dom_snapshot`
- `kahin_mirage_dom_events`
- `kahin_mirage_dom_action`
- `kahin_mirage_dom_stop`

### DOM query/action (12)

- `kahin_mirage_query`, `kahin_mirage_query_all`
- `kahin_mirage_click`, `kahin_mirage_type`
- `kahin_mirage_get_text`, `kahin_mirage_get_attribute`
- `kahin_mirage_set_attribute`, `kahin_mirage_focus`
- `kahin_mirage_hover`, `kahin_mirage_get_html`
- `kahin_mirage_wait_selector`, `kahin_mirage_get_value`

### Input (7)

- `kahin_mirage_mouse_click`, `kahin_mirage_mouse_move`
- `kahin_mirage_mouse_down`, `kahin_mirage_mouse_up`
- `kahin_mirage_key_press`, `kahin_mirage_key_text`
- `kahin_mirage_scroll`

### PageEx (6)

- `kahin_mirage_reload`, `kahin_mirage_go_back`, `kahin_mirage_go_forward`
- `kahin_mirage_stop`, `kahin_mirage_frame_tree`, `kahin_mirage_page_content`

### Tab/session (6)

- `kahin_mirage_tab_new`, `kahin_mirage_tab_switch`, `kahin_mirage_tab_close`
- `kahin_mirage_tab_list`, `kahin_mirage_tab_bring_front`
- `kahin_mirage_context_new`

### Network/console (10)

- `kahin_mirage_network_requests`, `kahin_mirage_get_response_body`
- `kahin_mirage_intercept_requests`, `kahin_mirage_unintercept_requests`
- `kahin_mirage_network_continue`, `kahin_mirage_network_abort`
- `kahin_mirage_cache_disable`, `kahin_mirage_clear_cache`
- `kahin_mirage_console_log`, `kahin_mirage_errors_list`

### Storage (6)

- `kahin_mirage_cookie_get`, `kahin_mirage_cookie_set`, `kahin_mirage_cookie_clear`
- `kahin_mirage_storage_local_get`, `kahin_mirage_storage_local_set`
- `kahin_mirage_storage_session_get`

### Emulation (10)

- `kahin_mirage_set_user_agent`, `kahin_mirage_set_viewport`
- `kahin_mirage_set_device_scale_factor`, `kahin_mirage_set_media`
- `kahin_mirage_set_touch`, `kahin_mirage_set_color_scheme`
- `kahin_mirage_set_reduced_motion`, `kahin_mirage_set_locale`
- `kahin_mirage_set_timezone`, `kahin_mirage_set_geolocation`

### Dialog/download/worker/WebSocket (7)

- `kahin_mirage_dialog_list`, `kahin_mirage_dialog_accept`,
  `kahin_mirage_dialog_dismiss`
- `kahin_mirage_download_list`, `kahin_mirage_download_save`
- `kahin_mirage_worker_list`, `kahin_mirage_websocket_list`

### Upload (2)

- `kahin_mirage_set_file_chooser_intercept`
- `kahin_mirage_upload_files`

### Screencast (4)

- `kahin_mirage_screencast_start`, `kahin_mirage_screencast_frame`
- `kahin_mirage_screencast_stop`, `kahin_mirage_screencast_pending`

### Accessibility ve health (2)

- `kahin_mirage_accessibility_tree`
- `kahin_engine_health`

## 7. Hazır akışlar

### 7.1 Dinamik form doldurma

```text
1. kahin_browser_start(engine="mirage", headless=true)
2. kahin_navigate(url="https://example.test/account")
3. kahin_mirage_dom_start(max_events=512)
4. kahin_mirage_dom_snapshot()
5. textbox nodeId'si ile kahin_mirage_dom_action(action="type", text="Ada")
6. kahin_mirage_dom_snapshot(selector="#name") ile value doğrula
7. Save button nodeId'si ile dom_action(action="click")
8. kahin_mirage_dom_events(after_seq=..., stream_id=..., wait_ms=5000)
9. Sonuç panelini snapshot/evaluate ile doğrula
```

### 7.2 SPA navigation veya hydration bekleme

```text
dom_start
snapshot -> (streamId=S, cursor=C)
dom_events(after_seq=C, stream_id=S, wait_ms=30000)
  event geldiyse: ilgili subtree'yi yeniden snapshot et
  reset/dropped geldiyse: full snapshot al, S/C'yi yenile
  timeout olduysa: küçük bir snapshot veya uygulama-ready kontrolü yap
```

### 7.3 Iframe

```text
frame_tree -> frame_id=F
dom_snapshot(frame_id=F)
dom_action(node_id=N, action="click", frame_id=F)
dom_events(after_seq=C, stream_id=S, frame_id=F, wait_ms=5000)
```

## 8. Hata kurtarma matrisi

| Hata/sinyal | Sebep | Kurtarma |
|---|---|---|
| `Browser engine is not running` | Start yok | `kahin_browser_start(engine="mirage")` |
| `Browser engine is dead` | Sidecar/Camoufox öldü | `kahin_browser_stop`, sonra yeni `start` |
| `stale_node` | Node silindi veya document değişti | Snapshot + yeni nodeId |
| `reset`/`dropped` | Stream değişti veya ring overflow | Snapshot; eski cursor'u bırak |
| `not_found` | Selector artık yok | Event/snapshot ile yeni hedef bul |
| `truncated` | Snapshot cap'i küçük | Selector veya cap daralt/genişlet |
| `unsupported_action` | Action allow-list dışında | Yalnızca desteklenen action kullan |
| `not_text_input` | Hedef textbox değil | Role/name/action ipuçlarını tekrar değerlendir |
| `Method not found` | Juggler'da CDP methodu yok/yanlış | Validate + get command/dependency; native Mirage tool seç |

Hata alınca aynı hatayı körlemesine tekrarlama. Önce state'i yeniden oku,
gerekirse `kahin_error_decode` ve `kahin_get_dependencies` kullan.

## 9. Güvenlik ve sınırlar

- DOM snapshot ve event payload'ları cap'lidir; bounded sonuçlar ajana açıkça
  `truncated`, `pending`, `reset` ve `dropped` durumlarını verir.
- Password input değerleri snapshot/event descriptor'larında redacted olur.
- `attributes` tam HTML değildir; güvenli kimlik ve erişilebilirlik alanlarıyla
  sınırlıdır. Tam HTML gerekiyorsa bunun maliyetini bilerek
  `kahin_mirage_get_html` veya `kahin_mirage_page_content` kullan.
- `kahin_mirage_dom_action` allow-list dışı JavaScript çalıştırmaz.
  `kahin_evaluate` genel amaçlıdır; sayfa verisi ve yan etkiler bakımından
  ayrıca değerlendirilmelidir.
- DOM event'i uygulamanın başarılı olduğunu kanıtlamaz. Her write/click
  sonrasında görünür sonucu veya state'i doğrula.

## 10. Kaynak ve tasarım kararları

Bu tasarımda browser gözlemi için gerçek web platformu `MutationObserver`,
Juggler binding/init-script surface'i ve bounded cursor modeli kullanılır.
CDP DOM domain'indeki node-id/document reset fikriyle aynı güvenlik kuralı
uygulanır: document değişince eski node truth değildir.

- [MDN MutationObserver](https://developer.mozilla.org/en-US/docs/Web/API/MutationObserver/observe)
- [Chrome DevTools Protocol DOM domain](https://chromedevtools.github.io/devtools-protocol/tot/DOM/)
- [Playwright Page API: init scripts and page lifecycle](https://playwright.dev/docs/api/class-page)
- [Model Context Protocol server concepts](https://modelcontextprotocol.io/docs/learn/server-concepts)

MCP resource subscription her istemci/SDK sürümünde aynı şekilde mevcut
olmadığından, taşınabilir ilk sözleşme tool + bounded long-poll'dur. İleride
resource subscription eklense bile `streamId`, `cursor`, `reset` ve
`dropped` kuralları değişmemelidir.
