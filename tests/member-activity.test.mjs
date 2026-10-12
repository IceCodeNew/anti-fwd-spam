import assert from 'node:assert/strict';
import { test } from 'node:test';
import { readFile } from 'node:fs/promises';
import { database, dispatch, telegram, tick } from './worker-runtime.mjs';
import { message, report } from './telegram-fake.mjs';

const windowSeconds = 48 * 3600;

async function activityCount(bot = 123, chat = -10012, user = 22) {
  const row = await database.prepare('SELECT matured_count FROM member_activity WHERE bot_id=? AND chat_id=? AND user_id=?')
    .bind(bot, chat, user).first();
  return row?.matured_count ?? 0;
}

test('user: Given ten observed group messages, When they naturally reach 48 hours, Then credit matures once without deleting Telegram messages', async () => {
  const sentAt = Math.floor(Date.now() / 1000) - 60;
  for (let id = 100; id < 110; id++) {
    const current = { ...message(id), date: sentAt };
    telegram.send(current);
    assert.equal((await dispatch({ update_id: id, message: current })).status, 200);
  }
  assert.equal(await activityCount(), 0, 'observation does not write cumulative credit');
  await tick(sentAt + windowSeconds - 1);
  assert.equal(await activityCount(), 0, 'credit cannot mature early');
  await tick(sentAt + windowSeconds);
  assert.equal(await activityCount(), 10, 'ten naturally expired observations establish the member');
  assert.equal((await database.prepare('SELECT COUNT(*) AS n FROM recent_messages').first()).n, 0);
  await tick(sentAt + windowSeconds + 1);
  assert.equal(await activityCount(), 10, 'repeated cron cannot credit the same observations again');
  for (let id = 100; id < 110; id++) assert.equal(telegram.has(id), true);
});

test('user: Given cumulative activity in different bots, groups and accounts, When another batch expires, Then only eligible observations add credit and each counter caps at ten', async () => {
  const sentAt = Math.floor(Date.now() / 1000) - 60;
  await database.prepare('INSERT INTO member_activity VALUES (123,-10012,22,8)').run();
  const observations = [
    [123, -10012, 22, 1], [123, -10012, 22, 1], [123, -10012, 22, 1],
    [124, -10012, 22, 1], [123, -10013, 22, 1], [123, -10012, 33, 1],
    [123, -10012, 44, 0],
  ];
  for (const [index, [bot, chat, sender, eligible]] of observations.entries()) {
    await database.prepare('INSERT INTO recent_messages (bot_id,chat_id,message_id,sender_id,sent_at,activity_eligible) VALUES(?,?,?,?,?,?)')
      .bind(bot, chat, 200 + index, sender, sentAt, eligible).run();
  }
  assert.equal(await activityCount(), 8, 'pending rows cannot contribute early');
  await tick(sentAt + windowSeconds);
  assert.equal(await activityCount(), 10);
  assert.equal(await activityCount(124, -10012, 22), 1);
  assert.equal(await activityCount(123, -10013, 22), 1);
  assert.equal(await activityCount(123, -10012, 33), 1);
  assert.equal(await activityCount(123, -10012, 44), 0);
  await tick(sentAt + windowSeconds + 86400);
  assert.equal(await activityCount(), 10, 'matured credit persists after index retention');
  assert.equal(await activityCount(124, -10012, 22), 1);
});

test('user: Given eligible observations deleted by a trusted report, When their expiry passes, Then moderation cleanup cannot mature their activity', async () => {
  const sentAt = Math.floor(Date.now() / 1000) - 60;
  const current = { ...message(70), date: sentAt };
  telegram.send(current);
  assert.equal((await dispatch({ update_id: 70, message: current })).status, 200);
  const indexed = await database.prepare('SELECT activity_eligible FROM recent_messages WHERE message_id=70').first();
  assert.equal(indexed.activity_eligible, 1);
  assert.equal((await dispatch(report(current))).status, 200);
  assert.equal(telegram.has(70), false);
  assert.deepEqual(await database.prepare('SELECT activity_eligible,history_removed FROM recent_messages WHERE message_id=70').first(),
    { activity_eligible: 0, history_removed: 1 }, 'confirmed deletion retains an ineligible replay tombstone');
  await tick(sentAt + windowSeconds);
  assert.equal(await activityCount(), 0, 'forgotten observations never contribute');
});

test('user: Given a detected low-history advertisement whose deletion fails, When redelivered after allow and natural expiry, Then its observation never earns maturity', async () => {
  const sentAt = Math.floor(Date.now() / 1000) - 60;
  const current = { ...message(72), date: sentAt, text: '@synthetic_bot campaign_1 marker' };
  telegram.send(message(60));
  telegram.send(current);
  telegram.faults.set('deleteMessage:72', () => Response.json({ ok: false, error_code: 429 }, { status: 429 }));
  assert.equal((await dispatch({ update_id: 72, message: current })).status, 503);
  assert.equal((await database.prepare('SELECT activity_eligible FROM recent_messages WHERE message_id=72').first()).activity_eligible, 0);
  const recovery = { update_id: 73, message: { ...message(73, 11), text: '/allow 22', entities: [{ type: 'bot_command', offset: 0, length: 6 }] } };
  assert.equal((await dispatch(recovery)).status, 200);
  assert.equal((await dispatch({ update_id: 72, message: current })).status, 200);
  await tick(sentAt + windowSeconds);
  assert.equal(await activityCount(), 0);
  assert.equal(telegram.has(60), true, 'only the current message is a moderation target');
  assert.equal(telegram.has(72), true, 'a cancelled older retry cannot delete the restored member sample');
  assert.equal(telegram.canSend(22), true);
});

for (const failure of ['none', 'permanent_cleanup_rejection']) {
  test(`user: a blacklisted sender's observation earns no maturity after ${failure} and webhook redelivery`, async () => {
    const sentAt = Math.floor(Date.now() / 1000) - 60;
    await database.prepare('INSERT INTO blacklisted_users(bot_id,user_id,added_at) VALUES(123,22,0)').run();
    const update = { update_id: 76, message: { ...message(76), date: sentAt } };
    telegram.send(update.message);
    if (failure !== 'none') telegram.faults.set('deleteMessages', () => Response.json({ ok: false, error_code: 403 }, { status: 403 }));
    assert.equal((await dispatch(update)).status, 200);
    assert.equal((await dispatch(update)).status, 200);
    const observation = await database.prepare('SELECT activity_eligible FROM recent_messages WHERE message_id=76').first();
    assert.ok(observation === null || observation.activity_eligible === 0);
    await tick(sentAt + windowSeconds);
    assert.equal(await activityCount(), 0);
  });
}

test('user: history-cleaned observations remain ineligible when original webhooks replay after recovery', async () => {
  const sentAt = Math.floor(Date.now() / 1000) - 60;
  const original = { update_id: 77, message: { ...message(77), date: sentAt } };
  telegram.send(original.message);
  assert.equal((await dispatch(original)).status, 200);
  const reported = report(original.message);
  reported.update_id = 78;
  assert.equal((await dispatch(reported)).status, 200);
  assert.equal(telegram.has(77), false);
  const recovery = { update_id: 79, message: { ...message(79, 11), text: '/allow 22', entities: [{ type: 'bot_command', offset: 0, length: 6 }] } };
  assert.equal((await dispatch(recovery)).status, 200);
  assert.equal((await dispatch(original)).status, 200);
  await tick(sentAt + windowSeconds);
  assert.equal(await activityCount(), 0);
});

for (const status of ['administrator', 'member']) {
  test(`user: Given a report target whose ban is ${status === 'administrator' ? 'protected' : 'rejected'}, When only its current message is deleted, Then retained history cannot mature the target`, async () => {
    const sentAt = Math.floor(Date.now() / 1000) - 60;
    const current = { ...message(70), date: sentAt };
    telegram.members.set(22, { status });
    telegram.send(current);
    assert.equal((await dispatch({ update_id: 70, message: current })).status, 200);
    if (status === 'member') telegram.faults.set('banChatMember:22', () => Response.json({ ok: false, error_code: 403 }, { status: 403 }));
    assert.equal((await dispatch(report(current))).status, 200);
    assert.equal(telegram.has(70), false);
    assert.equal(telegram.members.get(22).status, status);
    await tick(sentAt + windowSeconds);
    assert.equal(await activityCount(), 0);
  });
}

test('user: Given a command in a media caption, When the observation naturally expires, Then it never earns member maturity', async () => {
  const sentAt = Math.floor(Date.now() / 1000) - 60;
  const current = { ...message(75), date: sentAt, caption: '/help', caption_entities: [{ type: 'bot_command', offset: 0, length: 5 }],
    photo: [{ file_id: 'synthetic_photo', file_unique_id: 'synthetic_unique', width: 1, height: 1 }] };
  delete current.text;
  telegram.send(current);
  assert.equal((await dispatch({ update_id: 75, message: current })).status, 200);
  await tick(sentAt + windowSeconds);
  assert.equal(await activityCount(), 0);
  assert.equal(telegram.has(75), true);
});

for (const stage of ['credit', 'deletion']) {
  test(`user: Given a failed ${stage} in scheduled expiry, When storage recovers, Then credit and identifier removal roll back together and retry exactly once`, async () => {
    const sentAt = Math.floor(Date.now() / 1000) - 60;
    await database.prepare('INSERT INTO recent_messages (bot_id,chat_id,message_id,sender_id,sent_at,activity_eligible) VALUES(123,-10012,300,22,?,1)')
      .bind(sentAt).run();
    const table = stage === 'credit' ? 'member_activity' : 'recent_messages';
    const operation = stage === 'credit' ? 'INSERT' : 'DELETE';
    await database.prepare(`CREATE TRIGGER reject_expiry BEFORE ${operation} ON ${table} BEGIN SELECT RAISE(ABORT, 'unavailable'); END`).run();
    try {
      await tick(sentAt + windowSeconds);
      assert.equal(await activityCount(), 0, 'failed expiry cannot partially credit activity');
      assert.ok(await database.prepare('SELECT 1 FROM recent_messages WHERE message_id=300').first(), 'failed expiry retains its observation');
    } finally {
      await database.prepare('DROP TRIGGER reject_expiry').run();
    }
    await tick(sentAt + windowSeconds);
    assert.equal(await activityCount(), 1);
    assert.equal(await database.prepare('SELECT 1 FROM recent_messages WHERE message_id=300').first(), null);
    await tick(sentAt + windowSeconds);
    assert.equal(await activityCount(), 1);
  });
}

test('user: Given 1001 equally old mixed-eligibility observations, When bounded cron batches run, Then credit follows exactly the deleted rows without skipping ineligible identifiers', async () => {
  const sentAt = Math.floor(Date.now() / 1000) - 60;
  await database.prepare('WITH RECURSIVE ids(i) AS (SELECT 1 UNION ALL SELECT i+1 FROM ids WHERE i<1001) '
    + 'INSERT INTO recent_messages(bot_id,chat_id,message_id,sender_id,sent_at,activity_eligible) '
    + 'SELECT 123,-10012,i,CASE WHEN i=1 THEN 33 WHEN i=1001 THEN 44 ELSE 22 END,?,CASE WHEN i IN (1,1001) THEN 1 ELSE 0 END FROM ids')
    .bind(sentAt).run();
  await database.prepare('INSERT INTO recent_messages(bot_id,chat_id,message_id,sender_id,sent_at,activity_eligible) VALUES(123,-10012,1002,55,?,1)')
    .bind(sentAt + 1).run();
  await tick(sentAt + windowSeconds);
  assert.equal(await activityCount(123, -10012, 33), 1);
  assert.equal(await activityCount(123, -10012, 44), 0, 'last expired observation waits for the next bounded batch');
  assert.equal(await activityCount(), 0, 'ineligible rows expire without credit');
  assert.equal(await activityCount(123, -10012, 55), 0, 'unexpired observation cannot mature');
  assert.deepEqual((await database.prepare('SELECT message_id FROM recent_messages ORDER BY message_id').all()).results,
    [{ message_id: 1001 }, { message_id: 1002 }]);
  await tick(sentAt + windowSeconds);
  assert.equal(await activityCount(123, -10012, 44), 1);
  assert.equal(await activityCount(123, -10012, 33), 1, 'first batch cannot contribute twice');
  assert.deepEqual((await database.prepare('SELECT message_id FROM recent_messages').all()).results, [{ message_id: 1002 }]);
  await tick(sentAt + windowSeconds + 1);
  assert.equal(await activityCount(123, -10012, 55), 1);
});

test('user: Given a pre-migration message index, When activity support is installed and legacy rows expire, Then old observations remain uncredited and stored counters reject invalid values', async () => {
  const sentAt = Math.floor(Date.now() / 1000) - 60;
  await database.prepare('DROP TABLE recent_messages').run();
  await database.prepare('DROP TABLE member_activity').run();
  await database.prepare('DROP TABLE automatic_mutes').run();
  for (const name of ['0003_recent_messages.sql', '0004_automatic_mutes.sql']) {
    for (const sql of (await readFile(`migrations/${name}`, 'utf8')).split(';').filter(sql => sql.trim())) {
      await database.prepare(sql).run();
    }
  }
  await database.prepare('INSERT INTO recent_messages VALUES(123,-10012,400,22,?)').bind(sentAt).run();
  for (const sql of (await readFile('migrations/0017_member_activity.sql', 'utf8')).split(';').filter(sql => sql.trim())) {
    await database.prepare(sql).run();
  }
  const legacyReplay = { ...message(400), date: sentAt + 1 };
  assert.equal((await dispatch({ update_id: 400, message: legacyReplay })).status, 200);
  assert.equal((await dispatch({ update_id: 402, edited_message: { ...legacyReplay, date: sentAt + 2, edit_date: sentAt + 2 } })).status, 200);
  assert.deepEqual(await database.prepare('SELECT sent_at,activity_eligible FROM recent_messages WHERE message_id=400').first(),
    { sent_at: sentAt, activity_eligible: 0 }, 'redelivery and edits cannot upgrade legacy eligibility or refresh its age');
  await database.prepare('INSERT INTO recent_messages(bot_id,chat_id,message_id,sender_id,sent_at) VALUES(123,-10012,401,22,?)')
    .bind(sentAt).run();
  await assert.rejects(database.prepare('UPDATE recent_messages SET activity_eligible=2').run());
  for (const count of [-1, 11]) {
    await assert.rejects(database.prepare('INSERT INTO member_activity VALUES(123,-10012,22,?)').bind(count).run());
  }
  await tick(sentAt + windowSeconds);
  assert.equal(await activityCount(), 0);
  assert.equal((await database.prepare('SELECT COUNT(*) AS n FROM recent_messages').first()).n, 0);
});
