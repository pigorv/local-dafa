import assert from 'node:assert/strict';
import { mkdtemp, mkdir, readFile, writeFile } from 'node:fs/promises';
import { join } from 'node:path';
import { tmpdir } from 'node:os';
import test from 'node:test';

import {
  claudeProjectDirForCwd,
  projectCwdForWorkflowId,
  projectNameForWorkflowId,
  rewriteJsonl,
  tickOnce,
} from './index.js';

test('issue workflow project names start at the repo owner', () => {
  assert.equal(
    projectNameForWorkflowId('df-issue-pigorv-claude-monitor-dafa-1-run-1'),
    'pigorv-claude-monitor-dafa-1-run-1',
  );
  assert.equal(
    projectCwdForWorkflowId('df-issue-pigorv-claude-monitor-dafa-1-run-1'),
    '/pigorv-claude-monitor-dafa-1-run-1',
  );
  assert.equal(
    claudeProjectDirForCwd('/pigorv-claude-monitor-dafa-1-run-1'),
    '-pigorv-claude-monitor-dafa-1-run-1',
  );
});

test('non-issue workflow project names remain readable', () => {
  assert.equal(projectNameForWorkflowId('manual-run-1'), 'manual-run-1');
  assert.equal(projectCwdForWorkflowId('manual-run-1'), '/manual-run-1');
});

test('rewriteJsonl replaces only the original cwd', async () => {
  const root = await mkdtemp(join(tmpdir(), 'df-transformer-'));
  const src = join(root, 'input.jsonl');
  const dst = join(root, 'nested', 'output.jsonl');
  await writeFile(
    src,
    [
      JSON.stringify({ cwd: '/workspace', message: 'rewrite me' }),
      JSON.stringify({ cwd: '/already-short', message: 'leave me' }),
      '{not-json',
      '',
    ].join('\n'),
  );

  await rewriteJsonl(src, dst, 'df-issue-pigorv-claude-monitor-dafa-1-run-1');

  const lines = (await readFile(dst, 'utf8')).split('\n');
  assert.deepEqual(JSON.parse(lines[0]), {
    cwd: '/pigorv-claude-monitor-dafa-1-run-1',
    message: 'rewrite me',
  });
  assert.deepEqual(JSON.parse(lines[1]), {
    cwd: '/already-short',
    message: 'leave me',
  });
  assert.equal(lines[2], '{not-json');
  assert.equal(lines[3], '');
});

test('tickOnce writes the short cooked project and removes the legacy one', async () => {
  const root = await mkdtemp(join(tmpdir(), 'df-transformer-'));
  const rawRoot = join(root, 'raw');
  const cookedRoot = join(root, 'cooked');
  const wfId = 'df-issue-pigorv-claude-monitor-dafa-1-run-1';
  const sourceDir = join(rawRoot, wfId, 'projects', '-workspace');
  const legacyDir = join(cookedRoot, `-workspace--${wfId}`);
  await mkdir(sourceDir, { recursive: true });
  await mkdir(legacyDir, { recursive: true });
  await writeFile(
    join(sourceDir, 'session.jsonl'),
    `${JSON.stringify({ cwd: '/workspace', message: 'hello' })}\n`,
  );
  await writeFile(join(legacyDir, 'stale.jsonl'), '{}\n');

  await tickOnce({ rawRoot, cookedRoot, cursorMap: new Map() });

  const cooked = await readFile(
    join(cookedRoot, '-pigorv-claude-monitor-dafa-1-run-1', 'session.jsonl'),
    'utf8',
  );
  assert.deepEqual(JSON.parse(cooked.trim()), {
    cwd: '/pigorv-claude-monitor-dafa-1-run-1',
    message: 'hello',
  });
  await assert.rejects(readFile(join(legacyDir, 'stale.jsonl'), 'utf8'), {
    code: 'ENOENT',
  });
});
