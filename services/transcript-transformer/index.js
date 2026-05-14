// Rewrites Claude Code JSONL transcripts from dark-factory workers so each
// workflow shows as a distinct project in claude-monitor.
//
// Workers all run with cwd=/workspace, and claude-monitor groups by cwd.
// We read raw transcripts from a per-workflow subpath of the shared volume,
// rewrite cwd to a short project name, and write to a flat cooked tree that
// claude-monitor watches at its default ~/.claude/projects path.
//
// Idempotent by construction: rewriting an already-rewritten cwd is a no-op,
// mtime cursor avoids redundant work, .tmp→rename keeps the watcher from
// seeing partial files.

import { readdir, mkdir, rename, rm, stat } from 'node:fs/promises';
import { createReadStream, createWriteStream } from 'node:fs';
import { createInterface } from 'node:readline';
import { join, relative, dirname } from 'node:path';
import { pathToFileURL } from 'node:url';

const RAW = process.env.RAW_ROOT ?? '/raw';
const COOKED = process.env.COOKED_ROOT ?? '/cooked';
const INTERVAL_MS = Number(process.env.WATCH_INTERVAL_S ?? 10) * 1000;
const ORIGINAL_CWD = process.env.ORIGINAL_CWD ?? '/workspace';
const ISSUE_WORKFLOW_PREFIX = 'df-issue-';

const cursor = new Map(); // dst path -> last mtimeMs processed

function sanitiseProjectName(value) {
  const cleaned = String(value ?? '')
    .trim()
    .replace(/[^A-Za-z0-9._-]+/g, '-')
    .replace(/-+/g, '-')
    .replace(/^-|-$/g, '');
  return cleaned || 'unknown-workflow';
}

export function projectNameForWorkflowId(wfId) {
  const raw = String(wfId ?? '').trim();
  const display = raw.startsWith(ISSUE_WORKFLOW_PREFIX)
    ? raw.slice(ISSUE_WORKFLOW_PREFIX.length)
    : raw;
  return sanitiseProjectName(display || raw);
}

export function projectCwdForWorkflowId(wfId) {
  return `/${projectNameForWorkflowId(wfId)}`;
}

export function claudeProjectDirForCwd(cwd) {
  return String(cwd).replaceAll('/', '-');
}

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

export async function rewriteJsonl(
  src,
  dst,
  wfId,
  { originalCwd = ORIGINAL_CWD } = {},
) {
  await mkdir(dirname(dst), { recursive: true });
  const tmp = `${dst}.tmp`;
  const out = createWriteStream(tmp);
  const rl = createInterface({
    input: createReadStream(src),
    crlfDelay: Infinity,
  });
  const newCwd = projectCwdForWorkflowId(wfId);
  for await (const line of rl) {
    if (!line.trim()) {
      out.write('\n');
      continue;
    }
    try {
      const obj = JSON.parse(line);
      if (obj && obj.cwd === originalCwd) obj.cwd = newCwd;
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

export async function tickOnce({
  rawRoot = RAW,
  cookedRoot = COOKED,
  originalCwd = ORIGINAL_CWD,
  cursorMap = cursor,
} = {}) {
  let workflows;
  try {
    workflows = await readdir(rawRoot, { withFileTypes: true });
  } catch (err) {
    if (err.code !== 'ENOENT') throw err;
    return;
  }
  for (const entry of workflows) {
    if (!entry.isDirectory()) continue;
    const wfId = entry.name;
    const sourceProjectDir = claudeProjectDirForCwd(originalCwd);
    const wsDir = join(rawRoot, wfId, 'projects', sourceProjectDir);
    const projectCwd = projectCwdForWorkflowId(wfId);
    const dstRoot = join(cookedRoot, claudeProjectDirForCwd(projectCwd));
    const legacyDstRoot = join(cookedRoot, `${sourceProjectDir}--${wfId}`);
    for await (const src of walkJsonl(wsDir)) {
      const rel = relative(wsDir, src);
      const dst = join(dstRoot, rel);
      let stats;
      try {
        stats = await stat(src);
      } catch {
        continue;
      }
      if (cursorMap.get(dst) === stats.mtimeMs) continue;
      try {
        await rewriteJsonl(src, dst, wfId, { originalCwd });
        cursorMap.set(dst, stats.mtimeMs);
      } catch (err) {
        console.error(`[transformer] failed to rewrite ${src}:`, err.message);
      }
    }
    if (legacyDstRoot !== dstRoot) {
      await rm(legacyDstRoot, { recursive: true, force: true });
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

if (process.argv[1] && import.meta.url === pathToFileURL(process.argv[1]).href) {
  console.log(
    `[transformer] watching ${RAW} → ${COOKED} every ${INTERVAL_MS / 1000}s ` +
      `(rewrite cwd ${ORIGINAL_CWD} → /<project_name>)`,
  );
  loop().catch(err => {
    console.error('[transformer] fatal:', err);
    process.exit(1);
  });
}
