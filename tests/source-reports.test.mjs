import assert from 'node:assert/strict';
import { test } from 'node:test';
import { database, dispatch, setBindings, telegram } from './worker-runtime.mjs';
import { chat, message, report } from './telegram-fake.mjs';

async function inlineReport(text = '/bs') {
  telegram.members.set(777, { status: 'member' });
  for (const [id, sender] of [[70, 22], [71, 777], [72, 11]]) {
    const history = message(id, sender);
    history.from.is_bot = sender === 777;
    telegram.send(history);
    await dispatch({ message: history });
  }
  const target = { ...message(), via_bot: { id: 777, is_bot: true } };
  const update = report(target);
  update.message.text = text;
  update.message.entities = [{ type: 'bot_command', offset: 0, length: text.split(/\s+/u)[0].length }];
  telegram.send(target);
  telegram.send(update.message);
  return update;
}

test('user: Given an inline message, When a reporter replies with a mention only, Then only the actual sender is banned and cleaned', async () => {
  const command = await inlineReport();
  const update = report(command.message.reply_to_message);
  telegram.send(update.message);
  assert.equal((await dispatch(update)).status, 200);
  for (const id of [70, 81, 82]) assert.equal(telegram.has(id), false);
  for (const id of [71, 72]) assert.equal(telegram.has(id), true);
  assert.equal(telegram.canJoin(22), false);
  assert.equal(telegram.canJoin(777), true);
  assert.deepEqual((await database.prepare('SELECT user_id FROM blacklisted_users').all()).results, [{ user_id: 22 }]);
  assert.equal(await database.prepare('SELECT source_id FROM blacklisted_sources WHERE source_id=777').first(), null);
});

for (const command of ['/bs', '/bs@test_gate_bot']) {
  test(`user: Given an inline message without a bot username, When a reporter replies with ${command}, Then both accounts are banned and cleaned and the bot is registered`, async () => {
    const update = await inlineReport(command);
    assert.equal((await dispatch(update)).status, 200);
    for (const id of [70, 71, 81, 82]) assert.equal(telegram.has(id), false);
    assert.equal(telegram.has(72), true);
    for (const id of [22, 777]) assert.equal(telegram.canJoin(id), false);
    assert.deepEqual((await database.prepare('SELECT user_id FROM blacklisted_users ORDER BY user_id').all()).results, [{ user_id: 22 }, { user_id: 777 }]);
    assert.equal((await database.prepare('SELECT source_id FROM blacklisted_sources WHERE source_id=777').first()).source_id, 777);
    assert.match(telegram.replies.at(-1).text, /777/);
    assert.equal((await dispatch(update)).status, 200);
    for (const id of [22, 777]) assert.equal(telegram.canJoin(id), false);
  });
}

test('user: Given a failed source ban after sender cleanup, When delivery retries after manually unbanning the sender, Then only the pending source is banned and command cleanup finishes', async () => {
  const update = await inlineReport();
  telegram.faults.set('banChatMember:777', () => Response.json({ ok: false, error_code: 429 }, { status: 429 }));
  assert.equal((await dispatch(update)).status, 503);
  assert.equal(telegram.canJoin(22), false);
  assert.equal(telegram.canJoin(777), true);
  assert.equal(telegram.has(70), false);
  assert.equal(telegram.has(71), true);
  assert.equal(telegram.has(82), true);
  telegram.members.set(22, { status: 'left' });
  telegram.faults.clear();
  assert.equal((await dispatch(update)).status, 200);
  assert.equal(telegram.canJoin(22), true);
  assert.equal(telegram.canJoin(777), false);
  assert.equal(telegram.has(71), false);
  assert.equal(telegram.has(82), false);
  assert.equal(telegram.has(72), true);
});

for (const protectedId of [22, 777]) {
  test(`user: Given administrator ${protectedId} in an inline report, When a reporter replies with bs, Then only the other account is banned and its history cleared`, async () => {
    const update = await inlineReport();
    telegram.members.set(protectedId, { status: 'administrator' });
    assert.equal((await dispatch(update)).status, 200);
    assert.equal(telegram.has(81), false);
    assert.equal(telegram.has(82), false);
    for (const [id, history] of [[22, 70], [777, 71]]) {
      assert.equal(telegram.canJoin(id), id === protectedId);
      assert.equal(telegram.has(history), id === protectedId);
    }
    assert.deepEqual(telegram.members.get(protectedId), { status: 'administrator' });
  });
}

test('user: Given a listed ordinary member, When they reply with bs, Then the source is registered but neither account is punished', async () => {
  const update = await inlineReport();
  telegram.members.set(11, { status: 'member' });
  assert.equal((await dispatch(update)).status, 200);
  for (const id of [70, 71, 81]) assert.equal(telegram.has(id), true);
  for (const id of [22, 777]) assert.equal(telegram.canJoin(id), true);
  assert.equal((await database.prepare('SELECT source_id FROM blacklisted_sources WHERE source_id=777').first()).source_id, 777);
  assert.equal((await database.prepare('SELECT COUNT(*) AS n FROM blacklisted_users').first()).n, 0);
});

test('user: Given an unlisted sender-chat identity, When a reply uses bs with an allowed compatibility user, Then neither blacklist changes', async () => {
  const update = await inlineReport();
  update.message.sender_chat = { ...chat, id: -10099 };
  assert.equal((await dispatch(update)).status, 200);
  for (const id of [70, 71, 81, 82]) assert.equal(telegram.has(id), true);
  assert.equal(await database.prepare('SELECT source_id FROM blacklisted_sources WHERE source_id=777').first(), null);
  assert.equal((await database.prepare('SELECT COUNT(*) AS n FROM reports').first()).n, 0);
});

test('user: Given a pending combined report, When reporter access is revoked before retry, Then the pending source and command remain untouched', async () => {
  const update = await inlineReport();
  telegram.faults.set('banChatMember:777', () => Response.json({ ok: false, error_code: 429 }, { status: 429 }));
  assert.equal((await dispatch(update)).status, 503);
  telegram.faults.clear();
  await setBindings({ REPORTER_IDS: '' });
  try {
    assert.equal((await dispatch(update)).status, 200);
    assert.equal(telegram.canJoin(777), true);
    assert.equal(telegram.has(71), true);
    assert.equal(telegram.has(82), true);
  } finally {
    await setBindings({});
  }
});

test('user: Given missing or invalid inline provenance, When a reporter replies with bs, Then a usage reply replaces punishment and source registration', async () => {
  for (const via_bot of [undefined, { id: 777, is_bot: false }, { id: -777, is_bot: true }, { id: true, is_bot: true }]) {
    const update = await inlineReport();
    update.message.reply_to_message.via_bot = via_bot;
    assert.equal((await dispatch(update)).status, 200);
    for (const id of [70, 71, 81]) assert.equal(telegram.has(id), true);
    for (const id of [22, 777]) assert.equal(telegram.canJoin(id), true);
    assert.equal(await database.prepare('SELECT source_id FROM blacklisted_sources WHERE source_id=777').first(), null);
    assert.ok(telegram.replies.at(-1).text.length > 0);
  }
});

test('user: Given an inline message, When a reporter replies with an empty username argument, Then the command shows usage without registering or punishing either account', async () => {
  const update = await inlineReport('/bs @');
  assert.equal((await dispatch(update)).status, 200);
  for (const id of [70, 71, 81]) assert.equal(telegram.has(id), true);
  for (const id of [22, 777]) assert.equal(telegram.canJoin(id), true);
  assert.equal(telegram.has(82), false);
  assert.equal(await database.prepare('SELECT source_id FROM blacklisted_sources WHERE source_id=777').first(), null);
  assert.match(telegram.replies.at(-1).text, /Could not resolve/);
});

test('user: Given a reply target from a different group, When a reporter uses bs, Then no source is registered or account punished', async () => {
  const update = await inlineReport();
  update.message.reply_to_message.chat = { ...chat, id: -10099 };
  assert.equal((await dispatch(update)).status, 400);
  for (const id of [22, 777]) assert.equal(telegram.canJoin(id), true);
  assert.equal(await database.prepare('SELECT source_id FROM blacklisted_sources WHERE source_id=777').first(), null);
});
