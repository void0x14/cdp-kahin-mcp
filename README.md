# Kahin

Chrome DevTools Protocol'unu bilen, doğrulayan ve kontrol eden MCP server.

```bash
uv pip install kahin
# opencode/claude code'a ekle:
# "kahin": { "command": "python3", "args": ["-m", "kahin.oracle"] }
```

## Ne işe yarar?

CDP'yi (Chrome DevTools Protocol) bilirsiniz ya da bilmezsiniz. Kahin bilir.

AI modeller Chrome'un içine girip sayfa gezip kod çalıştırabilir ama CDP'yi ezbere bilmezler — hangi domain hangi komutu alır, hangi parametre zorunludur, hangi event ne zaman fırlar bilmezler. Kahin bunu onlara söyler, yanlış yapınca düzeltir, bilmiyorsa öğretir.

56 domain, 667 komut, 237 event, 609 type — Chrome 148 protokolü gömülü.

## 32 Tool · 6 Kategori

| Kategori | Ne işe yarar | Tool sayısı |
|----------|-------------|-------------|
| 🧠 CDP Bilgi | Domain/komut/event/type sorgulama, semantik arama | 7 |
| ✅ Doğrulama | Komut doğrulama, typo tespiti, hata çözümleme | 3 |
| 🚀 Browser Kontrol | Chrome başlat/durdur, gezin, tıkla, kod çalıştır, ekran görüntüsü | 9 |
| 🔗 Session | Yeni sayfa aç/kapat, session listele | 4 |
| 🔍 Debug | CDP event geçmişi, network istekleri, console mesajları | 4 |
| 📈 Pattern DB | Kullanım desenlerini öğren, sorgula, öner | 5 |

## Bir satırda özet

CDP'yi bilmeyen AI'a Chrome'u kontrol etmeyi öğreten, yanlış yapınca düzelten, her şeyi loglayan MCP.

## Kurulum

Zorunlu: Python 3.12+ · Chrome/Chromium

```bash
git clone https://github.com/void0x14/cdp-kahin-mcp
cd cdp-kahin-mcp
uv venv && source .venv/bin/activate
uv pip install -e .
```

### opencode'a ekle

`~/.config/opencode/opencode.json`:

```json
"kahin": {
  "type": "local",
  "command": ["python3", "-m", "kahin.oracle"],
  "enabled": true
}
```

### Claude Code'a ekle

`doggystyle/.mcp.json` veya herhangi bir projenin köküne:

```json
{
  "mcpServers": {
    "kahin": {
      "command": "python3",
      "args": ["-m", "kahin.oracle"]
    }
  }
}
```

## Kullanım

AI modeline şunu söyle: **"Kahin MCP'sini kullan."**

Gerisini AI halleder. Ama dilersen tool'ları direkt de çağırabilirsin:

```
→ kahin_list_domains                    → 56 domain listeler
→ kahin_get_command(Page,navigate)      → parametreleri gösterir
→ kahin_validate_command(Page,navigate) → doğrular
→ kahin_browser_start → navigate → extract → screenshot → stop
→ kahin_error_decode(error_code=-32601) → hatayı çözümler
```

Tam liste için: [AGENTS.md](AGENTS.md)

## Proje Felsefesi

- **Tahmin yok, bilgi var.** AI tahmin etmez, Kahin'in gömülü CDP şemasına bakar.
- **Hata kabul, eğitim zorunlu.** Yanlış komut gelince düzeltir, neden yanlış olduğunu söyler.
- **Minimal bağımlılık.** Temel işlevler için 7 paket, hiçbiri ağır değil.
- **Her şey loglanır.** `kahin/logs/kahin.log` — JSON satırları, her hata kayıt altında.

## Bağımlılıklar

mcp · orjson · Levenshtein · websockets · httpx · camoufox · Pillow

## Port Uyarısı

| Port | Kimin | Kullanma |
|------|-------|----------|
| 9222 | Chrome DevTools | RESERVED |
| 9240 | Kusatma Engine | RESERVED |
| 9241 | Obscura (varsayılan) | Kahin kullanır |
| 9242 | Mirage (varsayılan) | Kahin kullanır |

## Kendi Kendini Onarma

Kahin'de hata loglama ve kendini onarma sistemi gömülüdür:

- Hatalar `kahin/logs/kahin.log` dosyasına JSON satırları halinde yazılır
- Bağlantı kopması, engine çökmesi, session kaybı gibi durumlarda otomatik kurtarma dener
- `kahin_healer_stats` ile hata istatistikleri sorgulanabilir

## Mimari

```
oracle.py               → MCP server (32 tool, giriş kapısı)
  _healer.py             → Hata yönetimi, loglama, kendini onarma
  the_source/architect   → CDP şema motoru (56 domain, 667 komut)
  the_twins/shadow       → Obscura engine (hızlı Chrome, WebSocket CDP)
  the_twins/mirage       → Mirage engine (stealth, anti-detection)
  the_twins/chassis      → Ortak engine arayüzü (abstract)
  residual_self/fate     → Pattern DB (öğrenme, sorgulama, önerme)
```

---

## 🗺️ Yol Haritası

### ⚡ Acil (1-2 hafta)

- [ ] **Tek tık kurulum** — `uvx kahin` ile direkt çalıştır
- [ ] **Zero-dependency** hedefi (Go/Rust portu)
- [ ] **LSP modu** — kod içinde hata yakalama, AI'a yanlışını yüzüne vurma
- [ ] **Tool sayısı 50+** — eksik CDP domain tool'ları

### 🎯 Kısa vade (1 ay)

- [ ] **Gerçek zamanlı izleme** — AI'ın Kahin'i nasıl kullandığını canlı gör
- [ ] **Web dashboard** — tool çağrıları, hata oranları, trendler
- [ ] **MCP Ekosistemi** — üçüncü taraf MCP'lere proxy/entegrasyon
- [ ] **CLI aracı** — `kahin` komutu ile hızlı sorgulama
- [ ] **Pasif tarama** — arka planda CDP event'lerini izle, değişiklik olunca bildir
- [ ] **Dokümantasyon sitesi** — kapsamlı kullanım kılavuzu

### 🚀 Uzun vade (3+ ay)

- [ ] **CDP derleyici** — yeni Chrome sürümlerini otomatik tanıyıp şemayı güncelle
- [ ] **Plugin sistemi** — herkes kendi CDP tool'unu yazıp ekleyebilir
- [ ] **All-in-one MCP** — sadece CDP değil, browser kontrolünün tek adresi
- [ ] **AI davranış analizi** — hangi tool ne sıklıkta kullanılmış, hata trendleri
- [ ] **Paylaşımlı oturum** — ekibin MCP'sini tek merkezden yönet
- [ ] **İleri kendini onarma** — öngörülü hata önleme, otomatik düzeltme

---

## Geliştirme

```bash
.venv/bin/pytest tests/          # 66 test, 0 failed
.venv/bin/ruff check kahin/      # lint
.venv/bin/python -m kahin.oracle # manuel başlatma
```
