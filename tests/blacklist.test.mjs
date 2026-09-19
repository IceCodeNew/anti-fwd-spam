import assert from 'node:assert/strict';
import { test } from 'node:test';
import { chat, message, report } from './telegram-fake.mjs';
import { database, dispatch, runtime, telegram } from './worker-runtime.mjs';

test('user cleans indexed spam only: Given a blacklisted account with indexed and unindexed history, When a new message arrives, Then the sender cannot rejoin and only eligible indexed messages are explicitly cleared', async () => {
  await database.exec('INSERT INTO blacklisted_users VALUES (123, 22, 1)');
  await database.exec('INSERT INTO recent_messages VALUES (123, -10012, 70, 22, unixepoch() - 60)');
  for (const target of [message(70), message(71), message(72, 11), message(81)]) telegram.send(target);
  // A deployment without single-message deletion must still complete indexed cleanup.
  telegram.faults.set('deleteMessage', () => Response.json({ ok: false, error_code: 403 }, { status: 403 }));
  const update = { update_id: 901, message: message(81) };
  assert.equal((await dispatch(update)).status, 200);
  assert.equal(telegram.canJoin(22), false);
  assert.equal(telegram.has(70), false);
  assert.equal(telegram.has(81), false);
  assert.equal(telegram.has(71), true);
  assert.equal(telegram.has(72), true);
  telegram.members.set(22, { status: 'member' });
  assert.equal((await dispatch(update)).status, 200);
  assert.equal(telegram.canJoin(22), true);
  telegram.send(message(83));
  assert.equal((await dispatch({ update_id: 902, message: message(83) })).status, 200);
  assert.equal(telegram.canJoin(22), false);
  assert.equal(telegram.has(83), false);
});

test('user protects administrators and other bots: Given protected senders and an unrelated blacklist, When their messages arrive, Then membership and messages remain unchanged', async () => {
  await database.exec('INSERT INTO blacklisted_users VALUES (123, 11, 1), (123, 33, 1), (456, 22, 1)');
  telegram.members.set(33, { status: 'creator' });
  for (const id of [11, 33, 22]) {
    const target = message(id, id);
    telegram.send(target);
    assert.equal((await dispatch({ update_id: id, message: target })).status, 200);
    assert.equal(telegram.canJoin(id), true);
    assert.equal(telegram.has(id), true);
  }
});

test('user resumes indexed cleanup: Given a blacklisted inline sender and rejected batch deletion, When the update retries after manual unban, Then cleanup finishes without re-banning or falling back to single deletion', async () => {
  await database.exec('INSERT INTO blacklisted_users VALUES (123, 22, 1)');
  const target = { ...message(), via_bot: { id: 273234066, is_bot: true, first_name: 'Source' } };
  const update = { update_id: 901, message: target };
  telegram.send(target);
  telegram.faults.set('deleteMessages', () => Response.json({ ok: false, error_code: 429 }, { status: 429 }));
  assert.equal((await dispatch(update)).status, 503);
  assert.equal(telegram.canJoin(22), false);
  assert.equal(telegram.has(81), true);
  telegram.members.set(22, { status: 'left' });
  telegram.faults.clear();
  assert.equal((await dispatch(update)).status, 200);
  assert.equal(telegram.canJoin(22), true);
  assert.equal(telegram.has(81), false);
});

test('user does not pre-ban: Given a retained blacklist, When the bot gains group permissions, Then nobody is banned before sending a message', async () => {
  await database.exec('INSERT INTO blacklisted_users VALUES (123, 22, 1)');
  const user = { id: 123, is_bot: true, first_name: 'Moderator' };
  telegram.members.set(123, { status: 'administrator', can_restrict_members: true });
  assert.equal((await dispatch({ update_id: 1000, my_chat_member: {
    chat, from: message(1, 11).from, date: Math.floor(Date.now() / 1000),
    old_chat_member: { status: 'member', user },
    new_chat_member: { status: 'administrator', can_restrict_members: true, user },
  } })).status, 200);
  assert.equal(telegram.canJoin(22), true);
});

test('user cleans an already banned sender: Given an existing ban and rejected ban requests, When a delayed indexed message arrives, Then cleanup succeeds and the existing ban stays intact', async () => {
  await database.exec('INSERT INTO blacklisted_users VALUES (123, 22, 1)');
  telegram.members.set(22, { status: 'kicked' });
  telegram.faults.set('banChatMember', () => Response.json({ ok: false, error_code: 403 }, { status: 403 }));
  telegram.send(message());
  assert.equal((await dispatch({ update_id: 901, message: message() })).status, 200);
  assert.equal(telegram.canJoin(22), false);
  assert.equal(telegram.has(81), false);
});

test('user retains confirmed reports across evidence expiry: Given an anonymous administrator report, When evidence expires and the sender posts in another group, Then the retained account blacklist blocks the sender there', async () => {
  const update = report();
  update.message.sender_chat = { ...chat };
  telegram.send(update.message.reply_to_message);
  telegram.send(update.message);
  assert.equal((await dispatch(update)).status, 200);
  const saved = await database.prepare('SELECT expires_at FROM reports').first();
  await (await runtime.getWorker()).scheduled({ scheduledTime: new Date(saved.expires_at * 1000), cron: '* * * * *' });
  assert.equal((await database.prepare('SELECT count(*) AS count FROM reports').first()).count, 0);
  const destination = -10099;
  telegram.groups.set(destination, new Map([[22, { status: 'left' }]]));
  const target = { ...message(91), chat: { ...chat, id: destination } };
  telegram.send(target);
  assert.equal((await dispatch({ update_id: 902, message: target })).status, 200);
  assert.equal(telegram.canJoin(22, destination), false);
  assert.equal(telegram.has(91, destination), false);
});

test('user keeps unconfirmed reports local: Given a member report, protected sender, or rejected ban, When those reports finish, Then no account enters the shared blacklist', async () => {
  for (const [sender, reporterStatus, senderStatus, rejected] of [
    [31, 'member', 'member', false], [32, 'administrator', 'creator', false], [33, 'administrator', 'member', true],
  ]) {
    telegram.members.set(11, { status: reporterStatus });
    telegram.members.set(sender, { status: senderStatus });
    const update = report(message(sender, sender));
    update.update_id = sender;
    update.message.message_id = sender + 100;
    telegram.send(update.message.reply_to_message);
    telegram.send(update.message);
    if (rejected) telegram.faults.set(`banChatMember:${sender}`, () => Response.json({ ok: false, error_code: 400 }, { status: 400 }));
    assert.equal((await dispatch(update)).status, 200);
  }
  assert.deepEqual((await database.prepare('SELECT user_id FROM blacklisted_users').all()).results, []);
});

test('user retries blacklist persistence without re-banning: Given storage fails after a confirmed report ban, When storage recovers after a manual unban, Then the account is recorded without reversing the local unban', async () => {
  await database.exec("CREATE TRIGGER reject_blacklist BEFORE INSERT ON blacklisted_users BEGIN SELECT RAISE(ABORT, 'unavailable'); END");
  const update = report();
  telegram.send(update.message.reply_to_message);
  telegram.send(update.message);
  try {
    assert.equal((await dispatch(update)).status, 503);
    assert.equal(telegram.canJoin(22), false);
    assert.equal(telegram.has(82), true);
  } finally {
    await database.exec('DROP TRIGGER reject_blacklist');
  }
  telegram.members.set(22, { status: 'left' });
  assert.equal((await dispatch(update)).status, 200);
  assert.equal(telegram.canJoin(22), true);
  assert.deepEqual((await database.prepare('SELECT bot_id, user_id FROM blacklisted_users').all()).results,
    [{ bot_id: 123, user_id: 22 }]);
});

test('user excludes automatic mutes from shared bans: Given an inline source match, When automatic filtering mutes the sender, Then no account blacklist record is created', async () => {
  const target = { ...message(), via_bot: { id: 273234066, is_bot: true, first_name: 'Source' } };
  telegram.send(target);
  assert.equal((await dispatch({ update_id: 1, message: target })).status, 200);
  assert.equal(telegram.canSend(22), false);
  assert.equal(telegram.canJoin(22), true);
  assert.deepEqual((await database.prepare('SELECT user_id FROM blacklisted_users').all()).results, []);
});

test('user removes false positives: Given an account blacklist entry, When the operator deletes it before a new message, Then the sender and message remain unaffected', async () => {
  await database.exec('INSERT INTO blacklisted_users VALUES (123, 22, 1), (456, 22, 1)');
  await database.exec('DELETE FROM blacklisted_users WHERE bot_id = 123 AND user_id = 22');
  telegram.send(message());
  assert.equal((await dispatch({ update_id: 901, message: message() })).status, 200);
  assert.equal(telegram.canJoin(22), true);
  assert.equal(telegram.has(81), true);
});

test('user retries account moderation storage: Given a blacklisted sender and unavailable message indexing, When delivery fails then storage recovers, Then the response identifies a moderation storage failure and retry completes the ban and cleanup', async () => {
  await database.exec('INSERT INTO blacklisted_users VALUES (123, 22, 1)');
  await database.exec("CREATE TRIGGER reject_index BEFORE INSERT ON recent_messages BEGIN SELECT RAISE(ABORT, 'unavailable'); END");
  const update = { update_id: 901, message: message() };
  telegram.send(update.message);
  try {
    const response = await dispatch(update);
    assert.equal(response.status, 503);
    assert.equal(await response.text(), 'account moderation storage unavailable; retry pending');
    assert.equal(telegram.canJoin(22), true);
    assert.equal(telegram.has(81), true);
  } finally {
    await database.exec('DROP TRIGGER reject_index');
  }
  assert.equal((await dispatch(update)).status, 200);
  assert.equal(telegram.canJoin(22), false);
  assert.equal(telegram.has(81), false);
});

for (const mode of ['automatic source match', 'administrator report']) {
  test(`user keeps basic-group filtering: Given blacklisted accounts in a basic group, When an ${mode} arrives, Then the target disappears without changing membership or other history`, async () => {
    await database.exec('INSERT INTO blacklisted_users VALUES (123, 11, 1), (123, 22, 1)');
    const target = { ...message(), chat: { ...chat, type: 'group' } };
    const update = mode === 'administrator report' ? report(target) : {
      update_id: 901,
      message: { ...target, via_bot: { id: 273234066, is_bot: true, first_name: 'Source' } },
    };
    update.message.chat = { ...target.chat };
    telegram.send({ ...message(70), chat: { ...target.chat } });
    telegram.send(target);
    telegram.send(update.message);
    assert.equal((await dispatch(update)).status, 200);
    assert.equal(telegram.has(81), false);
    assert.equal(telegram.has(82), false);
    assert.equal(telegram.has(70), true);
    assert.equal(telegram.canJoin(22), true);
    assert.equal(telegram.canSend(22), true);
  });
}
