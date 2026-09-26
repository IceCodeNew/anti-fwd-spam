import assert from 'node:assert/strict';
import { test } from 'node:test';
import { database, dispatch, model, runtime, setBindings as setModelKeys, telegram } from './worker-runtime.mjs';
import { message } from './telegram-fake.mjs';

const keys = ['TYPESAFE_AI_API_KEY', 'EXPERIENTIAL_API_KEY', 'OPENCODE_API_KEY', 'CMD_API_KEY', 'AI_GATEWAY_API_KEY'];
const credentials = ['test-typesafe-key', 'test-model-key', 'test-opencode-key', 'test-commandcode-key', 'test-gateway-key'];
const configured = names => Object.fromEntries(names.map(name => [name, credentials[keys.indexOf(name)]]));
const pending = () => database.prepare('SELECT * FROM model_tasks WHERE message_id = 81').first();
// The webhook attempt can finish a few seconds after dispatch; later retries follow the scheduled clock exactly.
const WEBHOOK_SLACK = 5;
async function dispatchNow(update) {
  const sent = Math.floor(Date.now() / 1000);
  await dispatch(update);
  return sent;
}
async function tick(now) {
  telegram.now = now;
  await (await runtime.getWorker()).scheduled({ scheduledTime: new Date(now * 1000), cron: '* * * * *' });
}

for (const key of keys) {
  test(`user: Given only ${key}, When Jev returns a threshold score, Then the sender is muted and only the current message disappears`, async () => {
    await setModelKeys(configured([key]));
    model.probability = 0.95;
    telegram.send(message());
    telegram.send(message(80));
    assert.equal((await dispatch({ update_id: 1, message: message() })).status, 200);
    assert.equal(telegram.has(81), false);
    assert.equal(telegram.has(80), true);
    assert.equal(telegram.canJoin(22), true);
    assert.equal(telegram.canSend(22), false);
  });
}

test('user: Given four configured providers, When the first three reject requests, Then the fourth can classify after 1, 2 and 5 minutes', async () => {
  await setModelKeys(configured(keys.slice(0, 4)));
  model.response = url => url === 'https://api.commandcode.ai/provider/v1/systemone'
    ? Response.json({ answers: { spam: { type: 'noul', noul: 0.96 } } })
    : new Response('rejected', { status: 401 });
  telegram.send(message());
  telegram.send(message(80));
  const sent = await dispatchNow({ update_id: 1, message: message() });
  await tick(sent + 59);
  assert.equal(telegram.has(81), true);
  let now = sent + 60 + WEBHOOK_SLACK;
  await tick(now);
  for (const delay of [120, 300]) {
    now += delay;
    await tick(now - 1);
    assert.equal(telegram.has(81), true);
    await tick(now);
  }
  assert.equal(telegram.has(81), false);
  assert.equal(telegram.has(80), true);
  assert.equal(telegram.canSend(22), false);
  assert.equal((await pending()).input_json, null);
});

test('user: Given all five provider keys, When the first four fail, Then the retry budget ends without consulting the fifth provider', async () => {
  await setModelKeys(configured(keys));
  const calls = [];
  model.response = url => {
    calls.push(url);
    return url.includes('ai-gateway.vercel.sh')
      ? Response.json({ answers: { spam: { type: 'boolean', probability: 1 } } })
      : new Response('unavailable', { status: 503 });
  };
  telegram.send(message());
  const sent = await dispatchNow({ update_id: 1, message: message() });
  await tick(sent + 59);
  assert.equal(calls.length, 1);
  let now = sent + 60 + WEBHOOK_SLACK;
  await tick(now);
  for (const delay of [120, 300]) {
    const attempts = calls.length;
    await tick(now + delay - 1);
    assert.equal(calls.length, attempts);
    now += delay;
    await tick(now);
  }
  await tick(now + 600);
  assert.equal(calls.length, 4);
  assert.equal(calls.some(url => url.includes('ai-gateway.vercel.sh')), false);
  assert.equal(telegram.has(81), true);
  assert.equal(telegram.canSend(22), true);
  assert.equal((await pending()).input_json, null);
});

test('user: Given a valid non-spam or invalid Experiential answer, When another provider would delete it, Then no further classification occurs', async () => {
  await setModelKeys(configured(['EXPERIENTIAL_API_KEY', 'AI_GATEWAY_API_KEY']));
  let id = 81;
  for (const answer of [{ type: 'noul', noul: 0.9499 }, { type: 'noul', noul: '0.99' },
    { type: 'boolean', probability: 1 }, { type: 'noul', noul: true }, { type: 'noul', noul: 1.1 }]) {
    model.response = url => Response.json({ answers: { spam: url.includes('api.experientiallabs.ai')
      ? answer : { type: 'boolean', probability: 1 } } });
    const target = message(id++);
    telegram.send(target);
    await dispatch({ update_id: id, message: target });
    await tick(target.date + 600);
    assert.equal(telegram.has(target.message_id), true);
  }
});

test('user: Given two unavailable providers, When the first recovers before the third attempt, Then rotation returns to it', async () => {
  await setModelKeys(configured(keys.slice(0, 2)));
  model.response = new Response('unavailable', { status: 503 });
  telegram.send(message());
  await dispatch({ update_id: 1, message: message() });
  await tick((await pending()).due_at);
  assert.equal(telegram.has(81), true);
  model.response = url => url.includes('api.typesafe.ai')
    ? Response.json({ answers: { spam: { type: 'noul', noul: 0.95 } } })
    : new Response('unavailable', { status: 503 });
  await tick((await pending()).due_at);
  assert.equal(telegram.has(81), false);
  assert.equal((await pending()).input_json, null);
});

test('user: Given pending inference, When every key is removed then a different provider is enabled, Then work pauses and resumes with the current credentials', async () => {
  await setModelKeys(configured(['AI_GATEWAY_API_KEY']));
  model.response = new Response('unavailable', { status: 503 });
  telegram.send(message());
  await dispatch({ update_id: 1, message: message() });
  const due = (await pending()).due_at;
  await setModelKeys({});
  await tick(due);
  assert.equal(telegram.has(81), true);
  await setModelKeys(configured(['OPENCODE_API_KEY']));
  model.response = null;
  model.probability = 0.95;
  await tick(due + 60);
  assert.equal(telegram.has(81), false);
});

test('user: Given a saved gateway spam decision, When the provider changes while deletion is pending, Then deletion completes without a new verdict', async () => {
  await setModelKeys(configured(['AI_GATEWAY_API_KEY']));
  model.probability = 0.95;
  telegram.faults.set('deleteMessage', () => new Response('unavailable', { status: 503 }));
  telegram.send(message());
  await dispatch({ update_id: 1, message: message() });
  const due = (await pending()).due_at;
  await setModelKeys(configured(['TYPESAFE_AI_API_KEY']));
  model.probability = 0;
  telegram.faults.clear();
  await tick(due);
  assert.equal(telegram.has(81), false);
});
