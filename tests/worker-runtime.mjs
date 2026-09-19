import assert from 'node:assert/strict';
import { after, afterEach, before, beforeEach } from 'node:test';
import { readdir, readFile } from 'node:fs/promises';
import { resolve } from 'node:path';
import { Miniflare, convertV4MiniflareOptions } from 'miniflare';
import { unstable_getMiniflareWorkerOptions } from 'wrangler';
import { Telegram, token, username } from './telegram-fake.mjs';

export const telegram = new Telegram();
export let runtime, database;
const secret = 'test-secret';
export const headers = { 'content-type': 'application/json', 'x-telegram-bot-api-secret-token': secret };

before(async () => {
  const root = resolve('.wrangler/test-build');
  const paths = (await readdir(root, { recursive: true })).filter(path => path.endsWith('.py') && path !== 'entry.py');
  const { workerOptions } = unstable_getMiniflareWorkerOptions('wrangler.jsonc');
  runtime = new Miniflare(convertV4MiniflareOptions({
    ...workerOptions,
    modulesRoot: root,
    modules: ['entry.py', ...paths].map(path => ({ type: 'PythonModule', path: resolve(root, path) })),
    bindings: { ...workerOptions.bindings, BOT_TOKEN: token, TELEGRAM_WEBHOOK_SECRET: secret, BOT_USERNAME: username },
    outboundService: request => telegram.fetch(request),
  }));
  database = await runtime.getD1Database('REPORTS');
  for (const file of (await readdir('migrations')).filter(path => path.endsWith('.sql')).sort()) {
    for (const sql of (await readFile(`migrations/${file}`, 'utf8')).split(';').filter(sql => sql.trim())) {
      await database.prepare(sql).run();
    }
  }
});
after(async () => { await runtime?.dispose(); });
beforeEach(async () => {
  telegram.reset();
  await database.prepare('DELETE FROM reports').run();
  await database.prepare('DELETE FROM recent_messages').run();
  await database.prepare('DELETE FROM automatic_mutes').run();
  await database.prepare('DELETE FROM blacklisted_users').run();
});
afterEach(() => { assert.deepEqual(telegram.violations, []); });

export function dispatch(update, options = {}) {
  return runtime.dispatchFetch('https://worker.test/webhook', {
    method: 'POST', headers, body: JSON.stringify(update), ...options,
  });
}
