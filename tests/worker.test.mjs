import assert from 'node:assert/strict';
import { test } from 'node:test';
import { chat, message, report, token, username } from './telegram-fake.mjs';
import { database, dispatch, headers, model, runtime, telegram } from './worker-runtime.mjs';

async function evidence() {
  return (await database.prepare('SELECT * FROM reports ORDER BY update_id').all()).results;
}

test('user: Given no model secret, When an ordinary message arrives, Then it remains and its content is not shared with the model', async () => {
  model.probability = 1;
  telegram.send(message());
  assert.equal((await dispatch({ update_id: 1, message: message() })).status, 200);
  assert.equal(telegram.has(81), true);
  assert.equal(model.state, null);
});

test('user: Given paused model tasks without a secret, When a message is edited, Then its old content is discarded without inference', async () => {
  const now = Math.floor(Date.now() / 1000);
  await database.prepare(`INSERT INTO model_tasks
    (bot_id, chat_id, message_id, phase, input_json, due_at, stop_at, created_at, expires_at)
    VALUES (123, -10012, 81, 'classify', '{"message":{"text":"old"}}', ?, ?, ?, ?)`)
    .bind(now, now + 3600, now, now + 3 * 86400).run();
  telegram.send(message());
  assert.equal((await dispatch({ update_id: 2, edited_message: message() })).status, 200);
  const saved = await database.prepare('SELECT phase, input_json, expires_at FROM model_tasks').first();
  assert.equal(saved.phase, 'done');
  assert.equal(saved.input_json, null);
  assert.equal(saved.expires_at, now + 3 * 86400);
  assert.equal(model.state, null);
  assert.equal(telegram.has(81), true);
});

for (const outcome of ['confirmed', 'response lost']) {
  test(`user keeps an administrative unmute after ${outcome}: Given an automatic mute took effect, When an administrator unmutes before redelivery, Then the user remains able to send and new spam is still moderated`, async () => {
    const update = { update_id: 900, message: { ...message(), via_bot: { id: 273234066, is_bot: true } } };
    telegram.send(update.message);
    if (outcome === 'response lost') {
      telegram.faults.set('restrictChatMember', async () => {
        telegram.faults.clear();
        await telegram.fetch(new Request(`https://api.telegram.org/bot${token}/restrictChatMember`, {
          method: 'POST', headers: { 'content-type': 'application/json' },
          body: JSON.stringify({ chat_id: chat.id, user_id: 22, until_date: 0,
            use_independent_chat_permissions: true, permissions: { can_send_messages: false } }),
        }));
        return new Response('response lost', { status: 502 });
      });
    }
    assert.equal((await dispatch(update)).status, outcome === 'confirmed' ? 200 : 503);
    assert.equal(telegram.canSend(22), false);
    telegram.members.set(22, { status: 'member' });
    assert.equal((await dispatch(update)).status, 200);
    assert.equal(telegram.canSend(22), true);
    assert.equal(telegram.has(81), false);
    assert.equal((await dispatch({ update_id: 901, edited_message: update.message })).status, 200);
    assert.equal(telegram.canSend(22), true);
    const fresh = { ...update.message, message_id: 90 };
    telegram.send(fresh);
    assert.equal((await dispatch({ update_id: 902, message: fresh })).status, 200);
    assert.equal(telegram.has(90), false);
    assert.equal(telegram.canSend(22), false);
    assert.equal(await database.prepare('SELECT user_id FROM blacklisted_users WHERE user_id=22').first(), null);
  });
}

test('user recovers automatic moderation: Given unavailable storage followed by a temporary Telegram rejection, When delivery resumes after recovery, Then the message stays deleted and muting eventually succeeds', async () => {
  const update = { update_id: 1, message: { ...message(), via_bot: { id: 273234066, is_bot: true } } };
  telegram.send(update.message);
  await database.prepare('ALTER TABLE automatic_mutes RENAME TO unavailable_mutes').run();
  try {
    assert.equal((await dispatch(update)).status, 503);
    assert.equal(telegram.has(81), false);
    assert.equal(telegram.canSend(22), true);
  } finally {
    await database.prepare('ALTER TABLE unavailable_mutes RENAME TO automatic_mutes').run();
  }
  telegram.faults.set('restrictChatMember', () => Response.json({ ok: false, error_code: 429 }, { status: 429 }));
  assert.equal((await dispatch(update)).status, 503);
  assert.equal(telegram.canSend(22), true);
  telegram.faults.clear();
  assert.equal((await dispatch(update)).status, 200);
  assert.equal(telegram.canSend(22), false);
});

test('user keeps an unmute across overlapping automatic deliveries: Given a delayed membership lookup, When another delivery finishes and an administrator unmutes, Then the late delivery preserves that permission', async () => {
  const entered = Promise.withResolvers(), release = Promise.withResolvers();
  const update = { update_id: 1, message: { ...message(), via_bot: { id: 273234066, is_bot: true } } };
  telegram.send(update.message);
  telegram.faults.set('getChatMember:22', () => { entered.resolve(); return release.promise; });
  const pending = dispatch(update);
  await entered.promise;
  telegram.faults.clear();
  try {
    assert.equal((await dispatch(update)).status, 200);
    assert.equal(telegram.canSend(22), false);
    telegram.members.set(22, { status: 'member' });
  } finally {
    release.resolve(Response.json({ ok: true, result: { status: 'member', user: message().from } }));
    assert.equal((await pending).status, 200);
  }
  assert.equal(telegram.canSend(22), true);
  assert.equal(telegram.has(81), false);
});

test('user expires mute identifiers: Given an automatic restriction, When redelivery and scheduled cleanup run, Then identifiers retain their original three-day expiry without storing content', async () => {
  const update = { update_id: 1, message: { ...message(), via_bot: { id: 273234066, is_bot: true } } };
  telegram.send(update.message);
  const before = Math.floor(Date.now() / 1000);
  assert.equal((await dispatch(update)).status, 200);
  const rows = async () => (await database.prepare('SELECT * FROM automatic_mutes').all()).results;
  const [saved] = await rows();
  assert.deepEqual(Object.keys(saved).sort(), ['bot_id', 'chat_id', 'expires_at', 'message_id']);
  assert.ok(saved.expires_at >= before + 259200 && saved.expires_at <= Math.floor(Date.now() / 1000) + 259200);
  // Represent an earlier attempt so an accidental TTL refresh cannot hide within the same second.
  saved.expires_at -= 60;
  await database.prepare('UPDATE automatic_mutes SET expires_at = ?').bind(saved.expires_at).run();
  assert.equal((await dispatch(update)).status, 200);
  assert.deepEqual(await rows(), [saved]);
  const worker = await runtime.getWorker();
  await worker.scheduled({ scheduledTime: new Date((saved.expires_at - 1) * 1000), cron: '* * * * *' });
  assert.deepEqual(await rows(), [saved]);
  await worker.scheduled({ scheduledTime: new Date(saved.expires_at * 1000), cron: '* * * * *' });
  assert.deepEqual(await rows(), []);
});

test('user clears recent history: Given observed messages from different senders and groups, When an administrator reports spam, Then only the reported sender\'s recent messages in that group disappear', async () => {
  const now = Math.floor(Date.now() / 1000);
  const recent = { ...message(70), date: now - 3600 };
  const old = { ...message(71), date: now - 49 * 3600 };
  const other = { ...message(72, 11), date: now - 3600 };
  const elsewhere = { ...message(73), date: now - 3600, chat: { ...chat, id: -10099 } };
  for (const msg of [recent, old, other, elsewhere]) {
    telegram.send(msg);
    assert.equal((await dispatch({ message: msg })).status, 200);
  }
  // This message was indexed earlier and has since aged beyond the API window.
  await database.prepare('INSERT INTO recent_messages (bot_id, chat_id, message_id, sender_id, sent_at) VALUES (?, ?, ?, ?, ?)')
    .bind(123, chat.id, old.message_id, 22, old.date).run();
  const update = report({ ...message(), date: now });
  telegram.send(update.message.reply_to_message);
  telegram.send(update.message);

  assert.equal((await dispatch(update)).status, 200);

  assert.equal(telegram.has(70), false);
  assert.equal(telegram.has(71), true);
  assert.equal(telegram.has(72), true);
  assert.equal(telegram.messages.has('-10099:73'), true);
  assert.equal(telegram.has(81), false);
  assert.equal(telegram.has(82), false);
  assert.equal(telegram.canJoin(22), false);
});

test('user keeps history: Given an old message, When a blacklisted inline message arrives, Then only the new message disappears and the sender is permanently muted', async () => {
  const spam = { ...message(), via_bot: { id: 273234066, is_bot: true, first_name: 'Source' } };
  const history = { ...message(80), date: Math.floor(Date.now() / 1000) - 3600 };
  telegram.send(history);
  assert.equal((await dispatch({ message: history })).status, 200);
  telegram.send(spam);

  const response = await dispatch({ update_id: 70, message: spam });

  assert.equal(response.status, 200);
  assert.equal(telegram.has(81), false);
  assert.equal(telegram.has(80), true);
  for (const permission of ['can_send_messages', 'can_send_photos', 'can_send_polls', 'can_send_other_messages']) {
    assert.equal(telegram.canSend(22, permission), false);
  }
  assert.equal(telegram.members.get(22).until_date, 0);
  assert.equal(telegram.canJoin(22), true);
  assert.equal(await database.prepare('SELECT user_id FROM blacklisted_users WHERE user_id=22').first(), null);
});

test('user bans reported spam: Given a non-member commenter and a sticker report, When an administrator mentions the bot, Then the sticker and report disappear and the sender cannot comment or rejoin', async () => {
  telegram.members.set(22, { status: 'left' });
  const target = { ...message(), sticker: { file_id: 'sticker', file_unique_id: 'unique-sticker' } };
  delete target.text;
  const update = report(target);
  telegram.send(message(80));
  telegram.send(update.message.reply_to_message);
  telegram.send(update.message);
  telegram.send(message(79, 11));

  const response = await dispatch(update);

  assert.equal(response.status, 200);
  assert.equal(telegram.canJoin(22), false);
  assert.equal(telegram.canSend(22), false);
  assert.equal(telegram.has(80), true);
  assert.equal(telegram.has(81), false);
  assert.equal(telegram.has(82), false);
  assert.equal(telegram.has(79), true);
  const [row] = await evidence();
  assert.deepEqual(JSON.parse(row.raw_update), update);
  assert.equal(row.expires_at - row.received_at, 259200);
});

test('user: Given the bot cannot ban the sender and deletion waits for a retry, When the report is redelivered after the reporter loses admin rights, Then the target is deleted without another ban attempt', async () => {
  const update = report();
  telegram.send(update.message.reply_to_message);
  telegram.send(update.message);
  let banAttempts = 0;
  telegram.faults.set('banChatMember', () => {
    banAttempts += 1;
    return Response.json({
      ok: false, error_code: 400, description: 'Bad Request: not enough rights to restrict/unrestrict chat member',
    }, { status: 400 });
  });
  telegram.faults.set('deleteMessage:81', () => Response.json({ ok: false, error_code: 429 }, { status: 429 }));
  assert.equal((await dispatch(update)).status, 503);
  assert.equal(telegram.has(81), true);
  telegram.faults.delete('deleteMessage:81');
  telegram.members.set(11, { status: 'member' });
  assert.equal((await dispatch(update)).status, 200);
  assert.equal(telegram.has(81), false);
  assert.equal(telegram.has(82), false);
  assert.equal(banAttempts, 1);
  assert.equal(telegram.canJoin(22), true);
  assert.equal((await database.prepare('SELECT COUNT(*) AS n FROM blacklisted_users').first()).n, 0);
});

test('user: Given a forum topic, When an administrator mentions the bot without replying to a message, Then the topic creator is not reported', async () => {
  const topic = { ...message(60), forum_topic_created: { name: 'topic', icon_color: 1 } };
  telegram.send(topic);
  const update = report(topic);
  update.message.message_thread_id = 60;
  update.message.is_topic_message = true;
  telegram.send(update.message);
  telegram.send(message(80));
  await dispatch({ update_id: 70, message: message(80) });
  assert.equal((await dispatch(update)).status, 200);
  assert.equal(telegram.canJoin(22), true);
  for (const id of [60, 80, 82]) assert.equal(telegram.has(id), true);
  assert.equal((await evidence()).length, 0);
});

test('user reports old spam: Given a target Telegram refuses to delete, When an administrator reports it, Then the sender is banned but the report remains and the outcome does not claim deletion', async () => {
  const target = { ...message(), date: 1 };
  const update = report(target);
  const recent = { ...message(70), date: Math.floor(Date.now() / 1000) - 60 };
  telegram.send(recent);
  await dispatch({ message: recent });
  telegram.send(target);
  telegram.send(update.message);
  telegram.send(message(80));

  const response = await dispatch(update);

  assert.equal(response.status, 200);
  assert.match(await response.text(), /^deletion rejected; banned/);
  assert.equal(telegram.has(70), false);
  assert.equal(telegram.has(81), true);
  assert.equal(telegram.has(82), true);
  assert.equal(telegram.has(80), true);
  assert.equal(telegram.canJoin(22), false);
});

test('user resumes bulk cleanup: Given more than one batch and a temporary failure, When an administrator unbans the sender and a later message edit arrives before redelivery, Then older observed messages disappear but later messages and membership survive', async () => {
  const now = Math.floor(Date.now() / 1000);
  for (let id = 100; id < 202; id++) {
    const msg = { ...message(id), date: now - 60 };
    telegram.send(msg);
    assert.equal((await dispatch({ message: msg })).status, 200);
  }
  const update = report({ ...message(300), date: now });
  update.message.message_id = 400;
  telegram.send(update.message.reply_to_message);
  telegram.send(update.message);
  assert.equal((await dispatch(update)).status, 503);
  for (let id = 100; id < 200; id++) assert.equal(telegram.has(id), false);
  assert.equal(telegram.has(200), true);
  assert.equal(telegram.has(201), true);
  assert.equal(telegram.has(400), true);
  telegram.members.set(22, { status: 'left' });
  const fresh = { ...message(401), date: now };
  telegram.send(fresh);
  assert.equal((await dispatch({ update_id: 401, edited_message: fresh })).status, 200);
  telegram.faults.set('deleteMessages', () => Response.json({ ok: false, error_code: 429 }, { status: 429 }));
  assert.equal((await dispatch(update)).status, 503);
  assert.equal(telegram.has(200), true);
  assert.equal(telegram.has(400), true);
  telegram.faults.clear();

  assert.equal((await dispatch(update)).status, 200);

  assert.equal(telegram.has(200), false);
  assert.equal(telegram.has(201), false);
  assert.equal(telegram.has(400), false);
  assert.equal(telegram.has(401), true);
  assert.equal(telegram.canJoin(22), true);
  assert.equal(telegram.canSend(22), true);
  assert.equal((await dispatch(update)).status, 200);
  assert.equal(telegram.has(401), true);
});

test('user completes a full batch: Given exactly 100 indexed messages, When an administrator reports, Then cleanup finishes without another delivery', async () => {
  for (let id = 100; id < 200; id++) {
    const msg = { ...message(id), date: Math.floor(Date.now() / 1000) - 60 };
    telegram.send(msg);
    await dispatch({ message: msg });
  }
  const update = report(message(300));
  update.message.message_id = 400;
  telegram.send(update.message);

  assert.equal((await dispatch(update)).status, 200);

  for (let id = 100; id < 200; id++) assert.equal(telegram.has(id), false);
  assert.equal(telegram.has(400), false);
});

test('user retains failed cleanup evidence: Given a permanent bulk deletion rejection, When an administrator reports, Then the target disappears but the report and remaining history stay visible', async () => {
  const history = { ...message(70), date: Math.floor(Date.now() / 1000) - 60 };
  telegram.send(history);
  await dispatch({ message: history });
  const update = report();
  telegram.send(update.message.reply_to_message);
  telegram.send(update.message);
  telegram.faults.set('deleteMessages', () => Response.json({ ok: false, error_code: 403 }, { status: 403 }));

  const response = await dispatch(update);

  assert.equal(response.status, 200);
  assert.match(await response.text(), /history cleanup rejected/);
  assert.equal(telegram.has(70), true);
  assert.equal(telegram.has(81), false);
  assert.equal(telegram.has(82), true);
  assert.equal(telegram.canJoin(22), false);
});

test('user preserves message age: Given duplicate updates, edits and messages outside the deletion window, When an administrator reports, Then only eligible user messages disappear without storing their text', async () => {
  const now = Math.floor(Date.now() / 1000);
  const recent = { ...message(70), date: now - 47 * 3600, text: 'private text' };
  const old = { ...message(71), date: now - 48 * 3600, edit_date: now };
  const sentAsChat = { ...message(72), date: now, sender_chat: chat };
  const topic = { ...message(73), date: now, forum_topic_created: { name: 'topic', icon_color: 1 } };
  for (const msg of [recent, old, sentAsChat, topic]) {
    telegram.send(msg);
    await dispatch({ message: msg });
    await dispatch({ edited_message: { ...msg, edit_date: now } });
  }
  const rows = (await database.prepare('SELECT * FROM recent_messages').all()).results;
  assert.deepEqual(rows, [{ bot_id: 123, chat_id: chat.id, message_id: 70, sender_id: 22, sent_at: recent.date }]);

  assert.equal((await dispatch(report())).status, 200);

  assert.equal(telegram.has(70), false);
  for (const id of [71, 72, 73]) assert.equal(telegram.has(id), true);
});

test('user expires message identifiers: Given an indexed message and a controlled scheduled clock, When its original age reaches 48 hours, Then its identifier expires without deleting report evidence', async () => {
  const recent = { ...message(70), date: Math.floor(Date.now() / 1000) - 60 };
  await dispatch({ message: recent });
  telegram.members.set(11, { status: 'member' });
  await dispatch(report());
  const worker = await runtime.getWorker();
  const count = async () => (await database.prepare('SELECT count(*) AS count FROM recent_messages WHERE message_id = 70').first()).count;
  await worker.scheduled({ scheduledTime: new Date((recent.date + 48 * 3600 - 1) * 1000), cron: '* * * * *' });
  assert.equal(await count(), 1);
  await worker.scheduled({ scheduledTime: new Date((recent.date + 48 * 3600) * 1000), cron: '* * * * *' });
  assert.equal(await count(), 0);
  assert.equal((await evidence()).length, 1);
});

test('user recovers indexing: Given unavailable index storage, When delivery resumes after storage recovers, Then sender filtering succeeds while source history cleanup waits for storage', async () => {
  const recent = { ...message(70), date: Math.floor(Date.now() / 1000) - 60 };
  telegram.send(recent);
  await database.prepare('ALTER TABLE recent_messages RENAME TO unavailable_messages').run();
  try {
    assert.equal((await dispatch({ message: recent })).status, 503);
    assert.equal(telegram.has(70), true);
    const spam = { ...message(71), via_bot: { id: 273234066, is_bot: true } };
    telegram.send(spam);
    assert.equal((await dispatch({ update_id: 71, message: spam })).status, 503);
    assert.equal(telegram.has(71), false);
  } finally {
    await database.prepare('ALTER TABLE unavailable_messages RENAME TO recent_messages').run();
  }
  assert.equal((await dispatch({ message: recent })).status, 200);
  assert.equal((await dispatch(report())).status, 200);
  assert.equal(telegram.has(70), false);
});

test('user protects promoted senders: Given paused history cleanup, When the sender becomes an administrator before redelivery, Then their remaining messages and privileges survive', async () => {
  const history = { ...message(70), date: Math.floor(Date.now() / 1000) - 60 };
  telegram.send(history);
  await dispatch({ message: history });
  telegram.faults.set('deleteMessages', () => Response.json({ ok: false, error_code: 429 }, { status: 429 }));
  assert.equal((await dispatch(report())).status, 503);
  telegram.faults.clear();
  telegram.members.set(22, { status: 'administrator' });

  assert.equal((await dispatch(report())).status, 200);

  assert.equal(telegram.has(70), true);
  assert.deepEqual(telegram.members.get(22), { status: 'administrator' });
});

test('user records a report: Given a regular member and rich media, When they mention the bot in a reply, Then all evidence survives without deleting or restricting anyone', async () => {
  telegram.members.set(11, { status: 'member' });
  const target = {
    ...message(), animation: { file_id: 'animation', file_unique_id: 'unique-a' }, document: { file_id: 'animation' },
    photo: [{ file_id: 'photo', file_unique_id: 'unique-p', width: 320, height: 200 }],
    caption: '垃圾链接', caption_entities: [{ type: 'text_link', offset: 0, length: 4, url: 'https://example.com' }],
    effect_id: 'effect-1', rich_message: { blocks: [{ type: 'photo', photo: [{ file_id: 'nested' }] }] },
    future_field: { nested: [false, null, { 未知: 42 }] }, via_bot: { id: 777, is_bot: true, first_name: 'Source' },
  };
  const update = report(target);
  const history = { ...message(70), date: Math.floor(Date.now() / 1000) - 60 };
  telegram.send(history);
  await dispatch({ message: history });
  telegram.send(target);
  telegram.send(update.message);

  const response = await dispatch(update);

  assert.equal(await response.text(), 'report recorded');
  assert.equal(telegram.has(70), true);
  assert.equal(telegram.has(81), true);
  assert.equal(telegram.has(82), true);
  assert.equal(telegram.canSend(22), true);
  const [row] = await evidence();
  assert.deepEqual(JSON.parse(row.raw_update), update);
});

for (const status of ['creator', 'administrator']) {
  for (const automatic of [false, true]) {
    test(`user protects ${status}: Given their old and targeted messages, When ${automatic ? 'an automatic match' : 'an owner report'} arrives, Then ${automatic ? 'both messages survive' : 'only the target disappears'} and privileges remain`, async () => {
      telegram.members.set(11, { status: 'creator' });
      telegram.members.set(22, { status });
      const target = message();
      const update = automatic
        ? { update_id: 71, message: { ...target, via_bot: { id: 273234066, is_bot: true } } }
        : report(target);
      telegram.send(target);
      const history = { ...message(80), date: Math.floor(Date.now() / 1000) - 60 };
      telegram.send(history);
      await dispatch({ message: history });
      if (!automatic) telegram.send(update.message);

      const response = await dispatch(update);

      assert.equal(response.status, 200);
      assert.match(await response.text(), /skipped/);
      assert.equal(telegram.has(81), automatic);
      assert.equal(telegram.has(80), true);
      assert.deepEqual(telegram.members.get(22), { status });
    });
  }
}

test('user protects anonymous senders: Given a target sent as a chat, When an administrator reports it, Then the target is deleted without punishing its compatibility user', async () => {
  const target = { ...message(), sender_chat: chat };
  telegram.send(target);
  telegram.send(message(80));

  assert.equal((await dispatch(report(target))).status, 200);

  assert.equal(telegram.has(81), false);
  assert.equal(telegram.has(80), true);
  assert.equal(telegram.canSend(22), true);
});

test('user mutes a non-member commenter: Given a commenter with left status, When a rule matches their message, Then they are muted without losing history', async () => {
  telegram.members.set(22, { status: 'left' });
  telegram.send(message(80));
  const spam = { ...message(), via_bot: { id: 273234066, is_bot: true } };
  telegram.send(spam);

  assert.equal((await dispatch({ update_id: 1, message: spam })).status, 200);

  assert.equal(telegram.canSend(22), false);
  assert.equal(telegram.has(80), true);
});

test('user retries safely: Given a completed report, When Telegram redelivers it after an administrator lifts the ban, Then the user remains unbanned and evidence is unchanged', async () => {
  const update = report();
  telegram.send(update.message.reply_to_message);
  telegram.send(update.message);
  assert.equal((await dispatch(update)).status, 200);
  const saved = await evidence();
  telegram.members.set(22, { status: 'member' });
  telegram.send(message(90));

  assert.equal((await dispatch(update)).status, 200);

  assert.equal(telegram.canSend(22), true);
  assert.equal(telegram.canJoin(22), true);
  assert.equal(telegram.has(90), true);
  assert.deepEqual(await evidence(), saved);
});

test('user keeps an unban during cleanup: Given a successful ban and temporary report-deletion failure, When delivery resumes after an administrator unbans the sender, Then only the report is removed and new messages survive', async () => {
  const update = report();
  telegram.send(update.message.reply_to_message);
  telegram.send(update.message);
  telegram.faults.set('deleteMessage:82', () => Response.json({ ok: false, error_code: 429 }, { status: 429 }));
  assert.equal((await dispatch(update)).status, 503);
  assert.equal(telegram.canJoin(22), false);
  telegram.members.set(22, { status: 'member' });
  telegram.send(message(90));
  telegram.faults.clear();

  assert.equal((await dispatch(update)).status, 200);

  assert.equal(telegram.canJoin(22), true);
  assert.equal(telegram.has(90), true);
  assert.equal(telegram.has(82), false);
});

test('user resumes target deletion: Given a confirmed ban and temporary target-deletion failure, When redelivery follows an administrator unban, Then only the target and report disappear without banning again', async () => {
  const update = report();
  telegram.send(update.message.reply_to_message);
  telegram.send(update.message);
  telegram.faults.set('deleteMessage:81', () => Response.json({ ok: false, error_code: 429 }, { status: 429 }));

  const pending = await dispatch(update);
  assert.equal(pending.status, 503);
  assert.equal(await pending.text(), 'deletion pending retry; banned');
  assert.equal(telegram.canJoin(22), false);
  assert.equal(telegram.has(81), true);
  assert.equal(telegram.has(82), true);
  telegram.members.set(22, { status: 'member' });
  telegram.send(message(90));
  telegram.faults.clear();

  assert.equal((await dispatch(update)).status, 200);

  assert.equal(telegram.canJoin(22), true);
  assert.equal(telegram.canSend(22), true);
  assert.equal(telegram.has(90), true);
  assert.equal(telegram.has(81), false);
  assert.equal(telegram.has(82), false);
});

test('user retains evidence during failure: Given unavailable report storage, When a report or source match arrives, Then reports wait and automatic sender filtering proceeds while source bans wait', async () => {
  await database.prepare('ALTER TABLE reports RENAME TO unavailable_reports').run();
  try {
    const update = report();
    telegram.send(update.message.reply_to_message);
    assert.equal((await dispatch(update)).status, 503);
    assert.equal(telegram.has(81), true);
    assert.equal(telegram.canSend(22), true);
    update.message = { ...message(), via_bot: { id: 273234066, is_bot: true } };
    assert.equal((await dispatch(update)).status, 503);
    assert.equal(telegram.has(81), false);
    assert.equal(telegram.canSend(22), false);
    assert.equal(telegram.canJoin(273234066), true);
  } finally {
    await database.prepare('ALTER TABLE unavailable_reports RENAME TO reports').run();
  }
});

for (const stage of ['getChatMember:11', 'getChatMember:22', 'banChatMember']) {
  test(`user recovers from ${stage}: Given a temporary Telegram failure, When delivery is retried, Then evidence is retained and moderation finishes`, async () => {
    const update = report();
    telegram.send(message(80));
    telegram.send(update.message.reply_to_message);
    telegram.send(update.message);
    telegram.faults.set(stage, () => Response.json({ ok: false, error_code: 429, parameters: { retry_after: 1 } }, { status: 429 }));

    assert.equal((await dispatch(update)).status, 503);
    const [pending] = await evidence();
    assert.equal(pending.response_status, null);
    assert.equal(telegram.has(82), true);
    assert.equal(telegram.canJoin(22), true);
    assert.equal(telegram.has(80), true);
    telegram.faults.clear();
    assert.equal((await dispatch(update)).status, 200);

    assert.equal(telegram.canJoin(22), false);
    assert.equal(telegram.has(80), true);
    assert.equal(telegram.has(81), false);
    assert.equal(telegram.has(82), false);
    const [finished] = await evidence();
    assert.equal(finished.response_status, 200);
    assert.equal(finished.expires_at, pending.expires_at);
  });
}

test('user receives administrator protection on retry: Given a failed ban, When the target becomes an administrator before redelivery, Then history and privileges survive', async () => {
  const update = report();
  telegram.send(message(80));
  telegram.send(update.message.reply_to_message);
  telegram.faults.set('banChatMember', () => Response.json({ ok: false, error_code: 429 }, { status: 429 }));
  assert.equal((await dispatch(update)).status, 503);
  telegram.faults.clear();
  telegram.members.set(22, { status: 'administrator' });

  assert.equal((await dispatch(update)).status, 200);

  assert.equal(telegram.has(81), false);
  assert.equal(telegram.has(80), true);
  assert.deepEqual(telegram.members.get(22), { status: 'administrator' });
});

test('user chooses the reported bot: Given username entities and UTF-16 offsets, When a reply or edited caption mentions this bot, Then only an exact username records a report', async () => {
  telegram.members.set(11, { status: 'member' });
  const candidates = [
    [{ text: '😀 @TEST_GATE_BOT', entities: [{ type: 'mention', offset: 3, length: 14 }] }, true],
    [{ caption: '@test_gate_bot', caption_entities: [{ type: 'mention', offset: 0, length: 14 }] }, true],
    [{ entities: [{ type: 'text_mention', user: { id: 999, username: 'TEST_GATE_BOT' } }] }, true],
    [{ entities: [{ type: 'text_mention', user: { id: 123, username: 'other_bot' } }] }, false],
    [{ entities: [{ type: 'text_mention', user: { id: 123, first_name: username } }] }, false],
    [{ text: '@test_gate_bot_extra', entities: [{ type: 'mention', offset: 0, length: 20 }] }, false],
    [{ text: '@test_gate_bot' }, false],
    [{ text: '😀 @test_gate_bot', entities: [{ type: 'mention', offset: 2, length: 14 }] }, false],
    [{ text: '@test_gate_bot', entities: [{ type: 'mention', offset: 0, length: 99 }] }, false],
  ];
  for (const [index, [fields, expected]] of candidates.entries()) {
    const update = report();
    delete update.message.text;
    delete update.message.entities;
    Object.assign(update.message, fields);
    telegram.send(update.message.reply_to_message);
    const response = await dispatch({ update_id: index, edited_message: update.message });
    assert.equal(response.status, 200);
    assert.equal((await evidence()).some(row => row.update_id === index), expected, JSON.stringify(fields));
    assert.equal(telegram.has(81), true);
    assert.equal(telegram.canSend(22), true);
  }
});

test('user filters provenance: Given bot IDs and lookalike usernames, When messages arrive, Then only explicit blacklisted inline or forwarded bot origins are deleted', async () => {
  const cases = [
    [{ via_bot: { id: 273234066, is_bot: true, username: 'renamed_bot' } }, true],
    [{ via_bot: { id: 273234067, is_bot: true, username: 'PostBot' } }, false],
    [{ forward_origin: { type: 'user', sender_user: { id: 273234066, is_bot: true } } }, true],
    [{ forward_origin: { type: 'user', sender_user: { id: 273234066, is_bot: false } } }, false],
    [{ forward_origin: { type: 'hidden_user', sender_user_name: 'PostBot' } }, false],
    [{ forward_origin: { type: 'channel', chat: { id: -273234066, type: 'channel' } } }, false],
    [{ text: '@PostBot', reply_to_message: { via_bot: { id: 273234066, is_bot: true } } }, false],
    [{ from: { id: 273234066, is_bot: true } }, false],
  ];
  for (const [index, [fields, expected]] of cases.entries()) {
    telegram.reset();
    const target = { ...message(81 + index), ...fields };
    telegram.send(target);
    assert.equal((await dispatch({ update_id: index, edited_message: target })).status, 200);
    assert.equal(telegram.has(target.message_id), !expected, JSON.stringify(fields));
    assert.equal(telegram.canSend(22), !expected);
  }
});

test('user limits filtering to groups: Given a private chat, channel or basic group, When a rule matches, Then only group messages are deleted and no member is restricted', async () => {
  for (const type of ['private', 'channel', 'group']) {
    const target = { ...message(), chat: { ...chat, type }, via_bot: { id: 273234066, is_bot: true } };
    telegram.send(target);
    assert.equal((await dispatch({ update_id: 1, message: target })).status, 200);
    assert.equal(telegram.has(81), type !== 'group');
    assert.equal(telegram.canSend(22), true);
  }
});

test('user reports in a basic group: Given an administrator reply, When they mention the bot, Then only the target and report disappear without changing membership or history', async () => {
  const update = report();
  update.message.chat.type = 'group';
  update.message.reply_to_message.chat.type = 'group';
  telegram.send(update.message);
  telegram.send(update.message.reply_to_message);
  telegram.send({ ...message(80), chat: { ...chat, type: 'group' } });

  assert.equal((await dispatch(update)).status, 200);

  assert.equal(telegram.has(81), false);
  assert.equal(telegram.has(82), false);
  assert.equal(telegram.has(80), true);
  assert.deepEqual(telegram.members.get(22), { status: 'member' });
});

test('user rejects malformed updates: Given invalid identifiers or cross-chat replies, When delivered to the webhook, Then no message or membership changes', async () => {
  const target = message();
  const invalid = [
    [], { message: [] }, { message: target, edited_message: target },
    { message: { ...target, message_id: true } },
    { message: { ...target, chat: { id: 0, type: 'group' } } },
    { message: { ...target, chat: { id: -1, type: [] } } },
    { message: { ...target, via_bot: { id: '273234066', is_bot: true } } },
    { message: { ...target, via_bot: { id: 273234066, is_bot: false } } },
    { message: { ...target, forward_origin: { type: [] } } },
    report({ ...target, chat: { ...chat, id: -999 } }),
    report({ ...target, message_id: false }),
  ];
  telegram.send(target);
  for (const update of invalid) assert.equal((await dispatch(update)).status, 400, JSON.stringify(update));
  assert.equal(telegram.has(81), true);
  assert.equal(telegram.canSend(22), true);
  assert.deepEqual(await evidence(), []);
});

test('user authenticates delivery: Given a report, When its route, method, secret, encoding or media type is invalid, Then no evidence or moderation is accepted', async () => {
  const update = report();
  telegram.send(update.message.reply_to_message);
  const cases = [
    [{ headers: { ...headers, 'x-telegram-bot-api-secret-token': 'wrong' } }, 401],
    [{ headers: { ...headers, 'x-telegram-bot-api-secret-token': 'é' } }, 401],
    [{ headers: { 'content-type': 'application/json' } }, 401],
    [{ headers: { ...headers, 'x-telegram-bot-api-secret-token': 'wrong' }, body: '{' }, 401],
    [{ method: 'GET', body: undefined }, 405],
    [{ headers: { ...headers, 'content-type': 'text/plain' } }, 415],
    [{ body: '{' }, 400],
    [{ body: new Uint8Array([0xff]) }, 400],
  ];
  for (const [options, status] of cases) assert.equal((await dispatch(update, options)).status, status);
  assert.equal((await runtime.dispatchFetch('https://worker.test/')).status, 404);
  assert.equal(telegram.has(81), true);
  assert.equal(telegram.canSend(22), true);
  assert.deepEqual(await evidence(), []);
});

test('user bounds webhook payloads: Given a chunked request without a length header, When it crosses one MiB, Then the webhook rejects it but accepts the exact boundary', async () => {
  for (const [size, expected] of [[1_048_576, 200], [1_048_577, 413]]) {
    const body = new ReadableStream({ start(controller) {
      controller.enqueue(new TextEncoder().encode('{}'));
      controller.enqueue(new TextEncoder().encode(' '.repeat(size - 2)));
      controller.close();
    } });
    assert.equal((await dispatch({}, { body, duplex: 'half' })).status, expected);
  }
});

test('user expires evidence: Given records immediately before and at expiry, When the scheduled clock advances, Then only expired records are deleted in bounded batches', async () => {
  telegram.members.set(11, { status: 'member' });
  await dispatch(report());
  const [row] = await evidence();
  const worker = await runtime.getWorker();
  await worker.scheduled({ scheduledTime: new Date((row.expires_at - 1) * 1000), cron: '* * * * *' });
  assert.equal((await evidence()).length, 1);
  // Seed already-aged evidence through D1, not wall-clock sleeps or Python patches.
  await database.prepare(`INSERT INTO reports (bot_id, update_id, received_at, expires_at, raw_update)
    WITH RECURSIVE ids(n) AS (SELECT 1 UNION ALL SELECT n+1 FROM ids WHERE n<1001)
    SELECT 123, 100+n, ?, ?, '{}' FROM ids`).bind(row.received_at, row.expires_at).run();
  await database.prepare('UPDATE reports SET expires_at = ? WHERE update_id = 1101').bind(row.expires_at + 1).run();

  await worker.scheduled({ scheduledTime: new Date(row.expires_at * 1000), cron: '* * * * *' });
  assert.equal((await evidence()).length, 2);
  await worker.scheduled({ scheduledTime: new Date(row.expires_at * 1000), cron: '* * * * *' });
  assert.deepEqual((await evidence()).map(row => row.update_id), [1101]);
  await worker.scheduled({ scheduledTime: new Date((row.expires_at + 1) * 1000), cron: '* * * * *' });
  assert.deepEqual(await evidence(), []);
});

test('user survives an invalid membership response: Given a missing or mismatched member, When moderation checks authority, Then nobody is banned and delivery remains retryable', async () => {
  for (const result of [{}, { status: 'member', user: { id: 999 } }, { status: 'unknown', user: { id: 22 } }]) {
    telegram.send(message());
    telegram.faults.set('getChatMember:22', () => Response.json({ ok: true, result }));
    assert.equal((await dispatch(report())).status, 503);
    assert.equal(telegram.canSend(22), true);
    assert.equal((await evidence())[0].response_status, null);
  }
});

test('user keeps history when deletion fails: Given rejected or malformed Telegram responses, When the target cannot be deleted, Then no punishment follows and only temporary failures request redelivery', async () => {
  const cases = [
    [403, JSON.stringify({ ok: false, error_code: 403 }), 200],
    [500, JSON.stringify({ ok: false, error_code: 500 }), 503],
    [200, JSON.stringify({ ok: false, error_code: 500 }), 503],
    [200, JSON.stringify({ ok: false }), 503],
    [200, JSON.stringify({ ok: false, error_code: '400' }), 503],
    [200, JSON.stringify({ ok: false, error_code: true }), 503],
    [200, JSON.stringify({ ok: true, result: false }), 503],
    [200, JSON.stringify({ result: true }), 503],
    [200, 'not json', 503],
    [401, 'not json', 200],
    [200, 'x'.repeat(65_537), 503],
  ];
  for (const [index, [status, body, expected]] of cases.entries()) {
    telegram.send(message());
    telegram.faults.set('deleteMessage', () => new Response(body, { status }));
    const response = await dispatch({ update_id: index, message: { ...message(), via_bot: { id: 273234066, is_bot: true } } });
    assert.equal(response.status, expected);
    assert.equal(telegram.has(81), true);
    assert.equal(telegram.canSend(22), true);
  }
});

test('user: Given a ban awaiting confirmation and a duplicate report, When deletion temporarily fails afterward, Then redelivery still deletes the target', async () => {
  const entered = Promise.withResolvers(), release = Promise.withResolvers();
  const update = report();
  telegram.send(update.message.reply_to_message);
  telegram.send(update.message);
  telegram.faults.set('banChatMember', async () => {
    entered.resolve();
    await release.promise;
    telegram.members.set(22, { status: 'kicked' });
    return Response.json({ ok: true, result: true });
  });
  telegram.faults.set('deleteMessage:81', () => Response.json({ ok: false, error_code: 429 }, { status: 429 }));
  const pending = dispatch(update);
  await entered.promise;
  try {
    assert.equal((await dispatch(update)).status, 200);
  } finally {
    release.resolve();
    await pending;
  }
  assert.equal(telegram.has(81), true);
  telegram.faults.clear();
  assert.equal((await dispatch(update)).status, 200);
  assert.equal(telegram.has(81), false);
  assert.equal(telegram.has(82), false);
  assert.equal(telegram.canJoin(22), false);
});

test('user preserves concurrent completion: Given overlapping deliveries, When a late attempt fails after another succeeds, Then redelivery does not repeat a completed punishment', async () => {
  const entered = Promise.withResolvers(), release = Promise.withResolvers();
  telegram.faults.set('getChatMember:11', () => { entered.resolve(); return release.promise; });
  const update = report();
  telegram.send(update.message.reply_to_message);
  const pending = dispatch(update);
  await entered.promise;
  telegram.faults.clear();
  try {
    assert.equal((await dispatch(update)).status, 200);
  } finally {
    release.resolve(Response.json({ ok: false, error_code: 500 }, { status: 500 }));
    assert.equal((await pending).status, 503);
  }
  telegram.members.set(22, { status: 'member' });

  assert.equal((await dispatch(update)).status, 200);

  assert.equal(telegram.canJoin(22), true);
  assert.equal((await evidence())[0].response_status, 200);
});

test('user keeps an unban across overlapping reports: Given two deliveries of one report, When the delayed delivery resumes after completion and an administrator unban, Then new messages and membership survive', async () => {
  const entered = Promise.withResolvers(), release = Promise.withResolvers();
  telegram.faults.set('getChatMember:11', () => { entered.resolve(); return release.promise; });
  const update = report();
  telegram.send(update.message.reply_to_message);
  telegram.send(update.message);
  const pending = dispatch(update);
  await entered.promise;
  telegram.faults.clear();
  try {
    assert.equal((await dispatch(update)).status, 200);
    telegram.members.set(22, { status: 'member' });
    telegram.send(message(90));
  } finally {
    release.resolve(Response.json({ ok: true, result: { status: 'administrator', user: message(1, 11).from } }));
    await pending;
  }

  assert.equal(telegram.canJoin(22), true);
  assert.equal(telegram.has(90), true);
  assert.equal((await dispatch(update)).status, 200);
});

for (const failure of ['lost Telegram response', 'failed D1 checkpoint']) {
  test(`user keeps an unban after ${failure}: Given a ban that already took effect, When delivery resumes without a saved success, Then the bot leaves the new membership and messages unchanged`, async () => {
    const update = report();
    telegram.send(update.message.reply_to_message);
    telegram.send(update.message);
    if (failure === 'lost Telegram response') {
      telegram.faults.set('banChatMember', async () => {
        telegram.faults.clear();
        await telegram.fetch(new Request(`https://api.telegram.org/bot${token}/banChatMember`, {
          method: 'POST', headers: { 'content-type': 'application/json' },
          body: JSON.stringify({ chat_id: chat.id, user_id: 22, until_date: 0, revoke_messages: true }),
        }));
        return new Response('upstream response lost', { status: 502 });
      });
    } else {
      await database.prepare(`CREATE TRIGGER reject_checkpoint BEFORE UPDATE OF moderation_result ON reports
        BEGIN SELECT RAISE(ABORT, 'checkpoint unavailable'); END`).run();
    }
    try {
      assert.equal((await dispatch(update)).status, 503);
    } finally {
      await database.prepare('DROP TRIGGER IF EXISTS reject_checkpoint').run();
    }
    assert.equal(telegram.canJoin(22), false);
    telegram.members.set(22, { status: 'member' });
    telegram.send(message(90));

    const response = await dispatch(update);

    assert.equal(response.status, 200);
    assert.match(await response.text(), /check membership/);
    assert.equal(telegram.canJoin(22), true);
    assert.equal(telegram.has(90), true);
    assert.equal(telegram.has(82), true);
  });
}

for (const failure of ['server error', 'unexpected success payload']) {
  test(`user retries an unconfirmed report deliberately: Given an unconfirmed ban (${failure}), When the same delivery repeats, Then messages remain until an administrator sends a new report`, async () => {
    const update = report();
    telegram.send(update.message.reply_to_message);
    telegram.send(update.message);
    telegram.faults.set('banChatMember', () => failure === 'server error'
      ? Response.json({ ok: false, error_code: 500 }, { status: 500 })
      : Response.json({ ok: true, result: false }));
    assert.equal((await dispatch(update)).status, 503);
    telegram.faults.clear();

    assert.equal((await dispatch(update)).status, 200);
    assert.equal(telegram.canJoin(22), true);
    assert.equal(telegram.has(81), true);
    assert.equal(telegram.has(82), true);

    const newReport = { ...report(), update_id: 72 };
    newReport.message.message_id = 83;
    telegram.send(newReport.message);
    assert.equal((await dispatch(newReport)).status, 200);
    assert.equal(telegram.canJoin(22), false);
    assert.equal(telegram.has(81), false);
    assert.equal(telegram.has(83), false);
  });
}
