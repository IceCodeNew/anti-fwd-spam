import assert from 'node:assert/strict';
import { test } from 'node:test';
import { database, dispatch, model, runtime, telegram } from './worker-runtime.mjs';
import { message } from './telegram-fake.mjs';

model.enabled = true;

async function tick(now) {
  telegram.now = now;
  await (await runtime.getWorker()).scheduled({ scheduledTime: new Date(now * 1000), cron: '* * * * *' });
}

const task = () => database.prepare('SELECT * FROM model_tasks WHERE message_id = 81').first();

for (const stage of ['inference', 'deletion']) {
  test(`user: Given a temporary ${stage} failure, When time advances, Then retries wait 1, 2 and 5 minutes and stop`, async () => {
    model.probability = 1;
    if (stage === 'inference') model.response = new Response('unavailable', { status: 503 });
    else telegram.faults.set('deleteMessage', () => Response.json({ ok: false, error_code: 503 }, { status: 503 }));
    telegram.send(message());
    assert.equal((await dispatch({ update_id: 1, message: message() })).status, 200);
    const initial = await task();
    if (stage === 'inference') assert.ok(initial.input_json.includes('User 22'));
    else assert.equal(initial.input_json, null);
    let due = initial.due_at - 60;
    assert.ok(due >= initial.created_at);
    for (const delay of [60, 120, 300]) {
      due += delay;
      assert.equal((await task()).due_at, due);
      await tick(due - 1);
      assert.equal((await task()).due_at, due);
      await tick(due);
      assert.equal(telegram.has(81), true);
    }
    assert.equal((await task()).input_json, null);
    telegram.faults.clear();
    model.response = null;
    model.probability = 1;
    await tick(due + 3600);
    assert.equal(telegram.has(81), true);
    assert.equal((await task()).expires_at, initial.created_at + 3 * 86400);
    await tick(initial.created_at + 3 * 86400 - 1);
    assert.ok(await task());
    await tick(initial.created_at + 3 * 86400);
    assert.equal(await task(), null);
  });
}

test('user: Given a saved spam decision and failed deletion, When Telegram recovers, Then deletion resumes without reevaluation', async () => {
  model.probability = 1;
  telegram.faults.set('deleteMessage', () => Response.json({ ok: false, error_code: 429 }, { status: 429 }));
  telegram.send(message());
  assert.equal((await dispatch({ update_id: 2, message: message() })).status, 200);
  const pending = await task();
  assert.equal(pending.input_json, null);
  model.probability = 0;
  telegram.faults.clear();
  await dispatch({ update_id: 2, message: message() });
  assert.equal(telegram.has(81), true);
  await tick(pending.due_at);
  assert.equal(telegram.has(81), false);
  assert.equal(telegram.canSend(22), true);
  assert.equal(telegram.canJoin(22), true);
});

test('user: Given pending inference, When an edit arrives, Then the old content is cleared and never deleted', async () => {
  model.response = new Response('unavailable', { status: 503 });
  telegram.send(message());
  await dispatch({ update_id: 3, message: message() });
  const pending = await task();
  const edited = { ...message(), text: 'Corrected message' };
  telegram.send(edited);
  await dispatch({ update_id: 4, edited_message: edited });
  assert.equal((await task()).input_json, null);
  model.response = null;
  model.probability = 1;
  await tick(pending.due_at);
  await dispatch({ update_id: 3, message: message() });
  assert.equal(telegram.has(81), true);
});

for (const stage of ['inference', 'deletion']) {
  test(`user: Given pending ${stage} at the deletion age limit, When cron runs, Then content expires without deletion`, async () => {
    model.probability = 1;
    if (stage === 'inference') model.response = new Response('unavailable', { status: 503 });
    else telegram.faults.set('deleteMessage', () => Response.json({ ok: false, error_code: 503 }, { status: 503 }));
    const target = message();
    telegram.send(target);
    await dispatch({ update_id: 5, message: target });
    telegram.faults.clear();
    model.response = null;
    model.probability = 1;
    await tick(target.date + 48 * 3600);
    assert.equal(telegram.has(81), true);
    assert.equal((await task()).input_json, null);
  });
}

test('user: Given an edit during inference, When the stale spam answer arrives, Then the edited message remains', async () => {
  const started = Promise.withResolvers();
  const release = Promise.withResolvers();
  model.response = async () => {
    started.resolve();
    await release.promise;
    return Response.json({ answers: { spam: { type: 'noul', noul: 1 } } });
  };
  telegram.send(message());
  const delivery = dispatch({ update_id: 7, message: message() });
  await started.promise;
  try {
    const edited = { ...message(), text: 'Corrected message' };
    telegram.send(edited);
    assert.equal((await dispatch({ update_id: 8, edited_message: edited })).status, 200);
  } finally {
    release.resolve();
  }
  assert.equal((await delivery).status, 200);
  assert.equal(telegram.has(81), true);
  assert.equal((await task()).input_json, null);
});

test('user: Given an edit delivered before its original, When the original later arrives, Then stale content cannot trigger deletion', async () => {
  const edited = { ...message(), text: 'Corrected message' };
  telegram.send(edited);
  model.probability = 1;
  await dispatch({ update_id: 10, edited_message: edited });
  await dispatch({ update_id: 9, message: message() });
  assert.equal(telegram.has(81), true);
  assert.equal((await task()).input_json, null);
});

test('user: Given transient HTTP failures or permanent rejections, When the service recovers, Then only transient failures are retried', async () => {
  let id = 81;
  for (const status of [408, 429, 500, 502, 400, 401, 403, 422]) {
    const target = message(id++);
    model.response = new Response('failure', { status });
    telegram.send(target);
    await dispatch({ update_id: id, message: target });
    const saved = await database.prepare('SELECT * FROM model_tasks WHERE message_id = ?').bind(target.message_id).first();
    model.response = null;
    model.probability = 1;
    await tick(saved.due_at);
    assert.equal(telegram.has(target.message_id), ![408, 429, 500, 502].includes(status));
  }
});

test('user: Given a crashed leased inference, When its lease and retry delay expire, Then processing resumes and removes only its target', async () => {
  const now = Math.floor(Date.now() / 1000);
  telegram.send(message());
  await database.prepare(`INSERT INTO model_tasks
    (bot_id, chat_id, message_id, phase, input_json, attempts, generation, due_at, lease_until, stop_at, created_at, expires_at)
    VALUES (123, -10012, 81, 'classify', ?, 1, 1, ?, ?, ?, ?, ?)`)
    .bind(JSON.stringify({ nickname: 'User 22', bio: '', message: { text: 'spam' } }),
      now + 60, now + 30, message().date + 48 * 3600, now, now + 3 * 86400).run();
  model.probability = 1;
  await tick(now + 29);
  assert.equal(telegram.has(81), true);
  await tick(now + 59);
  assert.equal(telegram.has(81), true);
  await tick(now + 60);
  assert.equal(telegram.has(81), false);
  assert.equal((await task()).input_json, null);
});

test('user: Given a pending deletion, When the target is edited, Then cron does not remove the new version', async () => {
  model.probability = 1;
  telegram.faults.set('deleteMessage', () => Response.json({ ok: false, error_code: 500 }, { status: 500 }));
  telegram.send(message());
  await dispatch({ update_id: 11, message: message() });
  const pending = await task();
  telegram.faults.clear();
  const edited = { ...message(), text: 'Ordinary discussion' };
  telegram.send(edited);
  await dispatch({ update_id: 12, edited_message: edited });
  await tick(pending.due_at);
  assert.equal(telegram.has(81), true);
});

test('user: Given a temporary failure then a spam decision, When overlapping cron runs become due, Then only the target is removed', async () => {
  model.response = new Response('unavailable', { status: 503 });
  telegram.send(message());
  telegram.send(message(80));
  await dispatch({ update_id: 6, message: message() });
  const pending = await task();
  model.response = null;
  model.probability = 0.95;
  await Promise.all([tick(pending.due_at), tick(pending.due_at)]);
  assert.equal(telegram.has(81), false);
  assert.equal(telegram.has(80), true);
  assert.equal((await task()).input_json, null);
});

test('user: Given two due messages, When one cron runs, Then only one message is processed', async () => {
  model.response = new Response('unavailable', { status: 503 });
  for (const id of [81, 82]) {
    telegram.send(message(id));
    await dispatch({ update_id: id, message: message(id) });
  }
  const { due } = await database.prepare('SELECT MAX(due_at) AS due FROM model_tasks').first();
  model.response = null;
  model.probability = 1;
  await tick(due);
  assert.equal([telegram.has(81), telegram.has(82)].filter(Boolean).length, 1);
  await tick(due + 60);
  assert.equal(telegram.has(81), false);
  assert.equal(telegram.has(82), false);
});

test('user: Given a reclaimed task, When an earlier worker returns a stale spam answer, Then it cannot override the current decision', async () => {
  const firstStarted = Promise.withResolvers();
  const secondStarted = Promise.withResolvers();
  const firstReply = Promise.withResolvers();
  const secondReply = Promise.withResolvers();
  const responses = [
    async () => { firstStarted.resolve(); await firstReply.promise; return 1; },
    async () => { secondStarted.resolve(); await secondReply.promise; return 0; },
  ];
  model.response = async () => Response.json({ answers: { spam: { type: 'noul', noul: await responses.shift()() } } });
  telegram.send(message());
  const original = dispatch({ update_id: 30, message: message() });
  await firstStarted.promise;
  const reclaimed = tick((await task()).due_at);
  await secondStarted.promise;
  try {
    firstReply.resolve();
    assert.equal((await original).status, 200);
    assert.equal(telegram.has(81), true);
  } finally {
    firstReply.resolve();
    secondReply.resolve();
    await reclaimed;
  }
  assert.equal(telegram.has(81), true);
  assert.equal((await task()).input_json, null);
});

test('user: Given Telegram times out a deletion, When it recovers, Then the saved spam decision still removes the target', async () => {
  let id = 90;
  for (const [status, body] of [[408, 'Request Timeout'], [408, '{"ok":false,"error_code":408}'],
    [200, '{"ok":false,"error_code":408}']]) {
    const target = message(id++);
    model.probability = 1;
    telegram.send(target);
    telegram.faults.set('deleteMessage', () => new Response(body, { status }));
    assert.equal((await dispatch({ update_id: id, message: target })).status, 200);
    assert.equal(telegram.has(target.message_id), true);
    telegram.faults.clear();
    model.probability = 0;
    const saved = await database.prepare('SELECT due_at FROM model_tasks WHERE message_id = ?').bind(target.message_id).first();
    await tick(saved.due_at);
    assert.equal(telegram.has(target.message_id), false);
  }
});
