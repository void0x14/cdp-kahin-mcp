# cdp-kahin-mcp — AI Model Kullanım Kılavuzu

Bu MCP projesi sana CDP (Chrome DevTools Protocol) bilgisi ve doğrulaması sağlar.

## Kullanım Kuralları (UYMAK ZORUNDASIN)

### Kural 1: CDP komutu göndermeden ÖNCE doğrula
```
YANLIŞ: Page.navigate(url="...")
DOĞRU:  kahin_validate_command(domain="Page", command="navigate", parameters={"url": "..."})
        -> {"valid": true, ...}
        -> Page.navigate(url="...")
```

### Kural 2: Hata alınca çözümle
```
kahin_error_decode(error_code=-32601, error_message="Method not found: Page.navigat")
-> {"common_causes": ["Typo: 'Page.navigat' should be 'Page.navigate'"]}
```

### Kural 3: Bilmediğin CDP'yi sorgula
```
kahin_get_command(domain="Page", command="navigate")
-> {"parameters": [{"name": "url", "type": "string", ...}], ...}
```

### Kural 4: Pattern'ları sorgula ve öğren
```
kahin_pattern_query(context="login")     -> geçmiş pattern'lar
kahin_pattern_suggest(partial="navig")   -> autocomplete
```

## Mevcut Tool'lar (31 adet)

### GRIMOIRE — CDP Bilgi
- `kahin_list_domains` — 56 domain listele
- `kahin_get_domain` — Domain detayı
- `kahin_get_command` — Komut parametre/dönüş detayı
- `kahin_get_event` — Event parametre detayı
- `kahin_find_concept` — Semantik arama
- `kahin_list_types` — Type listele
- `kahin_get_type` — Type detay (properties, enum)

### SERAPH — CDP Doğrulama
- `kahin_validate_command` — Komut doğrula (typo tespiti!)
- `kahin_error_decode` — Hata çözümle
- `kahin_get_dependencies` — Ön koşulları göster

### PILOT — Browser Kontrol
- `kahin_browser_start` — Engine başlat (shadow/mirage)
- `kahin_browser_stop` — Engine durdur
- `kahin_navigate` — URL'e git
- `kahin_click` — Element tıkla (CSS selector)
- `kahin_extract` — Metin/attribut çek
- `kahin_screenshot` — Ekran görüntüsü
- `kahin_evaluate` — JavaScript çalıştır
- `kahin_execute_cdp` — Ham CDP komutu (advanced)

### TRAINMAN — Session/Target
- `kahin_list_sessions` — Tüm target'ları listele
- `kahin_get_session` — Session detayı
- `kahin_create_session` — Yeni target oluştur
- `kahin_kill_session` — Target kapat

### DEJA_VU — Debug/Network
- `kahin_event_history` — CDP event geçmişi
- `kahin_list_network_requests` — Network istekleri
- `kahin_get_console` — Console mesajları
- `kahin_iframe_tree` — Frame hiyerarşisi

### PROPHECY — Pattern DB
- `kahin_pattern_learn` — Pattern öğret
- `kahin_pattern_query` — Pattern sorgula
- `kahin_pattern_suggest` — Autocomplete öner
- `kahin_pattern_forget` — Pattern unuttur
- `kahin_pattern_stats` — Pattern istatistikleri

## Önemli Notlar
1. CDP **case-sensitive**: `Page.navigate` ✓, `page.navigate` ✗
2. Port 9222 ve 9240 KULLANILAMAZ
3. `kahin_execute_cdp` en güçlü tool — her CDP komutunu çalıştırabilir
4. Pattern'lar otomatik öğrenilir (navigate/click/evaluate sonrası)
