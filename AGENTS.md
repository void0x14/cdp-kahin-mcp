# cdp-kahin-mcp — AI Model Kullanım Kılavuzu

Bu MCP projesi sana CDP (Chrome DevTools Protocol) bilgisi ve doğrulaması sağlar.

## Kullanım Kuralları (UYMAK ZORUNDASIN)

### Kural 1: CDP komutu göndermeden ÖNCE doğrula

```python
# YANLIŞ — doğrulamadan gönderme:
CDP komutu: Page.navigate(url="...")

# DOĞRU — önce doğrula:
-> kahin_validate_command(domain="Page", command="navigate", parameters={"url": "..."})
<- {"valid": true, ...}
-> CDP komutu: Page.navigate(url="...")
```

### Kural 2: Hata alınca çözümle

```
-> CDP hatası: "Method not found: Page.navigat"
-> kahin_error_decode(error_code=-32601, error_message="Method not found: Page.navigat")
<- {"common_causes": ["Typo: 'Page.navigat' should be 'Page.navigate'"], ...}
```

### Kural 3: Bilmediğin CDP'yi sorgula

```
-> kahin_get_command(domain="Fetch", command="enable")
<- {"parameters": [{"name": "patterns", ...}], ...}
```

### Kural 4: Proje pattern'larını sorgula

```
-> kahin_pattern_query(context="login")
<- [{"pattern": "Page.navigate + Network.responseReceived", ...}]
```

## Mevcut Tool'lar

### CDP Bilgi (GRIMOIRE)
- `kahin_list_domains` — 56 domain listele
- `kahin_get_domain` — Domain detayı
- `kahin_get_command` — Komut parametre/dönüş detayı
- `kahin_get_event` — Event parametre detayı
- `kahin_kahin_find_concept` — Semantik arama
- `kahin_list_types` — Type listele
- `kahin_get_type` — Type detay (properties, enum)

### CDP Doğrulama (SERAPH)
- `kahin_validate_command` — Komut doğrula (typo tespiti dahil)
- `kahin_error_decode` — Hata çözümle
- `kahin_get_dependencies` — Ön koşulları göster

## Önemli Notlar

1. CDP method isimleri **case-sensitive**: `Page.navigate` ✓, `page.navigate` ✗, `Page.Navigate` ✗
2. CDP domain'ler: `Accessibility`, `Animation`, `Audits`, `Autofill`, `BackgroundService`, `Browser`, `CSS`, `CacheStorage`, `Cast`, `Console`, `DOM`, `DOMDebugger`, `DOMSnapshot`, `DOMStorage`, `Database`, `Debugger`, `DeviceOrientation`, `Emulation`, `EventBreakpoints`, `Extensions`, `FedCM`, `Fetch`, `FileSystem`, `HeadlessExperimental`, `HeapProfiler`, `IO`, `IndexedDB`, `Input`, `Inspector`, `LayerTree`, `Log`, `Media`, `Memory`, `Network`, `Overlay`, `Page`, `Performance`, `PerformanceTimeline`, `Preload`, `Profiler`, `Runtime`, `Security`, `ServiceWorker`, `Storage`, `SystemInfo`, `Target`, `Tethering`, `Tracing`, `WebAudio`, `WebAuthn`, `DeviceAccess`, `StorageKey`, `SharedStorage`, `BluetoothDeviceEmulation`, `NetworkCustom`, `PWA`
3. Port 9222 ve 9240 KULLANILAMAZ — başka port kullan
