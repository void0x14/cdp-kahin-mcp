#!/usr/bin/env node
// kahin global launcher — gömülü wheel'den kahin MCP server'ı çalıştırır.
// Kurulum: pnpm add -g @kahinmcp/kahin  (postinstall venv + wheel kurar)

import { spawn } from "node:child_process";
import { existsSync } from "node:fs";
import { homedir } from "node:os";
import { join } from "node:path";

const HOME = process.env.KAHIN_HOME || join(homedir(), ".local", "share", "kahin");
const VENV = join(HOME, "venv");
const PY = process.platform === "win32" ? join(VENV, "Scripts", "python.exe") : join(VENV, "bin", "python");

function sh(cmd, args, opts = {}) {
  return new Promise((resolve) => {
    const c = spawn(cmd, args, { stdio: ["ignore", "ignore", "pipe"], ...opts });
    c.on("error", (err) => {
      process.stderr.write(`[kahin] ${cmd} başlatılamadı: ${err.message}\n`);
      resolve(1);
    });
    c.stderr.on("data", (d) => process.stderr.write(`[kahin] ${d}`));
    c.on("close", (code) => resolve(code));
  });
}

async function setup() {
  // venv yoksa gömülü wheel'den kur (postinstall atlanmışsa / bozuksa)
  const { installPython } = await import("./setup.mjs");
  const res = await installPython();
  if (res && res.error) {
    process.stderr.write(`[kahin] python kurulumu başarısız (çıkış ${res.error})\n`);
    process.exit(1);
  }
  const python = existsSync(PY) ? PY : (process.env.KAHIN_PY || "python3");
  const check = await sh(python, ["-c", "import kahin"], { stdio: ["ignore", "pipe", "pipe"] });
  if (check !== 0) {
    process.stderr.write(`[kahin] kahin Python paketi bulunamadı. ${python} ile çalıştırılamıyor.\n`);
    process.exit(1);
  }
  return python;
}

const args = process.argv.slice(2);

if (args[0] === "setup") {
  const { setup: registerClients } = await import("./setup.mjs");
  registerClients();
  const { installPython } = await import("./setup.mjs");
  const install = await installPython();
  if (install && install.error) {
    process.stderr.write(`[kahin] Python/Camoufox kurulumu başarısız (çıkış ${install.error})\n`);
    process.exit(1);
  }
  process.exit(0);
}

const python = await setup();
const child = spawn(python, ["-m", "kahin.oracle", ...args], { stdio: "inherit" });
child.on("exit", (code, signal) => {
  if (signal) process.kill(process.pid, signal);
  process.exit(code ?? 0);
});
