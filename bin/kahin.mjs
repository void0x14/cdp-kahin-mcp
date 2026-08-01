#!/usr/bin/env node
// kahin global launcher — kod tabanındaki kahin MCP server'ı çalıştırır.
// Kurulum: pnpm add -g @kahinmcp/kahin

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
    c.stderr.on("data", (d) => process.stderr.write(`[kahin] ${d}`));
    c.on("close", (code) => resolve(code));
  });
}

async function setup() {
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
  const { setup } = await import("./setup.mjs");
  setup();
  process.exit(0);
}

const python = await setup();
const child = spawn(python, ["-m", "kahin.oracle", ...args], { stdio: "inherit" });
child.on("exit", (code, signal) => {
  if (signal) process.kill(process.pid, signal);
  process.exit(code ?? 0);
});
