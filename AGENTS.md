# Kahin — CDP MCP Server Kullanım Kılavuzu

Bu MCP server, Chrome DevTools Protocol (CDP) bilgisi, doğrulaması ve browser kontrolü sağlar.
56 domain, 667 komut, 237 event, 609 type — Chrome 148.

## Tool İsimlendirme

OpenCode'da tool isimleri `kahin_` prefix'i ile başlar. AGENTS.md'deki isimler aynen kullanılır:

```
kahin_list_domains       → OpenCode: kahin_list_domains
kahin_get_command        → OpenCode: kahin_get_command
kahin_browser_start      → OpenCode: kahin_browser_start
```

## Guardian Sistemi — Otomatik Hata Yakalama ve Eğitim

Kahin MCP, AI modelin CDP hatalarını **otomatik yakalar** ve **doğrusunu öğretir**.

### Nasıl Çalışır?

```
AI: kahin_execute_cdp(domain="Page", command="navigat", parameters={"url": "..."})
    ↓ (otomatik validation)
MCP: {
  "error": "Command validation failed",
  "validation_errors": [{"param": "navigat", "message": "Unknown command 'Page.navigat'. Did you mean 'navigate'?"}],
  "correction": ["Typo: Page.navigat → Page.navigate"]
}
    ↓ (AI hatasını anlar, düzeltir)
AI: kahin_execute_cdp(domain="Page", command="navigate", parameters={"url": "..."})
    → {"frameId": "...", "loaderId": "..."}
```

### AI Model İçin Kural
Sakın tahmin etme. CDP komutunda en ufak şüphen varsa:
1. `kahin_validate_command` ile kontrol et
2. Hata alırsan `kahin_error_decode` ile çözümle
3. Pattern varsa `kahin_pattern_query` ile öğren

## ZORUNLU Kullanım Kuralları

### Kural 1: CDP komutu göndermeden ÖNCE doğrula
```
YANLIŞ: Page.navigate(url="...")                    # typo riski
DOĞRU:  kahin_validate_command(domain="Page", command="navigate", parameters={"url": "..."})
        → {"valid": true, ...}
        → Page.navigate(url="...")
```

### Kural 2: Hata alınca çözümle
```
kahin_error_decode(error_code=-32601, error_message="Method not found: Page.navigat")
→ "Typo in method name: Page.navigat should be Page.navigate"
```

### Kural 3: Bilmediğin CDP'yi sorgula
```
kahin_get_command(domain="Page", command="navigate")
→ {"parameters": [{"name": "url", "type": "string", ...}], ...}
```

### Kural 4: Pattern'ları sorgula ve öğren
```
kahin_pattern_query(context="doggystyle")
kahin_pattern_suggest(partial="navig")
```

## Tool Listesi (32 adet)

### GRIMOIRE — CDP Bilgi (7)
| Tool | Ne işe yarar? |
|------|---------------|
| `kahin_list_domains` | Tüm domainleri listele (56 adet) |
| `kahin_get_domain` | Domain içindeki komut/event/type'ları göster |
| `kahin_get_command` | Komut parametrelerini ve dönüşlerini göster |
| `kahin_get_event` | Event parametrelerini göster |
| `kahin_find_concept` | Doğal dille CDP konsepti ara (ör: "take screenshot") |
| `kahin_list_types` | Domain'deki type'ları listele |
| `kahin_get_type` | Type property'lerini ve enum değerlerini göster |

### SERAPH — Doğrulama (3)
| Tool | Ne işe yarar? |
|------|---------------|
| `kahin_validate_command` | CDP komutunu göndermeden ÖNCE doğrula (typo tespit eder!) |
| `kahin_error_decode` | CDP hata kodunu çözümle, alternatif öner |
| `kahin_get_dependencies` | Komutun ön koşullarını göster |

### PILOT — Browser Kontrol (9)
| Tool | Ne işe yarar? |
|------|---------------|
| `kahin_browser_start` | Browser motoru başlat (shadow/mirage) |
| `kahin_browser_stop` | Browser'ı durdur, state temizle |
| `kahin_navigate` | URL'e git |
| `kahin_click` | CSS selector ile element tıkla |
| `kahin_extract` | Sayfadan metin/attribute çek |
| `kahin_screenshot` | Ekran görüntüsü al (base64 PNG) |
| `kahin_evaluate` | JavaScript çalıştır |
| `kahin_execute_cdp` | Ham CDP komutu gönder (ileri seviye) |

### TRAINMAN — Session (4)
| Tool | Ne işe yarar? |
|------|---------------|
| `kahin_list_sessions` | Açık target'ları listele |
| `kahin_get_session` | Session detayını göster |
| `kahin_create_session` | Yeni sayfa/target oluştur |
| `kahin_kill_session` | Target'ı kapat |

### DEJA_VU — Debug (4)
| Tool | Ne işe yarar? |
|------|---------------|
| `kahin_event_history` | CDP event geçmişini göster (filtreleme destekler) |
| `kahin_list_network_requests` | Network isteklerini listele |
| `kahin_get_console` | Console mesajlarını göster |
| `kahin_iframe_tree` | Frame/iframe hiyerarşisini göster |

### PROPHECY — Pattern DB (5)
| Tool | Ne işe yarar? |
|------|---------------|
| `kahin_pattern_learn` | Yeni pattern öğret |
| `kahin_pattern_query` | Pattern ara (domain/context filtresi) |
| `kahin_pattern_suggest` | Kısmi isimle autocomplete |
| `kahin_pattern_forget` | Pattern sil |
| `kahin_pattern_stats` | Pattern istatistikleri |

## Örnek İş Akışları

### 1. CDP Komutu Araştırma + Doğrulama + Gönderme
```
→ kahin_find_concept(query="navigate to url", max_results=3)
→ kahin_get_command(domain="Page", command="navigate")
→ kahin_validate_command(domain="Page", command="navigate", parameters={"url": "..."})
→ (komut güvenle gönderilir)
```

### 2. Browser Aç + Sayfaya Git + İçerik Çek
```
→ kahin_browser_start(engine="shadow", headless=true)
→ kahin_navigate(url="https://github.com/void0x14/doggystyle")
→ kahin_extract()                              -> tüm sayfa metni
→ kahin_screenshot()                           -> ekran görüntüsü
→ kahin_browser_stop()
```

### 3. Hata Ayıklama
```
→ kahin_error_decode(error_code=-32601, error_message="Method not found: Page.navigat")
  -> "Typo: Page.navigat → Page.navigate"
→ kahin_get_dependencies(domain="Fetch", command="enable")
  -> "Must call Fetch.enable to activate the domain"
```

### 4. Pattern Kullanımı
```
→ kahin_pattern_stats()                          -> mevcut pattern'lar
→ kahin_pattern_learn(domain="Page", command="navigate", context="doggystyle")
→ kahin_pattern_query(context="doggystyle")      -> öğrenilenler
```

## Önemli Notlar
1. CDP **case-sensitive**: `Page.navigate` ✓, `page.navigate` ✗
2. Port 9222 (Chrome DevTools) ve 9240 REZERVE — kullanma
3. Hatalar `logs/kahin.log` dosyasına JSON formatında kaydedilir
4. `kahin_healer_stats` ile hata istatistiklerini sorgula
5. Pattern'lar otomatik öğrenilir (navigate/click/evaluate sonrası)
