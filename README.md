# cdp-kahin-mcp

CDP bilgisine sahip, schema validation yapan, deterministik ve hybrid engine'li MCP server.

## Özellikler

- **CDP Schema Knowledge** — 56 domain, 667 command, 237 event gömülü
- **Schema Validation** — typo tespiti, required param kontrolü
- **Deterministik** — LLM bağımlı değil, her zaman aynı sonucu verir
- **Hybrid Engine** — Obscura (hızlı Chrome) + Mirage (stealth)
- **Pattern Learning** — kullanılan CDP pattern'larını öğrenir ve önerir
- **31 MCP Tool** — knowledge, validation, browser control, session, debug, pattern

## Kurulum

```bash
cd cdp-kahin-mcp
uv venv && source .venv/bin/activate
uv pip install -e .
```

## Kullanım

MCP istemcisine `kahin` server'ını ekleyin:

```json
{
  "mcpServers": {
    "kahin": {
      "command": "python3",
      "args": ["-m", "kahin.oracle"],
      "cwd": "/path/to/cdp-kahin-mcp"
    }
  }
}
```

## Tool'lar (31 adet)

| Kategori | Tool'lar |
|---|---|
| GRIMOIRE (CDP Bilgi) | list_domains, get_domain, get_command, get_event, find_concept, list_types, get_type |
| SERAPH (Doğrulama) | validate_command, error_decode, get_dependencies |
| PILOT (Browser) | browser_start, browser_stop, navigate, click, extract, screenshot, evaluate, execute_cdp |
| TRAINMAN (Session) | list_sessions, get_session, create_session, kill_session |
| DEJA_VU (Debug) | event_history, list_network_requests, get_console, iframe_tree |
| PROPHECY (Pattern) | pattern_learn, pattern_query, pattern_suggest, pattern_forget, pattern_stats |

## Mimari

```
oracle.py → the_source/architect.py (schema engine)
          → the_keymaker/ (tools)
          → the_twins/ (browser engines)
          → residual_self/fate.py (pattern DB)
```

## Geliştirme

```bash
pip install -e ".[dev]"
pytest tests/
```

## Port Uyarısı

Port 9222 (Chrome DevTools) ve 9240 (kusatma engine) RESERVED'dir. Kullanmayın.
