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
let runtimeOptions;
const secret = 'test-secret';
export const headers = { 'content-type': 'application/json', 'x-telegram-bot-api-secret-token': secret };

before(async () => {
  const root = resolve('.wrangler/test-build');
  const paths = (await readdir(root, { recursive: true })).filter(path => path.endsWith('.py') && path !== 'entry.py');
  const { workerOptions } = unstable_getMiniflareWorkerOptions('wrangler.jsonc');
  runtimeOptions = {
    ...workerOptions,
    modulesRoot: root,
    modules: ['entry.py', ...paths].map(path => ({ type: 'PythonModule', path: resolve(root, path) })),
    bindings: { ...workerOptions.bindings, BOT_TOKEN: token, TELEGRAM_WEBHOOK_SECRET: secret, BOT_USERNAME: username,
      REPORTER_IDS: '11,-10012' },
    outboundService: async request => {
      const providers = {
        'https://api.experientiallabs.ai/v1/systemone': ['jev-latest', 'test-model-key'],
        'https://api.typesafe.ai/v1/systemone': ['jev-latest', 'test-typesafe-key'],
        'https://opencode.ai/zen/v1/systemone': ['jev-1.13', 'test-opencode-key'],
        'https://ai-gateway.vercel.sh/v4/ai/evaluation-model': ['typesafe-ai/jev', 'test-gateway-key'],
        'https://api.commandcode.ai/provider/v1/systemone': ['typesafe/jev', 'test-commandcode-key'],
      };
      if (Object.hasOwn(providers, request.url)) {
        const [modelId, apiKey] = providers[request.url];
        assert.equal(request.method, 'POST');
        assert.equal(request.headers.get('authorization'), `Bearer ${apiKey}`);
        const body = await request.json();
        const gateway = request.url.includes('ai-gateway.vercel.sh');
        if (gateway) {
          assert.equal(request.headers.get('ai-model-id'), modelId);
          assert.equal(request.headers.get('ai-gateway-protocol-version'), '0.0.1');
          assert.equal(request.headers.get('ai-gateway-auth-method'), 'api-key');
          assert.equal(request.headers.get('ai-evaluation-model-specification-version'), '4');
          assert.equal(body.model, undefined);
        } else assert.equal(body.model, modelId);
        assert.equal(body.questions.spam.type, gateway ? 'boolean' : 'noul');
        assert.equal(typeof body.questions.spam.instructions, 'string');
        model.state = body.state;
        if (typeof model.response === 'function') return model.response(request.url);
        return model.response ?? Response.json({ model: 'jev-latest', answers: {
          spam: gateway ? { type: 'boolean', probability: model.probability } : { type: 'noul', noul: model.probability },
        } });
      }
      if (request.url.endsWith(`/bot${token}/getChat`)) {
        assert.equal(request.method, 'POST');
        const { chat_id } = await request.clone().json();
        if (typeof chat_id === 'string') return telegram.fetch(request);
        assert.ok(Number.isSafeInteger(chat_id) && chat_id > 0);
        if (model.profile instanceof Response) return model.profile.clone();
        return Response.json({ ok: true, result: { id: chat_id, type: 'private', ...model.profile } });
      }
      return telegram.fetch(request);
    },
  };
  runtime = new Miniflare(convertV4MiniflareOptions({ ...runtimeOptions, bindings: { ...runtimeOptions.bindings,
    ...(model.enabled ? { EXPERIENTIAL_API_KEY: 'test-model-key' } : {}) } }));
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
  await database.prepare('DELETE FROM model_tasks').run();
  await database.prepare('DELETE FROM blacklisted_sources').run();
  await database.prepare('INSERT INTO blacklisted_sources (source_id) VALUES (273234066)').run();
});
afterEach(() => { assert.deepEqual(telegram.violations, []); });

export async function setBindings(bindings) {
  const configured = Object.fromEntries(Object.entries({ ...runtimeOptions.bindings, ...bindings })
    .filter(([, value]) => value !== undefined));
  await runtime.setOptions(convertV4MiniflareOptions({ ...runtimeOptions, bindings: configured }));
  database = await runtime.getD1Database('REPORTS');
}

export function dispatch(update, options = {}) {
  return runtime.dispatchFetch('https://worker.test/webhook', {
    method: 'POST', headers, body: JSON.stringify(update), ...options,
  });
}
