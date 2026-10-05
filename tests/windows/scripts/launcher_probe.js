#!/usr/bin/env node
// launcher(src/launcher.py)をMCPクライアントが行うのと同じstdio経路で駆動する
// 小さなドライバ。Claude Code自身がNode.jsプロセスからlauncherをchild_process
// で起動するため、そのstdioの扱いに合わせてNodeから検証する。
//
// 使い方: node launcher_probe.js <config.jsonのパス>
// configは以下の形の JSON:
//   {
//     command, args, cwd, env: {...},
//     steps: [{name, method, id?, params?, expectResponse, expectErrorCode?, timeoutMs?}],
//     responseTimeoutMs, exitTimeoutMs
//   }
// 標準出力の最終行に実行結果を1個のJSONで書き出す。プロセス自体は常にexit 0。
'use strict';

const { spawn } = require('child_process');
const fs = require('fs');

function main() {
  const configPath = process.argv[2];
  const config = JSON.parse(fs.readFileSync(configPath, 'utf8'));
  const {
    command,
    args,
    cwd,
    env,
    steps,
    responseTimeoutMs = 15000,
    exitTimeoutMs = 30000,
  } = config;

  const child = spawn(command, args, { cwd, env, stdio: ['pipe', 'pipe', 'pipe'] });

  let stdoutBuf = '';
  let stderrBuf = '';
  const pending = [];
  let exited = false;
  let exitCode = null;
  let exitSignal = null;

  child.stdout.on('data', (chunk) => {
    stdoutBuf += chunk.toString('utf8');
    let idx;
    while ((idx = stdoutBuf.indexOf('\n')) >= 0) {
      const line = stdoutBuf.slice(0, idx);
      stdoutBuf = stdoutBuf.slice(idx + 1);
      if (!line.trim()) continue;
      const waiter = pending.shift();
      if (!waiter) continue;
      try {
        waiter.resolve(JSON.parse(line));
      } catch (e) {
        waiter.reject(new Error('launcher stdout line is not valid JSON: ' + line));
      }
    }
  });
  child.stderr.on('data', (chunk) => {
    stderrBuf += chunk.toString('utf8');
  });
  child.on('exit', (code, sig) => {
    exited = true;
    exitCode = code;
    exitSignal = sig;
  });
  child.on('error', (err) => {
    stderrBuf += `\n[spawn error] ${err.message}\n`;
  });

  function waitForResponse(timeoutMs) {
    return new Promise((resolve, reject) => {
      const waiter = { resolve: null, reject: null };
      const timer = setTimeout(() => {
        const i = pending.indexOf(waiter);
        if (i >= 0) pending.splice(i, 1);
        reject(new Error(`timed out after ${timeoutMs}ms waiting for a response line`));
      }, timeoutMs);
      waiter.resolve = (v) => {
        clearTimeout(timer);
        resolve(v);
      };
      waiter.reject = (e) => {
        clearTimeout(timer);
        reject(e);
      };
      pending.push(waiter);
    });
  }

  function waitForExit(timeoutMs) {
    return new Promise((resolve, reject) => {
      if (exited) return resolve({ code: exitCode, signal: exitSignal });
      const timer = setTimeout(() => {
        reject(new Error(`launcher did not exit within ${timeoutMs}ms`));
      }, timeoutMs);
      child.once('exit', (code, sig) => {
        clearTimeout(timer);
        resolve({ code, signal: sig });
      });
    });
  }

  (async () => {
    const result = { ok: true, stage: 'start', error: null, responses: [] };
    try {
      for (const step of steps) {
        result.stage = step.name || step.method;
        const msg = { jsonrpc: '2.0', method: step.method };
        if (step.id !== undefined) msg.id = step.id;
        if (step.params !== undefined) msg.params = step.params;
        child.stdin.write(JSON.stringify(msg) + '\n');
        if (step.expectResponse) {
          const resp = await waitForResponse(step.timeoutMs || responseTimeoutMs);
          result.responses.push({ step: result.stage, response: resp });
          const errCode = resp && resp.error && resp.error.code;
          if (step.expectErrorCode !== undefined) {
            if (errCode !== step.expectErrorCode) {
              throw new Error(
                `step "${result.stage}": expected error code ${step.expectErrorCode}, got ${JSON.stringify(resp)}`
              );
            }
          } else if (resp && resp.error) {
            throw new Error(`step "${result.stage}": unexpected error response ${JSON.stringify(resp)}`);
          }
        }
      }
      result.stage = 'stdin-close';
      child.stdin.end();
      result.stage = 'wait-exit';
      const { code, signal } = await waitForExit(exitTimeoutMs);
      result.exitCode = code;
      result.exitSignal = signal;
      result.stage = 'done';
    } catch (e) {
      result.ok = false;
      result.error = String((e && e.message) || e);
    } finally {
      if (!exited) {
        try {
          child.kill('SIGKILL');
        } catch (_) {
          /* noop */
        }
      }
      result.stderr = stderrBuf;
      process.stdout.write(JSON.stringify(result) + '\n');
      process.exit(0);
    }
  })();
}

main();
