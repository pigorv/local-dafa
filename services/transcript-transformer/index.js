// Rewrites Claude Code JSONL transcripts from dark-factory workers so each
// workflow shows as a distinct project in claude-monitor.
//
// Workers all run with cwd=/workspace, and claude-monitor groups by cwd.
// We read raw transcripts from a per-workflow subpath of the shared volume,
// rewrite cwd to /workspace--<wf_id>, and write to a flat cooked tree that
// claude-monitor watches at its default ~/.claude/projects path.
//
// Idempotent by construction: rewriting an already-rewritten cwd is a no-op,
// mtime cursor avoids redundant work, .tmp→rename keeps the watcher from
// seeing partial files.

import { readdir, mkdir, rename, stat } from 'node:fs/promises';
import { createReadStream, createWriteStream } from 'node:fs';
import { createInterface } from 'node:readline';
import { join, relative, dirname } from 'node:path';

const RAW = process.env.RAW_ROOT ?? '/raw';
const COOKED = process.env.COOKED_ROOT ?? '/cooked';
const INTERVAL_MS = Number(process.env.WATCH_INTERVAL_S ?? 10) * 1000;
const ORIGINAL_CWD = process.env.ORIGINAL_CWD ?? '/workspace';

const cursor = new Map(); // dst path -> last mtimeMs processed

async function* walkJsonl(dir) {
  let entries;
  try {
    entries = await readdir(dir, { withFileTypes: true });
  } catch {
    return;
  }
  for (const entry of entries) {
    const full = join(dir, entry.name);
    if (entry.isDirectory()) yield* walkJsonl(full);
    else if (entry.isFile() && entry.name.endsWith('.jsonl')) yield full;
  }
}

async function rewriteJsonl(src, dst, wfId) {
  await mkdir(dirname(dst), { recursive: true });
  const tmp = `${dst}.tmp`;
  const out = createWriteStream(tmp);
  const rl = createInterface({
    input: createReadStream(src),
    crlfDelay: Infinity,
  });
  const newCwd = `${ORIGINAL_CWD}--${wfId}`;
  for await (const line of rl) {
    if (!line.trim()) {
      out.write('\n');
      continue;
    }
    try {
      const obj = JSON.parse(line);
      if (obj && obj.cwd === ORIGINAL_CWD) obj.cwd = newCwd;
      out.write(JSON.stringify(obj) + '\n');
    } catch {
      // Pass corrupt/non-JSON lines through unchanged so we don't drop data.
      out.write(line + '\n');
    }
  }
  await new Promise((resolve, reject) => {
    out.end(err => (err ? reject(err) : resolve()));
  });
  await rename(tmp, dst);
}

async function tickOnce() {
  let workflows;
  try {
    workflows = await readdir(RAW, { withFileTypes: true });
  } catch (err) {
    if (err.code !== 'ENOENT') throw err;
    return;
  }
  for (const entry of workflows) {
    if (!entry.isDirectory()) continue;
    const wfId = entry.name;
    const wsDir = join(RAW, wfId, 'projects', '-workspace');
    const dstRoot = join(COOKED, `-workspace--${wfId}`);
    for await (const src of walkJsonl(wsDir)) {
      const rel = relative(wsDir, src);
      const dst = join(dstRoot, rel);
      let stats;
      try {
        stats = await stat(src);
      } catch {
        continue;
      }
      if (cursor.get(dst) === stats.mtimeMs) continue;
      try {
        await rewriteJsonl(src, dst, wfId);
        cursor.set(dst, stats.mtimeMs);
      } catch (err) {
        console.error(`[transformer] failed to rewrite ${src}:`, err.message);
      }
    }
  }
}

async function loop() {
  while (true) {
    const start = Date.now();
    try {
      await tickOnce();
    } catch (err) {
      console.error('[transformer] tick failed:', err);
    }
    const elapsed = Date.now() - start;
    const wait = Math.max(0, INTERVAL_MS - elapsed);
    await new Promise(r => setTimeout(r, wait));
  }
}

console.log(
  `[transformer] watching ${RAW} → ${COOKED} every ${INTERVAL_MS / 1000}s ` +
    `(rewrite cwd ${ORIGINAL_CWD} → ${ORIGINAL_CWD}--<wf_id>)`,
);
loop().catch(err => {
  console.error('[transformer] fatal:', err);
  process.exit(1);
});
