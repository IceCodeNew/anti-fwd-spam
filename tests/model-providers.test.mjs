import assert from 'node:assert/strict';
import { createHash } from 'node:crypto';
import { test } from 'node:test';
import { dispatch, model, outboundRequests, setBindings, task as pending, telegram, tick } from './worker-runtime.mjs';
import { message } from './telegram-fake.mjs';

const credentials = {
  TYPESAFE_AI_API_KEY: 'test-typesafe-key', EXPERIENTIAL_API_KEY: 'test-model-key',
  OPENCODE_API_KEY: 'test-opencode-key', CMD_API_KEY: 'test-commandcode-key',
  AI_GATEWAY_API_KEY: 'test-gateway-key', OPENAI_API_KEY: 'test-openai-key', OPENROUTER_API_KEY: 'test-openrouter-key',
};
const jev = [
  ['TYPESAFE_AI_API_KEY', 'https://api.typesafe.ai/v1/systemone'],
  ['EXPERIENTIAL_API_KEY', 'https://api.experientiallabs.ai/v1/systemone'],
  ['OPENCODE_API_KEY', 'https://opencode.ai/zen/v1/systemone'],
  ['CMD_API_KEY', 'https://api.commandcode.ai/provider/v1/systemone'],
  ['AI_GATEWAY_API_KEY', 'https://ai-gateway.vercel.sh/v4/ai/evaluation-model'],
];
const luna = [
  ['EXPERIENTIAL_API_KEY', 'https://api.experientiallabs.ai/v1/decisions'],
  ['OPENAI_API_KEY', 'https://api.openai.com/v1/decisions'],
  ['OPENROUTER_API_KEY', 'https://openrouter.ai/api/alpha/decisions'],
  ['AI_GATEWAY_API_KEY', 'https://ai-gateway.vercel.sh/v1/decisions'],
];
const configure = (backend, names) => setBindings({ MODEL_BACKEND: backend,
  ...Object.fromEntries(Object.keys(credentials).map(key => [key, names.includes(key) ? credentials[key] : undefined])),
});
const verdict = (url, probability) => Response.json({ answers: url.endsWith('/v1/decisions')
  ? [{ name: 'spam', type: 'predicate', probability }]
  : { spam: url.endsWith('/v4/ai/evaluation-model') ? { type: 'boolean', probability }
    : { type: 'noul', noul: probability } },
});

// Later attempts retain the original 1, 2 and 5 minute delays and share one four-attempt budget.
async function retrySchedule(calls, sent) {
  let due = (await pending()).due_at;
  assert.ok(due >= sent + 60 && due <= sent + 65);
  for (const delay of [120, 300, null]) {
    const attempts = calls.length;
    await tick(due - 1);
    assert.equal(calls.length, attempts);
    await tick(due);
    assert.equal(calls.length, attempts + 1);
    if (delay !== null) {
      assert.equal((await pending()).due_at, due + delay);
      due += delay;
    }
  }
  await tick(due + 600);
  assert.equal(calls.length, 4);
}

for (const [backend, providers] of [[undefined, jev], ['luna', luna]]) {
  for (const [key, url] of providers) {
    test(`user: Given only ${key} with ${backend ?? 'default Jev'}, When the provider returns a threshold score, Then only the current message is removed and its sender muted`, async () => {
      await configure(backend, [key]);
      const calls = [];
      model.response = endpoint => { calls.push(endpoint); return verdict(endpoint, 0.90); };
      telegram.send(message());
      telegram.send(message(80));
      assert.equal((await dispatch({ update_id: 1, message: message() })).status, 200);
      assert.deepEqual(calls, [url]);
      assert.equal(telegram.has(81), false);
      assert.equal(telegram.has(80), true);
      assert.equal(telegram.canJoin(22), true);
      assert.equal(telegram.canSend(22), false);
    });
  }
}

// Digests pin the pre-migration Jev prompts at 029997b, independently of current Python constants.
const originalJevPrompts = [
  '9fa98d3b7910c6c2cc2287424dab94e781e28049196e8d206071de6ef5d06293',
  'f9f946f6642c2cc7794fa851d3e48001bb5c5e3870e77d0a01fb8645d0a11f8a',
];
for (const backend of [undefined, '', 'jev']) {
  test(`user: Given MODEL_BACKEND=${backend === undefined ? 'unset' : JSON.stringify(backend)} and every provider key, When messages and joiners are classified, Then TypeSafe receives the original Jev prompts`, async () => {
    await configure(backend, Object.keys(credentials));
    const calls = [], prompts = [];
    model.response = (url, body) => {
      calls.push(url);
      const instructions = body.questions.spam?.instructions ?? body.questions[0]?.instructions;
      prompts.push(createHash('sha256').update(instructions).digest('hex'));
      return verdict(url, 0);
    };
    telegram.send(message());
    assert.equal((await dispatch({ update_id: 1, message: message() })).status, 200);
    const notice = { ...message(82, 11), new_chat_members: [message().from] };
    delete notice.text;
    telegram.send(notice);
    assert.equal((await dispatch({ update_id: 2, message: notice })).status, 200);
    assert.deepEqual(calls, [jev[0][1], jev[0][1]]);
    assert.deepEqual(prompts, originalJevPrompts);
    assert.equal(telegram.has(81), true);
    assert.equal(telegram.has(82), true);
    assert.equal(telegram.canJoin(22), true);
  });
}

for (const backend of ['unknown', 123]) {
  test(`user: Given invalid MODEL_BACKEND=${JSON.stringify(backend)}, When a webhook arrives, Then configuration fails before any external request`, async () => {
    await configure(backend, Object.keys(credentials));
    telegram.send(message());
    assert.equal((await dispatch({ update_id: 1, message: message() })).status, 500);
    assert.deepEqual(outboundRequests, []);
    assert.equal(telegram.has(81), true);
    assert.equal(await pending(), null);
  });
}

for (const [backend, keys] of [
  ['luna', ['TYPESAFE_AI_API_KEY', 'OPENCODE_API_KEY', 'CMD_API_KEY']],
  [undefined, ['OPENAI_API_KEY', 'OPENROUTER_API_KEY']],
]) {
  test(`user: Given ${backend ?? 'default Jev'} without its own credentials, When only the other backend has keys, Then classification is unavailable rather than cross-backend fallback`, async () => {
    await configure(backend, keys);
    const calls = [];
    model.response = url => { calls.push(url); return verdict(url, 1); };
    telegram.send(message());
    assert.equal((await dispatch({ update_id: 1, message: message() })).status, 200);
    await tick(Math.floor(Date.now() / 1000) + 3600);
    assert.deepEqual(calls, []);
    assert.equal(model.state, null);
    assert.equal(telegram.has(81), true);
    assert.equal(telegram.canSend(22), true);
    assert.equal((await pending())?.input_json ?? null, null);
  });
}

for (const [backend, providers] of [[undefined, jev], ['luna', luna]]) {
  for (const succeeds of [true, false]) {
    test(`user: Given every key with ${backend ?? 'default Jev'}, When the first ${succeeds ? 'three providers reject requests' : 'four providers fail'}, Then ${succeeds ? 'the fourth decides on the original retry schedule' : 'the fixed budget ends without extra or cross-backend attempts'}`, async () => {
      await configure(backend, Object.keys(credentials));
      const calls = [];
      model.response = url => {
        calls.push(url);
        return succeeds && url === providers[3][1] ? verdict(url, 0.96)
          : new Response('unavailable', { status: succeeds ? 401 : 503 });
      };
      telegram.send(message());
      telegram.send(message(80));
      const sent = Math.floor(Date.now() / 1000);
      assert.equal((await dispatch({ update_id: 1, message: message() })).status, 200);
      assert.deepEqual(calls, [providers[0][1]]);
      await retrySchedule(calls, sent);
      assert.deepEqual(calls, providers.slice(0, 4).map(([, url]) => url));
      assert.equal(telegram.has(81), !succeeds);
      assert.equal(telegram.has(80), true);
      assert.equal(telegram.canSend(22), !succeeds);
      assert.equal((await pending()).input_json, null);
    });
  }
}

for (const keys of [['OPENAI_API_KEY', 'OPENROUTER_API_KEY'], ['OPENROUTER_API_KEY', 'AI_GATEWAY_API_KEY']]) {
  test(`user: Given Luna ${keys.join(' then ')}, When the first configured provider fails, Then missing providers are skipped`, async () => {
    await configure('luna', [...keys, 'TYPESAFE_AI_API_KEY']);
    const calls = [];
    const urls = keys.map(key => luna.find(([name]) => name === key)[1]);
    model.response = url => {
      calls.push(url);
      return url === urls[0] ? new Response('unavailable', { status: 503 }) : verdict(url, 0.95);
    };
    telegram.send(message());
    await dispatch({ update_id: 1, message: message() });
    assert.deepEqual(calls, urls.slice(0, 1));
    await tick((await pending()).due_at);
    assert.deepEqual(calls, urls);
    assert.equal(telegram.has(81), false);
  });
}

test('user: Given a completed native non-spam or refused Luna answer, When later providers would punish, Then no moderation or fallback occurs', async () => {
  await configure('luna', Object.keys(credentials));
  let id = 81;
  for (const answer of [{ name: 'spam', type: 'predicate', probability: 0.8999 },
    { name: 'spam', type: 'refusal', refusal: 'Unable to decide' }]) {
    const calls = [];
    model.response = url => {
      calls.push(url);
      return url === luna[0][1] ? Response.json({ answers: [answer] }) : verdict(url, 1);
    };
    const target = message(id++);
    telegram.send(target);
    await dispatch({ update_id: id, message: target });
    await tick(Math.floor(Date.now() / 1000) + 3600);
    assert.equal(telegram.has(target.message_id), true);
    assert.equal(telegram.canSend(22), true);
    assert.equal(telegram.canJoin(22), true);
    assert.deepEqual(calls, [luna[0][1]]);
  }
});

test('user: Given a completed Jev non-spam or invalid answer, When another provider would punish, Then classification does not seek another verdict', async () => {
  await configure(undefined, ['EXPERIENTIAL_API_KEY', 'AI_GATEWAY_API_KEY']);
  let id = 81;
  for (const answer of [{ type: 'noul', noul: 0.8999 }, { type: 'noul', noul: '0.99' }]) {
    const calls = [];
    model.response = url => {
      calls.push(url);
      return url === jev[1][1] ? Response.json({ answers: { spam: answer } }) : verdict(url, 1);
    };
    const target = message(id++);
    telegram.send(target);
    await dispatch({ update_id: id, message: target });
    await tick(Math.floor(Date.now() / 1000) + 3600);
    assert.equal(telegram.has(target.message_id), true);
    assert.deepEqual(calls, [jev[1][1]]);
  }
});

test('user: Given two unavailable Luna providers, When the first recovers before the third attempt, Then rotation returns to it', async () => {
  await configure('luna', ['EXPERIENTIAL_API_KEY', 'OPENAI_API_KEY']);
  const calls = [];
  model.response = url => { calls.push(url); return new Response('unavailable', { status: 503 }); };
  telegram.send(message());
  await dispatch({ update_id: 1, message: message() });
  await tick((await pending()).due_at);
  assert.equal(telegram.has(81), true);
  model.response = url => { calls.push(url); return verdict(url, 0.95); };
  await tick((await pending()).due_at);
  assert.deepEqual(calls, [luna[0][1], luna[1][1], luna[0][1]]);
  assert.equal(telegram.has(81), false);
  assert.equal((await pending()).input_json, null);
});

test('user: Given pending Jev inference, When every key is removed then another provider is enabled, Then work pauses and resumes with current credentials', async () => {
  await configure(undefined, ['AI_GATEWAY_API_KEY']);
  model.response = new Response('unavailable', { status: 503 });
  telegram.send(message());
  await dispatch({ update_id: 1, message: message() });
  const due = (await pending()).due_at;
  await configure(undefined, []);
  await tick(due);
  assert.equal(telegram.has(81), true);
  await configure(undefined, ['OPENCODE_API_KEY']);
  model.response = null;
  model.probability = 0.90;
  await tick(due + 60);
  assert.equal(telegram.has(81), false);
});

test('user: Given a saved Jev gateway spam decision, When the provider changes while deletion is pending, Then deletion completes without a new verdict', async () => {
  await configure(undefined, ['AI_GATEWAY_API_KEY']);
  model.probability = 0.90;
  telegram.faults.set('deleteMessage', () => new Response('unavailable', { status: 503 }));
  telegram.send(message());
  await dispatch({ update_id: 1, message: message() });
  const due = (await pending()).due_at;
  await configure(undefined, ['TYPESAFE_AI_API_KEY']);
  const calls = [];
  model.response = url => { calls.push(url); return verdict(url, 0); };
  telegram.faults.clear();
  await tick(due);
  assert.equal(telegram.has(81), false);
  assert.deepEqual(calls, []);
});
