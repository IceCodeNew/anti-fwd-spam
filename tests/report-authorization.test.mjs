import assert from 'node:assert/strict';
import { test } from 'node:test';
import { database, dispatch, model, setBindings, telegram } from './worker-runtime.mjs';
import { chat, message, report } from './telegram-fake.mjs';

for (const allowed of ['', undefined, '22', '111']) {
  test(`user: Given reporter configuration ${JSON.stringify(allowed)}, When an unlisted owner reports, Then evidence and blacklist stay empty and messages remain`, async () => {
    await setBindings({ REPORTER_IDS: allowed, EXPERIENTIAL_API_KEY: 'test-model-key' });
    model.probability = 1;
    telegram.members.set(11, { status: 'creator' });
    const update = report();
    telegram.send(update.message);
    telegram.send(update.message.reply_to_message);
    const response = await dispatch(update);
    assert.equal(response.status, 200);
    assert.equal(telegram.has(81), true);
    assert.equal(telegram.has(82), true);
    assert.equal(telegram.canJoin(22), true);
    assert.equal(telegram.canSend(22), true);
    for (const table of ['reports', 'blacklisted_users']) {
      assert.equal((await database.prepare(`SELECT COUNT(*) AS count FROM ${table}`).first()).count, 0);
    }
  });
}

test('user: Given an unlisted member, When their reply mentioning the bot carries a campaign marker, Then the reply is deleted and they are muted without report evidence', async () => {
  await setBindings({ REPORTER_IDS: '11' });
  const update = report(message(80, 11));
  update.message.from = message(82, 22).from;
  update.message.text = '😀 @test_gate_bot @safdhifobot campaign_001';
  telegram.send(update.message);
  telegram.send(update.message.reply_to_message);
  assert.equal((await dispatch(update)).status, 200);
  assert.equal(telegram.has(82), false);
  assert.equal(telegram.has(80), true);
  assert.equal(telegram.canSend(22), false);
  assert.equal(telegram.canJoin(11), true);
  assert.equal((await database.prepare('SELECT COUNT(*) AS count FROM reports').first()).count, 0);
});

test('user: Given an allowed account sending anonymously, When its report contains a compatibility user ID, Then the group identity cannot authorize a report', async () => {
  await setBindings({ REPORTER_IDS: '11,1087968824' });
  const update = report();
  update.message.sender_chat = chat;
  telegram.send(update.message);
  telegram.send(update.message.reply_to_message);
  assert.equal((await dispatch(update)).status, 200);
  assert.equal(telegram.has(81), true);
  assert.equal(telegram.has(82), true);
  assert.equal(telegram.canJoin(22), true);
  assert.equal((await database.prepare('SELECT COUNT(*) AS count FROM reports').first()).count, 0);
});

test('user: Given multiple allowed reporters, When a listed administrator reports, Then the target and indexed history disappear and the account is blacklisted', async () => {
  await setBindings({ REPORTER_IDS: '33, 11,-10012,33' });
  telegram.send(message(70));
  await dispatch({ message: message(70) });
  const update = report();
  telegram.send(update.message.reply_to_message);
  telegram.send(update.message);
  assert.equal((await dispatch(update)).status, 200);
  for (const id of [70, 81, 82]) assert.equal(telegram.has(id), false);
  assert.equal(telegram.canJoin(22), false);
  assert.equal((await database.prepare('SELECT user_id FROM blacklisted_users').first()).user_id, 22);
});

test('user: Given a pending report, When the reporter loses allowlist access before redelivery, Then its target stays visible and no further punishment occurs', async () => {
  await setBindings({ REPORTER_IDS: '11' });
  const update = report();
  telegram.send(update.message.reply_to_message);
  telegram.faults.set('getChatMember:11', () => Response.json({ ok: false, error_code: 429 }, { status: 429 }));
  assert.equal((await dispatch(update)).status, 503);
  telegram.faults.clear();
  const saved = await database.prepare('SELECT * FROM reports').first();
  await setBindings({ REPORTER_IDS: '' });
  assert.equal((await dispatch(update)).status, 200);
  assert.equal(telegram.has(81), true);
  assert.equal(telegram.canJoin(22), true);
  assert.deepEqual(await database.prepare('SELECT * FROM reports').first(), saved);
});

for (const senderChat of [chat, { ...chat, id: -10099 }, { id: -10088, type: 'channel' }]) {
  test(`user: Given allowed sender chat identities, When a report is sent as ${senderChat.id}, Then only a listed identity can blacklist the target regardless of the destination group`, async () => {
    await setBindings({ REPORTER_IDS: `11,${chat.id},-10088` });
    const update = report();
    update.message.sender_chat = senderChat;
    update.message.from = { id: 1087968824, is_bot: true, first_name: 'Group' };
    telegram.send(update.message);
    telegram.send(update.message.reply_to_message);
    assert.equal((await dispatch(update)).status, 200);
    const accepted = senderChat.id !== -10099;
    assert.equal(telegram.has(81), !accepted);
    assert.equal(telegram.has(82), !accepted);
    assert.equal(telegram.canJoin(22), !accepted);
    assert.equal((await database.prepare('SELECT COUNT(*) AS count FROM blacklisted_users').first()).count, accepted ? 1 : 0);
  });
}

for (const status of ['member', 'kicked', 'administrator', 'creator']) {
  test(`user: Given a reported bot with ${status} membership, When an authorized administrator reports it, Then target deletion and indexed cleanup respect administrator protection`, async () => {
    await setBindings({ REPORTER_IDS: '11' });
    const history = message(70);
    history.from.is_bot = true;
    telegram.send(history);
    await dispatch({ message: history });
    telegram.send(message(71, 11));
    const target = message();
    target.from.is_bot = true;
    telegram.send(target);
    const update = report(target);
    telegram.send(update.message);
    telegram.members.set(22, { status });
    const protectedAccount = ['administrator', 'creator'].includes(status);
    assert.equal((await dispatch(update)).status, 200);
    assert.equal(telegram.has(81), false);
    assert.equal(telegram.has(82), false);
    assert.equal(telegram.has(70), protectedAccount);
    assert.equal(telegram.has(71), true);
    assert.equal(telegram.canJoin(22), protectedAccount);
    assert.equal(telegram.canSend(22), protectedAccount);
    assert.equal((await database.prepare('SELECT COUNT(*) AS count FROM blacklisted_users WHERE user_id=22').first()).count, protectedAccount ? 0 : 1);
    assert.equal((await dispatch(update)).status, 200);
    assert.equal(telegram.canJoin(22), protectedAccount);
    if (protectedAccount) assert.deepEqual(telegram.members.get(22), { status });
  });
}
