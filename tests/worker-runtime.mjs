import assert from 'node:assert/strict';
import { after, afterEach, before, beforeEach } from 'node:test';
import { readdir, readFile } from 'node:fs/promises';
import { resolve } from 'node:path';
import { Miniflare, convertV4MiniflareOptions } from 'miniflare';
import { unstable_getMiniflareWorkerOptions } from 'wrangler';
import { Telegram, token, username } from './telegram-fake.mjs';

export const telegram = new Telegram();
export const model = { enabled: false, probability: 0, profile: null, state: null, response: null };
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
    bindings: { ...workerOptions.bindings, BOT_TOKEN: token, TELEGRAM_WEBHOOK_SECRET: secret, BOT_USERNAME: username,
      ...(model.enabled ? { EXPERIENTIAL_API_KEY: 'test-model-key' } : {}) },
    outboundService: async request => {
      if (request.url === 'https://api.experientiallabs.ai/v1/systemone') {
        assert.equal(request.method, 'POST');
        assert.equal(request.headers.get('authorization'), 'Bearer test-model-key');
        const body = await request.json();
        assert.equal(body.model, 'jev-latest');
        assert.equal(body.questions.spam.type, 'noul');
        assert.equal(typeof body.questions.spam.instructions, 'string');
        model.state = body.state;
        return model.response ?? Response.json({ model: 'jev-latest', answers: {
          spam: { type: 'noul', noul: model.probability },
        } });
      }
      if (request.url.endsWith(`/bot${token}/getChat`)) {
        assert.equal(request.method, 'POST');
        const { chat_id } = await request.json();
        assert.ok(Number.isSafeInteger(chat_id) && chat_id > 0);
        if (model.profile instanceof Response) return model.profile.clone();
        return Response.json({ ok: true, result: { id: chat_id, type: 'private', ...model.profile } });
      }
      return telegram.fetch(request);
    },
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
  Object.assign(model, { probability: 0, profile: null, state: null, response: null });
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
