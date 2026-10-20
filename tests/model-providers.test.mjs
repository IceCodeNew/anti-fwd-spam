import assert from 'node:assert/strict';
import { createHash } from 'node:crypto';
import { test } from 'node:test';
import { dispatch, model, outboundRequests, setBindings, task as pending, telegram, tick } from './worker-runtime.mjs';
import { message } from './telegram-fake.mjs';

const credentials = {
  EXPERIENTIAL_API_KEY: 'test-model-key', OPENAI_API_KEY: 'test-openai-key',
  OPENROUTER_API_KEY: 'test-openrouter-key', AI_GATEWAY_API_KEY: 'test-gateway-key',
};
const providers = [
  ['EXPERIENTIAL_API_KEY', 'https://api.experientiallabs.ai/v1/decisions'],
  ['OPENAI_API_KEY', 'https://api.openai.com/v1/decisions'],
  ['OPENROUTER_API_KEY', 'https://openrouter.ai/api/alpha/decisions'],
  ['AI_GATEWAY_API_KEY', 'https://ai-gateway.vercel.sh/v1/decisions'],
];
const configure = names => setBindings(
  Object.fromEntries(Object.keys(credentials).map(key => [key, names.includes(key) ? credentials[key] : undefined])),
);
const verdict = (url, probability) => Response.json({ answers: url.endsWith('/v1/decisions')
  ? [{ name: 'spam', type: 'predicate', probability }]
  : { spam: { type: 'noul', noul: probability } },
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

for (const [key, url] of providers) {
  test(`user: Given only ${key}, When Luna returns a threshold score, Then only the current message is removed and its sender muted`, async () => {
    await configure([key]);
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

// Digests pin independently saved shared message/join prompt bytes, not current Python constants.
const sharedPrompts = [
  '73fef19406fca477cf90053be832fb23189f903d49fb409e4dd69fe18040cbd4',
  'b3b4c9cb4612e3308aa3d3e4fba90412b9e74f785b846618ea3eed63d7311fc7',
];
for (const [key, url] of [providers[0], providers[2]]) {
  test(`user: Given ${key}, When messages and joiners are classified, Then Luna receives the shared message and joining-profile prompts without conditional hints`, async () => {
    await configure([key]);
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
    assert.deepEqual(calls, [url, url]);
    assert.deepEqual(prompts, sharedPrompts);
    assert.equal(telegram.has(81), true);
    assert.equal(telegram.has(82), true);
    assert.equal(telegram.canJoin(22), true);
  });
}

test('user: Given only retired Jev credentials, When a message arrives, Then no external model is contacted and no moderation occurs', async () => {
  await setBindings({ TYPESAFE_AI_API_KEY: 'test-typesafe-key', OPENCODE_API_KEY: 'test-opencode-key',
    CMD_API_KEY: 'test-commandcode-key' });
  telegram.send(message());
  assert.equal((await dispatch({ update_id: 1, message: message() })).status, 200);
  await tick(Math.floor(Date.now() / 1000) + 3600);
  assert.deepEqual(outboundRequests.filter(url => new URL(url).hostname !== 'api.telegram.org'), []);
  assert.equal(model.state, null);
  assert.equal(telegram.has(81), true);
  assert.equal(telegram.canSend(22), true);
  assert.equal(telegram.canJoin(22), true);
  assert.equal((await pending())?.input_json ?? null, null);
});

for (const succeeds of [true, false]) {
  test(`user: Given every Luna key, When the first ${succeeds ? 'three providers reject requests' : 'four providers fail'}, Then ${succeeds ? 'the fourth decides on the original retry schedule' : 'the fixed budget ends without extra attempts'}`, async () => {
    await configure(Object.keys(credentials));
    const calls = [];
    model.response = url => {
      calls.push(url);
      if (succeeds && url === providers[3][1]) return verdict(url, 0.96);
      if (succeeds && url === providers[0][1]) return new Response('free_limit_reached', { status: 429 });
      return new Response('unavailable', { status: succeeds ? 401 : 503 });
    };
    telegram.send(message());
    telegram.send(message(80));
    const sent = Math.floor(Date.now() / 1000);
    assert.equal((await dispatch({ update_id: 1, message: message() })).status, 200);
    assert.deepEqual(calls, [providers[0][1]]);
    await retrySchedule(calls, sent);
    assert.deepEqual(calls, providers.map(([, url]) => url));
    assert.equal(telegram.has(81), !succeeds);
    assert.equal(telegram.has(80), true);
    assert.equal(telegram.canSend(22), !succeeds);
    assert.equal((await pending()).input_json, null);
  });
}

for (const keys of [['OPENAI_API_KEY', 'OPENROUTER_API_KEY'], ['OPENROUTER_API_KEY', 'AI_GATEWAY_API_KEY']]) {
  test(`user: Given Luna ${keys.join(' then ')}, When the first configured provider fails, Then missing providers are skipped`, async () => {
    await configure(keys);
    const calls = [];
    const urls = keys.map(key => providers.find(([name]) => name === key)[1]);
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

for (const [key, url, answers] of [
  ['EXPERIENTIAL_API_KEY', providers[0][1], [
    [{ name: 'spam', type: 'predicate', probability: 0.8999 }],
    [{ name: 'spam', type: 'refusal', refusal: 'Unable to decide' }],
    [{ name: 'spam', type: 'predicate', probability: '0.99' }],
  ]],
  ['OPENROUTER_API_KEY', providers[2][1], [
    { spam: { type: 'noul', noul: 0.8999 } },
    { spam: { type: 'refusal', refusal: 'Unable to decide' } },
    { spam: { type: 'noul', noul: '0.99' } },
  ]],
]) {
  test(`user: Given a completed non-spam, refused or invalid Luna answer from ${key}, When a later provider would punish, Then no moderation or fallback occurs`, async () => {
    await configure([key, 'AI_GATEWAY_API_KEY']);
    let id = 81;
    for (const answer of answers) {
      const calls = [];
      model.response = endpoint => {
        calls.push(endpoint);
        return endpoint === url ? Response.json({ answers: answer }) : verdict(endpoint, 1);
      };
      const target = message(id++);
      telegram.send(target);
      await dispatch({ update_id: id, message: target });
      await tick(Math.floor(Date.now() / 1000) + 3600);
      assert.equal(telegram.has(target.message_id), true);
      assert.equal(telegram.canSend(22), true);
      assert.equal(telegram.canJoin(22), true);
      assert.deepEqual(calls, [url]);
    }
  });
}

test('user: Given two unavailable Luna providers, When the first recovers before the third attempt, Then rotation returns to it', async () => {
  await configure(['EXPERIENTIAL_API_KEY', 'OPENAI_API_KEY']);
  const calls = [];
  model.response = url => { calls.push(url); return new Response('unavailable', { status: 503 }); };
  telegram.send(message());
  await dispatch({ update_id: 1, message: message() });
  await tick((await pending()).due_at);
  assert.equal(telegram.has(81), true);
  model.response = url => { calls.push(url); return verdict(url, 0.95); };
  await tick((await pending()).due_at);
  assert.deepEqual(calls, [providers[0][1], providers[1][1], providers[0][1]]);
  assert.equal(telegram.has(81), false);
  assert.equal((await pending()).input_json, null);
});

test('user: Given pending Luna inference, When every key is removed then another provider is enabled, Then work pauses and resumes with current credentials', async () => {
  await configure(['AI_GATEWAY_API_KEY']);
  const calls = [];
  model.response = url => { calls.push(url); return new Response('unavailable', { status: 503 }); };
  telegram.send(message());
  await dispatch({ update_id: 1, message: message() });
  const initial = await pending();
  await configure([]);
  await tick(initial.due_at);
  assert.equal(telegram.has(81), true);
  assert.deepEqual(calls, [providers[3][1]]);
  assert.equal((await pending()).attempts, initial.attempts);
  await configure(['OPENAI_API_KEY']);
  model.response = url => { calls.push(url); return verdict(url, 0.90); };
  await tick(initial.due_at + 60);
  assert.deepEqual(calls, [providers[3][1], providers[1][1]]);
  assert.equal(telegram.has(81), false);
});

test('user: Given a saved Luna gateway spam decision, When the provider changes while deletion is pending, Then deletion completes without a new verdict', async () => {
  await configure(['AI_GATEWAY_API_KEY']);
  model.probability = 0.90;
  telegram.faults.set('deleteMessage', () => new Response('unavailable', { status: 503 }));
  telegram.send(message());
  await dispatch({ update_id: 1, message: message() });
  const due = (await pending()).due_at;
  await configure(['EXPERIENTIAL_API_KEY']);
  const calls = [];
  model.response = url => { calls.push(url); return verdict(url, 0); };
  telegram.faults.clear();
  await tick(due);
  assert.equal(telegram.has(81), false);
  assert.equal(telegram.canSend(22), false);
  assert.deepEqual(calls, []);
});
