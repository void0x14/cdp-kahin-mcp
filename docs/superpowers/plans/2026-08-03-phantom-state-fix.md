# Phantom-State Fix — Kahin asla ölü browser'ı "çalışıyor" sanmayacak

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Browser crash/ölümünde Kahin'in state'i anında doğruya dönsün: `browser_start` gerçek boot kanıtı olmadan "started" demeyecek, ölü engine "already running" yalanı üretmeyecek, komutlar 30s asılmadan hızlı hata dönecek.

**Architecture:** Üç katmanlı kesin çözüm: (1) Zig sidecar browser fd HUP/EOF'unda çıkış yapar + `Browser.health` komutu sunar (mevcut `Instance.health()`'i production hattına bağlar); (2) Python Mirage start() gerçek boot doğrulaması yapar, reader ölümünde callback verir, `is_alive()` sunar; (3) Oracle liveness'e bakar, hata yutmaz, healer'ı gerçekten bağlar. Shadow/Obscura aynı arayüzden geçer (chassis'te abstract `is_alive`).

**Tech Stack:** Zig 0.16.0 (PINNED — 0.17 build'i kırar), Python 3.13 (uv venv), pytest (uv run), sidecar vendor binary (MCP runtime zig'siz).

## Global Constraints

- Zig sürümü: `~/.local/opt/zig-x86_64-linux-0.16.0/zig` — sadece 0.16.x. `scripts/build-sidecar.sh` kullan (zig doğrulaması içinde).
- Vendor binary her Zig değişikliğinde yeniden build edilip commit'lenir: `camoufox-harness/vendor/bin/kahin-sidecar` (MCP runtime zig gerektirmez).
- Camoufox binary'sine DOKUNULMAZ: raw spawn yok, bayrak değişikliği yok — sidecar'ın mevcut spawn yolu (driver.zig) aynen korunur.
- Wire protocol geriye dönük uyumlu: eski request/response/event şekilleri değişmez; sadece YENİ `Browser.health` komutu eklenir.
- 32 MCP tool adı DEĞİŞMEZ (AGENTS.md uyumu; tests/test_oracle.py:47 `len(tools) == 32`).
- Test komutları: `uv run pytest tests/ -q` (venv'de pytest yok — uv ile çalışır) ve `zig build test` (camoufox-harness/core içinde).
- Mevcut yeşil: 102 pytest koleksiyonu, zig 176 test, ruff temiz — hiçbiri kırılmayacak.
- Branch: `feat/faz8-phantom-fix` (main'den).

---

### Task 1: Zig sidecar — browser ölümünde çıkış + Browser.health komutu

**Files:**
- Modify: `camoufox-harness/core/ipc_main.zig:107-130` (ana poll loop), `camoufox-harness/core/ipc_main.zig:546-551` (readChunk EOF), `camoufox-harness/core/ipc_main.zig:182-188` (domain dispatch)
- Modify: `camoufox-harness/core/tests.zig` (yeni test)
- Rebuild: `scripts/build-sidecar.sh` → vendor binary güncelle

**Interfaces:**
- Produces: `Browser.health` komutu → `{"id":N,"result":{"alive":bool,"pid":i32,"state":"running"|"dead"}}`. Browser fd HUP/EOF'unda sidecar `running=false` yapar, döngü kırılır, `defer d.stop()` browser'ı indirir, exit 0.
- Consumes: `Instance.health()` — `camoufox-harness/core/process-manager/lifecycle.zig:193-209` (zaten var; production'a bağlanıyor).

- [ ] **Step 1: Mevcut davranışı kanıtlayan test yaz (zig)**

`camoufox-harness/core/tests.zig` içine (mevcut test yapısına uygun, pipe.zig:382-399'daki EOF testlerini örnek al):

```zig
test "health reports dead after child exit" {
    // Driver.start'ı fake firefox script'i ile ayağa kaldır (mevcut test fixture'ları varsa onları kullan),
    // Instance.health() -> .healthy, sonra child'i SIGKILL et,
    // 50ms bekle, health() -> .dead doğrula.
}
```

Not: Bu test mevcut lifecycle unit testlerini (`lifecycle.zig:331-385`) genişletir — gerçek child process + SIGKILL + health geçişi. Eğer tests.zig'de driver spawn fixture'ı yoksa, saf `Instance.spawnArgv` (lifecycle.zig:115) ile `sleep` gibi bir process spawn et, health → kill → health.

- [ ] **Step 2: Zig testi çalıştır, mevcut haliyle geçer/gerekirse RED doğrula**

Run: `zig build test` (camoufox-harness/core içinde, `~/.local/opt/zig-x86_64-linux-0.16.0/zig`)
Expected: yeni test geçer (health mekanizması zaten çalışıyor — sorun production bağlantısında).

- [ ] **Step 3: ipc_main.zig — browser fd HUP/ERR → running=false**

`ipc_main.zig:116-118` bloğunu şöyle değiştir:

```zig
if (pollfds[1].revents & (linux.POLL.IN | linux.POLL.HUP | linux.POLL.ERR) != 0) {
    drainEvents(&d, a, 0) catch {};
    if (pollfds[1].revents & (linux.POLL.HUP | linux.POLL.ERR) != 0) {
        running = false; // browser fd kapandı -> sidecar browser'la birlikte kapanır
    }
}
```

Ve `readChunk` içinde (`ipc_main.zig:546-551`) `n == 0` EOF'u da `running = false` tetiklesin: `drainEvents`'in döndüğü EOF sinyalini ana döngüye ilet — en temiz yol: `drainEvents` sonrası `if (pollfds[1].revents & (POLL.HUP|POLL.ERR) != 0) running = false;` (yukarıdaki zaten bunu yapar). Ayrıca `readChunk`'taki `n == 0` durumuna `running = false; return;` ekle (HUP olmadan saf EOF için, mevcut boş `else => {}` dalını değiştir).

- [ ] **Step 4: Browser.health komutu — domain dispatch**

`ipc_main.zig:182-188` domain dispatch bölgesine (Browser passthrough'undan ÖNCE yakalanır):

```zig
if (std.mem.eql(u8, command, "health")) {
    const h = d.instance.health();
    // alive: h == .healthy; pid: d.instance.child.pid; state: "running"|"dead"
    try respondJson(&d, arena, id, .{
        .alive = h == .healthy,
        .pid = @as(i32, @intCast(d.instance.child.pid)),
        .state = if (h == .healthy) "running" else "dead",
    });
    return;
}
```

`d.instance` erişimi driver.zig'de public mi kontrol et — değilse `Instance` alanına public getter ekle (driver.zig'de küçük ekleme; mevcut `Driver` struct'ına `pub const instance = ...` veya `pub fn` health passthrough). Yapıyı kırma, en az invaziv erişimi seç.

- [ ] **Step 5: Build + tüm zig testleri**

Run: `~/.local/opt/zig-x86_64-linux-0.16.0/zig build test && ~/.local/opt/zig-x86_64-linux-0.16.0/zig build-exe --dep driver -Mroot=ipc_main.zig -Mdriver=driver.zig -O ReleaseSafe -femit-bin=zig-out/bin/kahin-sidecar`
Expected: exit 0, tüm testler geçer.

- [ ] **Step 6: Vendor binary güncelle**

Run: `bash scripts/build-sidecar.sh`
Expected: `camoufox-harness/vendor/bin/kahin-sidecar` yenilenir (git diff boyut farkı gösterir).

- [ ] **Step 7: Commit**

```bash
git add camoufox-harness/core/ camoufox-harness/vendor/bin/kahin-sidecar
git commit -m "fix(sidecar): exit on browser fd HUP; add Browser.health command"
```

---

### Task 2: Mirage — boot doğrulaması, ölüm callback, is_alive

**Files:**
- Modify: `kahin/the_twins/mirage.py:79-120` (start), `:122-168` (reader), `:194-217` (stop)
- Modify: `kahin/the_twins/chassis.py` (`BrowserEngine` abstract — `is_alive` ekle)
- Modify: `kahin/the_twins/shadow.py` (Obscura `is_alive` implementasyonu)
- Test: `tests/test_mirage_ipc.py` (yeni testler)

**Interfaces:**
- Consumes: Task 1 `Browser.health` komutu.
- Produces: `Mirage.is_alive() -> bool` (process canlı mı); `Mirage.on_death(cb)` — reader ölümünde `cb()` çağrılır; `start()` boot doğrulaması başarısızsa `RuntimeError` raise eder (artık "started" yok).

- [ ] **Step 1: Failing testler yaz (fake sidecar)**

`tests/test_mirage_ipc.py` sonuna ekle:

```python
@pytest.mark.asyncio
async def test_start_raises_when_sidecar_dies_immediately(tmp_path, monkeypatch):
    """Boot fail: sidecar aninda cikar -> start() RuntimeError, 'started' YOK."""
    script = tmp_path / "die_sidecar.py"
    script.write_text("#!/usr/bin/env python3\nimport sys\nsys.exit(1)\n")
    script.chmod(0o755)
    monkeypatch.setattr(mirage_mod, "_sidecar_bin", lambda: script)
    monkeypatch.setattr(mirage_mod, "_camoufox_bin", lambda: script)
    engine = Mirage()
    with pytest.raises(RuntimeError):
        await engine.start()
    assert engine.is_alive() is False


@pytest.mark.asyncio
async def test_is_alive_tracks_process(fake_sidecar):
    engine = Mirage()
    await engine.start()
    assert engine.is_alive() is True
    await engine.stop()
    assert engine.is_alive() is False


@pytest.mark.asyncio
async def test_reader_death_fires_callback(fake_sidecar, monkeypatch):
    """Reader EOF (sidecar cikis) -> on_death callback tetiklenir."""
    script = tmp_path / "exit_sidecar.py"
    script.write_text("#!/usr/bin/env python3\nimport sys, json\nfor line in sys.stdin:\n    sys.exit(0)\n")
    script.chmod(0o755)
    monkeypatch.setattr(mirage_mod, "_sidecar_bin", lambda: script)
    monkeypatch.setattr(mirage_mod, "_camoufox_bin", lambda: script)
    engine = Mirage()
    died = asyncio.Event()
    engine.on_death(lambda: died.set())
    await engine.start()
    await engine.send_cdp("Browser", "close")  # sidecar'i kapatir
    await asyncio.wait_for(died.wait(), timeout=2.0)
```

Not: `test_reader_death_fires_callback` için `fake_sidecar` fixture'ı yerine tmp script kullan — mevcut fixture'ın `on_death` ile çakışmasın.

- [ ] **Step 2: Testleri çalıştır, RED doğrula**

Run: `uv run pytest tests/test_mirage_ipc.py -q -k "start_raises or is_alive or reader_death"`
Expected: FAIL — `is_alive` yok (AttributeError), start() raise etmiyor, callback yok.

- [ ] **Step 3: chassis.py — abstract is_alive**

`BrowserEngine` sınıfına (chassis.py abstract metodların yanına):

```python
@abc.abstractmethod
def is_alive(self) -> bool:
    """True while the engine process/connection is alive."""
```

`EngineContext`'e dokunma.

- [ ] **Step 4: mirage.py — start boot doğrulaması**

`start()` içinde `self._start_reader()` sonrası (satır 111 ile return arası):

```python
self._start_reader()
try:
    boot = await asyncio.wait_for(self.send_cdp("Browser", "health"), timeout=15.0)
    if not boot.get("alive"):
        raise RuntimeError(f"Camoufox failed to boot (health: {boot})")
except (asyncio.TimeoutError, RuntimeError):
    await self.stop()
    raise
return EngineContext(...)
```

Dikkat: `send_cdp` reader'a bağlı — sidecar spawn'dan hemen sonra çıktıysa reader EOF olur, `send_cdp` "Mirage sidecar exited" RuntimeError üretir → yakalanır → `stop()` → raise. `stop()` içinde reader None kontrolü zaten var (`mirage.py:195-204`), reader'ı `self._reader`'a kaydet (start'ta `_start_reader` bunu yapıyor mu kontrol et — değilse `self._reader = ...` düzelt).

- [ ] **Step 5: mirage.py — on_death + is_alive**

```python
def on_death(self, cb) -> None:
    """Register callback invoked when the reader detects sidecar death."""
    self._death_callbacks.append(cb)
```

`__init__`'e `self._death_callbacks: list = []` ekle. Reader `finally` bloğunda (satır 161-166, pending fail'lerden sonra):

```python
finally:
    for fut in self._pending.values():
        if not fut.done():
            fut.set_exception(RuntimeError("Mirage sidecar exited"))
    self._pending.clear()
    for cb in list(self._death_callbacks):
        try:
            result = cb()
            if asyncio.iscoroutine(result):
                asyncio.create_task(result)
        except Exception:
            logger.exception("death callback failed")
```

`is_alive`:

```python
def is_alive(self) -> bool:
    return self._process is not None and self._process.returncode is None
```

- [ ] **Step 6: shadow.py — Obscura is_alive**

Obscura'da (shadow.py) engine'in canlılığı: ws bağlantısı / reader task durumu. Mevcut yapıya en uygun implementasyon (ör. `self._ws is not None and not self._ws.closed` veya reader task `not done()`) — mevcut koda bak, en doğrusunu seç, testle doğrula.

- [ ] **Step 7: Testleri çalıştır, GREEN doğrula**

Run: `uv run pytest tests/test_mirage_ipc.py tests/test_e2e.py tests/test_obscura.py -q`
Expected: PASS — yeni 3 test + mevcut mirage/obscura/e2e hepsi yeşil.

- [ ] **Step 8: Commit**

```bash
git add kahin/the_twins/ tests/test_mirage_ipc.py
git commit -m "fix(mirage): boot verification via Browser.health; on_death callback; is_alive"
```

---

### Task 3: Oracle — liveness kontrolü, hata yutma kaldırma, state temizliği

**Files:**
- Modify: `kahin/oracle.py:182-187` (`_require_engine`), `:190-238` (browser_start), `:259-269` (browser_stop), reader-death handler ekleme
- Test: `tests/test_oracle_phase3.py` veya yeni `tests/test_phantom.py`

**Interfaces:**
- Consumes: Task 2 `is_alive()`, `on_death()`.
- Produces: `browser_start` ölü engine'de yeniden başlatır (önce temizler); `_require_engine` ölü engine'de net hata; reader death → `_current_engine=None` + `clear_state()`.

- [ ] **Step 1: Failing testler yaz**

`tests/test_phantom.py` (yeni dosya) — fake engine ile (Mirage/Obscura yerine stub):

```python
class DeadEngine:
    def __init__(self): self.stopped = False
    def is_alive(self): return False
    async def stop(self): self.stopped = True
    # BrowserEngine arayüzünün geri kalanı: send_cdp, screenshot, on_event -> dummy

@pytest.mark.asyncio
async def test_browser_start_replaces_dead_engine(monkeypatch):
    """State'te engine var ama ölü -> browser_start hata değil, yeniden başlatır."""
    # oracle._current_engine = DeadEngine() (or import edilebilir helper)
    # kahin_browser_start(engine="shadow") çağrısı: hata İÇERMEYEN başarı dönmeli
    # ve dead.stopped True olmalı (eski engine temizlendi)

@pytest.mark.asyncio
async def test_require_engine_detects_dead(monkeypatch):
    """_current_engine ölü -> _require_engine net 'engine is not alive' mesajı."""

@pytest.mark.asyncio
async def test_reader_death_clears_state(monkeypatch):
    """on_death callback'i _current_engine'i None yapar."""
```

Not: `oracle.py`'de `_current_engine` import edilebilir (`from kahin import oracle` → `oracle._current_engine`). Testler modül global'ini monkeypatch ile kurar.

- [ ] **Step 2: Testleri çalıştır, RED doğrula**

Run: `uv run pytest tests/test_phantom.py -q`
Expected: FAIL — mevcut davranış: "Engine already running" döner, temizleme yok.

- [ ] **Step 3: oracle.py — browser_start liveness**

`oracle.py:201-202` bloğunu değiştir:

```python
if _current_engine is not None:
    if _current_engine.is_alive():
        return "Engine already running. Stop it first with kahin_browser_stop."
    logger.warning("engine in state but dead — replacing")
    try:
        await _current_engine.stop()
    except Exception:
        logger.debug("dead engine stop failed (expected)", exc_info=True)
    _current_engine = None
    clear_state()
```

- [ ] **Step 4: oracle.py — _require_engine liveness**

```python
if _current_engine is None:
    return "No browser engine running. Use kahin_browser_start first."
if not _current_engine.is_alive():
    return "Browser engine is not alive (crashed?). Use kahin_browser_stop then kahin_browser_start."
```

- [ ] **Step 5: oracle.py — hata yutma kaldırma**

`oracle.py:232-236` bloğunu değiştir:

```python
try:
    await _current_engine.send_cdp("Network", "enable")
    await _current_engine.send_cdp("Console", "enable")
except Exception as e:
    await _current_engine.stop()
    _current_engine = None
    raise RuntimeError(f"Browser died during startup: {e}") from e
```

- [ ] **Step 6: oracle.py — reader death handler**

Modül seviyesinde handler (browser_start'ta her yeni engine'e bağlanır, `oracle.py:228-230` event callback'lerinin yanına):

```python
async def _on_engine_death() -> None:
    global _current_engine
    if _current_engine is not None:
        logger.warning("browser engine died — clearing state")
        _current_engine = None
        clear_state()

# browser_start içinde (event collector kayıtlarının yanına):
await _current_engine.on_death(_on_engine_death)
```

`on_death` callback'i sync de olabilir (Task 2'de hem sync hem coroutine destekleniyor) — `_on_engine_death` async yaz, Task 2'nin create_task dalı onu çalıştırır.

- [ ] **Step 7: Testleri çalıştır, GREEN doğrula**

Run: `uv run pytest tests/test_phantom.py tests/test_oracle.py tests/test_oracle_phase3.py -q`
Expected: PASS — yeni 3 + mevcut oracle testleri.

- [ ] **Step 8: Commit**

```bash
git add kahin/oracle.py tests/test_phantom.py
git commit -m "fix(oracle): liveness-aware engine state; no more phantom 'started'"
```

---

### Task 4: Healer — gerçek bağlama (bind_engine/bind_state)

**Files:**
- Modify: `kahin/oracle.py` (healer bind + restart yardımcısı)
- Modify: `kahin/_healer.py:150-154, 194-205` (bind API'sine uyum sağla — gerekirse)
- Test: `tests/test_phantom.py` (healer RESTART_ENGINE senaryosu)

**Interfaces:**
- Consumes: `_healer.bind_engine(engine_ref)`, `_healer.bind_state(state_ref)` (`_healer.py:150-154`), `RecoveryAction.RESTART_ENGINE` dalı (`_healer.py:194-205`).
- Produces: RESTART_ENGINE gerçekten stop eder; state'te ölü engine varsa `kahin_browser_start` otomatik yeniden başlatma için hazır olur (browser_start Task 3'te zaten ölü engine'i değiştiriyor).

- [ ] **Step 1: Failing test yaz**

`tests/test_phantom.py` sonuna:

```python
@pytest.mark.asyncio
async def test_healer_restart_action_clears_engine(monkeypatch):
    """RESTART_ENGINE action: engine.stop() çağrılır, state temizlenir."""
    # oracle modülünü import et; _healer.safe ile bilerek hata üret (ör. dead engine + timeout)
    # sonra RecoveryAction.RESTART_ENGINE tetiklendiğini ve engine stop edildiğini doğrula
```

Not: `_healer.py:194-205` zaten stop + state temizleme yapıyor — sorun `bind_engine`/`bind_state` hiç çağrılmıyor (rapor: research-a C.3). Test, bind edilmiş ref'lerin RESTART_ENGINE'de gerçekten kullanıldığını doğrular.

- [ ] **Step 2: Testi çalıştır, RED doğrula**

Run: `uv run pytest tests/test_phantom.py::test_healer_restart_action_clears_engine -q`
Expected: FAIL — bind çağrısı yok, healer ref'leri null.

- [ ] **Step 3: oracle.py — bind çağrıları**

Oracle modül başlatma bölgesinde (healer init'in olduğu yerde):

```python
_engine_holder: dict[str, Any] = {"_current_engine": None}
_healer_ref.bind_state(_engine_holder)
```

`_current_engine` global'ini `_engine_holder["_current_engine"]`'e taşı (tüm kullanım yerleri: `oracle.py:185, 201-202, 206, 209, 218, 221, 224, 228, 233, 264, 266-267, 415, 424` + `_safe_cdp:164` + `_require_engine:184` + yeni Task 3 kodları). `bind_engine` için: `_healer_ref.bind_engine(_engine_holder)` da geçerli (RESTART_ENGINE `_engine_ref.stop()` çağırıyor — holder dict'te stop yok; bu yüzden `bind_engine`'e doğrudan holder DEĞİL: küçük bir wrapper veya RESTART_ENGINE dalına `_state_ref["_current_engine"]` üzerinden stop ekle).

En az invaziv: `_healer.py:194-205` RESTART_ENGINE dalını şöyle güncelle (state holder'dan engine'i al, stop et, None yap):

```python
if action == RecoveryAction.RESTART_ENGINE:
    if self._state_ref is not None:
        engine = self._state_ref.get("_current_engine")
        if engine is not None:
            try:
                if hasattr(engine, "stop"):
                    await engine.stop()
            except Exception:
                pass
            self._state_ref["_current_engine"] = None
    self._notify(...)
```

`bind_engine` eski dalı koru (geriye uyumluluk) ama artık state holder esas yol.

- [ ] **Step 4: Testi çalıştır, GREEN doğrula**

Run: `uv run pytest tests/test_phantom.py tests/test_healer* -q` (healer testleri varsa)
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add kahin/oracle.py kahin/_healer.py tests/test_phantom.py
git commit -m "fix(healer): bind engine state; RESTART_ENGINE really stops the engine"
```

---

### Task 5: E2E + regresyon — gerçek Camoufox'ta phantom yok

**Files:**
- Modify: `tests/test_mirage_ipc.py` (gerçek e2e kill testi)
- Modify: `camoufox-harness/core/tests.zig` (gerekirse — Task 1 testlerinin tamamlayıcısı)
- Run: tam regresyon

**Interfaces:**
- Consumes: Task 1-4.

- [ ] **Step 1: Gerçek Camoufox kill testi yaz**

`tests/test_mirage_ipc.py` sonuna (mevcut `test_mirage_full_flow_real_camoufox` deseninde):

```python
@pytest.mark.asyncio
async def test_browser_kill_mid_session_detected():
    """Browser SIGKILL sonrasi: komut hizli hata dondurur (30s asilma YOK)."""
    engine = Mirage()
    await engine.start()
    try:
        # browser PID'i: sidecar'in child'i. process group kill yerine
        # _camoufox_bin'in child PID'ini al: psutil YOK — /proc taramasi veya
        # sidecar'a "Browser.health" -> pid cevabinda olan pid'i kullan (Task 1).
        health = await engine.send_cdp("Browser", "health")
        pid = health["pid"]
        os.kill(pid, signal.SIGKILL)
        start = time.monotonic()
        with pytest.raises(RuntimeError):
            await engine.send_cdp("Runtime", "evaluate", {"expression": "1"})
        elapsed = time.monotonic() - start
        assert elapsed < 15.0, f"expected fast failure, took {elapsed:.1f}s"
    finally:
        await engine.stop()
```

Dikkat: `Browser.health`'ın `pid` alanı sidecar'ın child PID'i (browser ana process). SIGKILL sonrası sidecar (Task 1 ile) çıkar → reader EOF → "Mirage sidecar exited" hızlı hata. `engine.stop()` temiz kapanışı bozmamalı (zaten ölü process'lerde hızlıdır).

- [ ] **Step 2: Testi çalıştır (gerçek Camoufox)**

Run: `uv run pytest tests/test_mirage_ipc.py::test_browser_kill_mid_session_detected -q`
Expected: PASS — hızlı hata (<15s), RuntimeError.

- [ ] **Step 3: Tam regresyon**

Run:
1. `uv run pytest tests/ -q` — 102+ yeni test, hepsi yeşil
2. `zig build test` (camoufox-harness/core) — 176+ yeni, yeşil
3. `bash scripts/build-sidecar.sh` — vendor binary güncel
4. `ruff check kahin/ camoufox-harness/` (mevcut pre-existing hatalar hariç yeni 0)

- [ ] **Step 4: Commit**

```bash
git add tests/test_mirage_ipc.py
git commit -m "test(mirage): real Camoufox kill mid-session — fast failure verified"
```

---

## Self-Review

- **Spec kapsamı:** Kullanıcı raporu (sorunlar/s.md): "çalışıyor sanıyor ama süreç yok" → Task 3 (liveness) + Task 2 (boot doğrulama). "Stop'u tekrar deniyorum" → Task 3 reader-death state temizliği + Task 4. 30s asılma (research-a C.1) → Task 1 (sidecar çıkışı) + Task 5 (kill testi). Healer inert (research-a C.3) → Task 4. Health dead-code (research-a A.4) → Task 1. Test boşlukları (research-a D) → Task 1-5 testleri.
- **Placeholder taraması:** Tüm test ve kod adımları somut; sadece shadow.py is_alive implementasyonu ve driver.zig Instance erişimi "mevcut koda bak" bırakıldı — çünkü implementer o dosyaları görecek; davranış ve imza plan'da tanımlı.
- **Tip tutarlılığı:** `is_alive() -> bool` tüm görevlerde aynı; `on_death(cb)` Task 2 tanımlı, Task 3 tüketiyor; `Browser.health` Task 1 üretiyor, Task 2/5 tüketiyor; `_engine_holder` Task 4 tanımlı, Task 3'teki `_current_engine` global'inin taşındığı yer.
