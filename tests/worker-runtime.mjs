import assert from 'node:assert/strict';
import { after, afterEach, before, beforeEach } from 'node:test';
import { readdir, readFile } from 'node:fs/promises';
import { resolve } from 'node:path';
import { Miniflare, convertV4MiniflareOptions } from 'miniflare';
import { unstable_getMiniflareWorkerOptions } from 'wrangler';
import { Telegram, token, username } from './telegram-fake.mjs';

export const telegram = new Telegram();
export const outboundRequests = [];
export const model = { enabled: false, probability: 0, profile: null, profiles: new Map(), state: null, response: null };
export let runtime, database;
let runtimeOptions;
let customBindings = false;
const defaultBindings = () => ({ ...runtimeOptions.bindings,
  ...(model.enabled ? { EXPERIENTIAL_API_KEY: 'test-model-key' } : {}) });
const secret = 'test-secret';
export const headers = { 'content-type': 'application/json', 'x-telegram-bot-api-secret-token': secret };

before(async () => {
  const root = resolve('.wrangler/test-build');
  const paths = (await readdir(root, { recursive: true })).filter(path => path.endsWith('.py') && path !== 'entry.py');
  const { workerOptions } = unstable_getMiniflareWorkerOptions('wrangler.jsonc');
  // Local secrets must not enable providers outside a scenario's fake configuration.
  for (const key of ['MODEL_BACKEND', 'TYPESAFE_AI_API_KEY', 'EXPERIENTIAL_API_KEY', 'OPENCODE_API_KEY',
    'CMD_API_KEY', 'AI_GATEWAY_API_KEY', 'OPENAI_API_KEY', 'OPENROUTER_API_KEY']) {
    delete workerOptions.bindings[key];
  }
  runtimeOptions = {
    ...workerOptions,
    modulesRoot: root,
    modules: ['entry.py', ...paths].map(path => ({ type: 'PythonModule', path: resolve(root, path) })),
    bindings: { ...workerOptions.bindings, BOT_TOKEN: token, TELEGRAM_WEBHOOK_SECRET: secret, BOT_USERNAME: username,
      REPORTER_IDS: '11,-10012' },
    outboundService: async request => {
      outboundRequests.push(request.url);
      const providers = {
        'https://api.experientiallabs.ai/v1/decisions': ['gpt-6-luna-decisions', 'test-model-key'],
        'https://api.openai.com/v1/decisions': ['gpt-6-luna', 'test-openai-key'],
        'https://openrouter.ai/api/alpha/decisions': ['openai/gpt-6-luna-decisions', 'test-openrouter-key'],
        'https://ai-gateway.vercel.sh/v1/decisions': ['openai/gpt-6-luna-decisions', 'test-gateway-key'],
      };
      if (Object.hasOwn(providers, request.url)) {
        const [modelId, apiKey] = providers[request.url];
        assert.equal(request.method, 'POST');
        assert.equal(request.headers.get('authorization'), `Bearer ${apiKey}`);
        const body = await request.json();
        assert.equal(body.model, modelId);
        const decisions = request.url.endsWith('/v1/decisions');
        if (decisions) {
          assert.equal(body.questions.length, 1);
          assert.equal(body.questions[0].name, 'spam');
          assert.equal(body.questions[0].type, 'predicate');
          assert.equal(typeof body.questions[0].instructions, 'string');
          assert.equal(typeof body.input, 'string');
          model.state = JSON.parse(body.input);
          assert.equal(body.state, undefined);
        } else {
          assert.equal(body.questions.spam.type, 'noul');
          assert.equal(typeof body.questions.spam.instructions, 'string');
          model.state = body.state;
          assert.equal(body.input, undefined);
        }
        if (typeof model.response === 'function') return model.response(request.url, body);
        return model.response ?? Response.json({ answers: decisions
          ? [{ name: 'spam', type: 'predicate', probability: model.probability }]
          : { spam: { type: 'noul', noul: model.probability } },
        });
      }
      if (request.url.endsWith(`/bot${token}/getChat`)) {
        assert.equal(request.method, 'POST');
        const { chat_id } = await request.clone().json();
        if (typeof chat_id === 'string') return telegram.fetch(request);
        assert.ok(Number.isSafeInteger(chat_id) && chat_id > 0);
        const profile = model.profiles.get(chat_id) ?? model.profile;
        if (profile instanceof Response) return profile.clone();
        return Response.json({ ok: true, result: { id: chat_id, type: 'private', ...profile } });
      }
      return telegram.fetch(request);
    },
  };
  runtime = new Miniflare(convertV4MiniflareOptions({ ...runtimeOptions, bindings: defaultBindings() }));
  database = await runtime.getD1Database('REPORTS');
  for (const file of (await readdir('migrations')).filter(path => path.endsWith('.sql')).sort()) {
    for (const sql of (await readFile(`migrations/${file}`, 'utf8')).split(';').filter(sql => sql.trim())) {
      await database.prepare(sql).run();
    }
  }
});
after(async () => { await runtime?.dispose(); });
beforeEach(async () => {
  if (customBindings) {
    // Each scenario starts from the file's default configuration.
    await runtime.setOptions(convertV4MiniflareOptions({ ...runtimeOptions, bindings: defaultBindings() }));
    database = await runtime.getD1Database('REPORTS');
    customBindings = false;
  }
  telegram.reset();
  outboundRequests.length = 0;
  Object.assign(model, { probability: 0, profile: null, profiles: new Map(), state: null, response: null });
  await database.prepare('DELETE FROM reports').run();
  await database.prepare('DELETE FROM recent_messages').run();
  await database.prepare('DELETE FROM member_activity').run();
  await database.prepare('DELETE FROM automatic_mutes').run();
  await database.prepare('DELETE FROM blacklisted_users').run();
  await database.prepare('DELETE FROM blacklisted_sticker_sets').run();
  await database.prepare('DELETE FROM blacklisted_stickers').run();
  await database.prepare('DELETE FROM model_tasks').run();
  await database.prepare('DELETE FROM blacklisted_sources').run();
  await database.prepare('INSERT INTO blacklisted_sources (source_id) VALUES (7788)').run();
});
afterEach(() => { assert.deepEqual(telegram.violations, []); });

export async function setBindings(bindings) {
  customBindings = true;
  const configured = Object.fromEntries(Object.entries({ ...defaultBindings(), ...bindings })
    .filter(([, value]) => value !== undefined));
  await runtime.setOptions(convertV4MiniflareOptions({ ...runtimeOptions, bindings: configured }));
  database = await runtime.getD1Database('REPORTS');
}

export async function tick(now) {
  telegram.now = now;
  await (await runtime.getWorker()).scheduled({ scheduledTime: new Date(now * 1000), cron: '* * * * *' });
}

export const task = () => database.prepare('SELECT * FROM model_tasks WHERE message_id = 81').first();

// The Worker checks biographies and runs classification after its response; Miniflare cannot await that work.
export async function dispatch(update, options = {}) {
  const response = await runtime.dispatchFetch('https://worker.test/webhook', {
    method: 'POST', headers, body: JSON.stringify(update), ...options,
  });
  const buffered = new Response(await response.arrayBuffer(), {
    status: response.status, statusText: response.statusText, headers: response.headers,
  });
  await backgroundTasksSettled();
  return buffered;
}

// A task is still running while it is unclaimed or its lease is active.
async function backgroundTasksSettled() {
  for (let polls = 0; polls < 1000; polls++) {
    const busy = await database.prepare("SELECT 1 FROM model_tasks WHERE phase != 'done' "
      + 'AND (attempts = 0 OR lease_until > unixepoch()) LIMIT 1').first();
    if (!busy) return;
    await new Promise(resolve => setTimeout(resolve, 10));
  }
  assert.fail('background model task did not settle');
}
