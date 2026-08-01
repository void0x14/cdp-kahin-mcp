#!/usr/bin/env node
// kahin launcher — kahin MCP server'ı başlatır.
// Kurulum: pnpm add -g @kahinmcp/kahin

import { spawn } from "node:child_process";

const args = process.argv.slice(2);
const py = process.env.KAHIN_PY || "python3";
const child = spawn(py, ["-m", "kahin.oracle", ...args], { stdio: "inherit" });
child.on("exit", (code, signal) => {
  if (signal) process.kill(process.pid, signal);
  process.exit(code ?? 0);
});
