import assert from 'node:assert/strict';
import { after, afterEach, before, beforeEach, test } from 'node:test';
import { readdir, readFile } from 'node:fs/promises';
import { resolve } from 'node:path';
import { Miniflare, convertV4MiniflareOptions } from 'miniflare';
import { unstable_getMiniflareWorkerOptions } from 'wrangler';
import { chat, message, report, Telegram, token, username } from './telegram-fake.mjs';

const telegram = new Telegram();
let runtime, database;
const secret = 'test-secret';
const headers = { 'content-type': 'application/json', 'x-telegram-bot-api-secret-token': secret };

before(async () => {
  const root = resolve('.wrangler/test-build');
  const paths = (await readdir(root, { recursive: true })).filter(path => path.endsWith('.py') && path !== 'entry.py');
  const { workerOptions } = unstable_getMiniflareWorkerOptions('wrangler.jsonc');
  runtime = new Miniflare(convertV4MiniflareOptions({
    ...workerOptions,
    modulesRoot: root,
    modules: ['entry.py', ...paths].map(path => ({ type: 'PythonModule', path: resolve(root, path) })),
    bindings: { ...workerOptions.bindings, BOT_TOKEN: token, TELEGRAM_WEBHOOK_SECRET: secret, BOT_USERNAME: username },
    outboundService: request => telegram.fetch(request),
  }));
  database = await runtime.getD1Database('REPORTS');
  for (const file of (await readdir('migrations')).filter(path => path.endsWith('.sql')).sort()) {
    for (const sql of (await readFile(`migrations/${file}`, 'utf8')).split(';').filter(sql => sql.trim())) {
      await database.prepare(sql).run();
    }
  }
});
after(async () => { await runtime?.dispose(); });
beforeEach(async () => {
  telegram.reset();
  await database.prepare('DELETE FROM reports').run();
});
afterEach(() => { assert.deepEqual(telegram.violations, []); });

function dispatch(update, options = {}) {
  return runtime.dispatchFetch('https://worker.test/webhook', {
    method: 'POST', headers, body: JSON.stringify(update), ...options,
  });
}

async function evidence() {
  return (await database.prepare('SELECT * FROM reports ORDER BY update_id').all()).results;
}

test('user keeps history: Given an old message, When a blacklisted inline message arrives, Then only the new message disappears and the sender is permanently muted', async () => {
  const spam = { ...message(), via_bot: { id: 273234066, is_bot: true, first_name: 'Source' } };
  telegram.send(message(80));
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
  assert.deepEqual(await evidence(), []);
});

test('user bans reported spam: Given an administrator reply mentioning the bot, When the report arrives, Then the sender cannot rejoin and their history is removed', async () => {
  const update = report();
  telegram.send(message(80));
  telegram.send(update.message.reply_to_message);
  telegram.send(update.message);
  telegram.send(message(79, 11));

  const response = await dispatch(update);

  assert.equal(response.status, 200);
  assert.equal(telegram.canJoin(22), false);
  assert.equal(telegram.canSend(22), false);
  assert.equal(telegram.has(80), false);
  assert.equal(telegram.has(81), false);
  assert.equal(telegram.has(82), false);
  assert.equal(telegram.has(79), true);
  const [row] = await evidence();
  assert.deepEqual(JSON.parse(row.raw_update), update);
  assert.equal(row.expires_at - row.received_at, 259200);
});

test('user reports old spam: Given a target older than the individual deletion limit, When an administrator reports it, Then the ban still removes its history and prevents rejoining', async () => {
  const target = { ...message(), date: 1 };
  telegram.send(target);
  telegram.send(message(80));
  telegram.faults.set('deleteMessage:81', () => Response.json({ ok: false, error_code: 400, description: "Bad Request: message can't be deleted" }, { status: 400 }));

  const response = await dispatch(report(target));

  assert.equal(response.status, 200);
  assert.equal(telegram.has(81), false);
  assert.equal(telegram.has(80), false);
  assert.equal(telegram.canJoin(22), false);
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
  telegram.send(target);
  telegram.send(update.message);

  const response = await dispatch(update);

  assert.equal(await response.text(), 'report recorded');
  assert.equal(telegram.has(81), true);
  assert.equal(telegram.has(82), true);
  assert.equal(telegram.canSend(22), true);
  const [row] = await evidence();
  assert.deepEqual(JSON.parse(row.raw_update), update);
  assert.deepEqual(JSON.parse(row.classification), {
    version: 1, content_types: ['animation', 'document', 'photo', 'rich_message', 'text'],
    media_fields: ['animation', 'document', 'photo'], via_bot_present: true, present_fields: Object.keys(target).sort(),
  });
});

test('user searches non-text evidence: Given media and service-message reports, When recorded, Then their content types remain searchable alongside the original JSON', async () => {
  telegram.members.set(11, { status: 'member' });
  const kinds = ['audio', 'live_photo', 'paid_media', 'sticker', 'story', 'video', 'video_note', 'voice',
    'checklist', 'contact', 'dice', 'game', 'poll', 'venue', 'location', 'gift', 'unique_gift', 'invoice',
    'successful_payment', 'new_chat_members', 'forum_topic_created', 'video_chat_started', 'web_app_data', 'giveaway'];
  for (const [index, kind] of kinds.entries()) {
    const target = { ...message(), [kind]: { sample: kind } };
    delete target.text;
    const update = { ...report(target), update_id: index };
    assert.equal((await dispatch(update)).status, 200);
    const row = (await evidence()).find(row => row.update_id === index);
    assert.deepEqual(JSON.parse(row.classification).content_types, [kind]);
    assert.deepEqual(JSON.parse(row.raw_update), update);
  }
});

for (const status of ['creator', 'administrator']) {
  for (const automatic of [false, true]) {
    test(`user protects ${status}: Given their old and targeted messages, When ${automatic ? 'an automatic match' : 'an owner report'} arrives, Then the target disappears but history and privileges remain`, async () => {
      telegram.members.set(11, { status: 'creator' });
      telegram.members.set(22, { status });
      const target = message();
      const update = automatic
        ? { update_id: 71, message: { ...target, via_bot: { id: 273234066, is_bot: true } } }
        : report(target);
      telegram.send(target);
      telegram.send(message(80));
      if (!automatic) telegram.send(update.message);

      const response = await dispatch(update);

      assert.equal(response.status, 200);
      assert.match(await response.text(), /(?:ban|mute) skipped/);
      assert.equal(telegram.has(81), false);
      assert.equal(telegram.has(80), true);
      assert.deepEqual(telegram.members.get(22), { status });
    });
  }
}

for (const senderChat of [chat, { id: -100999, type: 'channel', title: 'Linked channel' }]) {
  test(`user reports as ${senderChat.type}: Given an anonymous reply, When it mentions the bot, Then only the own-group identity authorizes banning`, async () => {
    const update = report();
    update.message.sender_chat = senderChat;
    update.message.from = { id: 1087968824, is_bot: true, first_name: 'Group', username: 'GroupAnonymousBot' };
    telegram.send(update.message.reply_to_message);
    telegram.send(update.message);
    const authorized = senderChat.id === chat.id;

    assert.equal((await dispatch(update)).status, 200);

    assert.equal(telegram.canJoin(22), !authorized);
    assert.equal(telegram.has(81), !authorized);
    assert.equal(telegram.has(82), !authorized);
    assert.equal((await evidence()).length, authorized ? 1 : 0);
  });
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

test('user reports without joining: Given a commenter with left status, When a rule matches their message, Then they are muted without losing history', async () => {
  telegram.members.set(22, { status: 'left' });
  telegram.send(message(80));
  const spam = { ...message(), via_bot: { id: 273234066, is_bot: true } };
  telegram.send(spam);

  assert.equal((await dispatch({ message: spam })).status, 200);

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

test('user retains evidence during failure: Given unavailable storage, When a report arrives, Then nothing is moderated while automatic filtering still works', async () => {
  await database.prepare('ALTER TABLE reports RENAME TO unavailable_reports').run();
  try {
    const update = report();
    telegram.send(update.message.reply_to_message);
    assert.equal((await dispatch(update)).status, 503);
    assert.equal(telegram.has(81), true);
    assert.equal(telegram.canSend(22), true);
    update.message = { ...message(), via_bot: { id: 273234066, is_bot: true } };
    assert.equal((await dispatch(update)).status, 200);
    assert.equal(telegram.has(81), false);
    assert.equal(telegram.canSend(22), false);
  } finally {
    await database.prepare('ALTER TABLE unavailable_reports RENAME TO reports').run();
  }
});

for (const stage of ['getChatMember:11', 'getChatMember:22', 'banChatMember', 'deleteMessage:82']) {
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
    if (stage !== 'deleteMessage:82') {
      assert.equal(telegram.canJoin(22), true);
      assert.equal(telegram.has(80), true);
    }
    telegram.faults.clear();
    assert.equal((await dispatch(update)).status, 200);

    assert.equal(telegram.canJoin(22), false);
    assert.equal(telegram.has(80), false);
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
    [{ text: '😀 @NIUQU_ICN_BOT', entities: [{ type: 'mention', offset: 3, length: 14 }] }, true],
    [{ caption: '@niuqu_icn_bot', caption_entities: [{ type: 'mention', offset: 0, length: 14 }] }, true],
    [{ entities: [{ type: 'text_mention', user: { id: 999, username: 'NIUQU_ICN_BOT' } }] }, true],
    [{ entities: [{ type: 'text_mention', user: { id: 123, username: 'other_bot' } }] }, false],
    [{ entities: [{ type: 'text_mention', user: { id: 123, first_name: username } }] }, false],
    [{ text: '@niuqu_icn_bot_extra', entities: [{ type: 'mention', offset: 0, length: 20 }] }, false],
    [{ text: '@niuqu_icn_bot' }, false],
    [{ text: '😀 @niuqu_icn_bot', entities: [{ type: 'mention', offset: 2, length: 14 }] }, false],
    [{ text: '@niuqu_icn_bot', entities: [{ type: 'mention', offset: 0, length: 99 }] }, false],
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
  for (const [fields, expected] of cases) {
    telegram.reset();
    const target = { ...message(), ...fields };
    telegram.send(target);
    assert.equal((await dispatch({ edited_message: target })).status, 200);
    assert.equal(telegram.has(81), !expected, JSON.stringify(fields));
    assert.equal(telegram.canSend(22), !expected);
  }
});

test('user limits filtering to groups: Given a private chat, channel or basic group, When a rule matches, Then only group messages are deleted and no member is restricted', async () => {
  for (const type of ['private', 'channel', 'group']) {
    const target = { ...message(), chat: { ...chat, type }, via_bot: { id: 273234066, is_bot: true } };
    telegram.send(target);
    assert.equal((await dispatch({ message: target })).status, 200);
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
  await database.prepare(`INSERT INTO reports (bot_id, update_id, received_at, expires_at, raw_update, classification)
    WITH RECURSIVE ids(n) AS (SELECT 1 UNION ALL SELECT n+1 FROM ids WHERE n<1001)
    SELECT 123, 100+n, ?, ?, '{}', '{}' FROM ids`).bind(row.received_at, row.expires_at).run();
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

test('user upgrades pending reports: Given legacy cleanup and failed-ban records, When migrations run and deliveries resume, Then completed actions stay completed while an unattempted ban can still proceed', async () => {
  const statements = [database.prepare('DROP TABLE reports')];
  for (const sql of (await readFile('migrations/0001_reports.sql', 'utf8')).split(';').filter(sql => sql.trim())) {
    statements.push(database.prepare(sql));
  }
  const outcomes = ['deleted; banned; report cleanup pending', 'already absent; ban skipped; report cleanup pending',
    'ban failed', 'report recorded; authority check failed'];
  for (const [index, outcome] of outcomes.entries()) {
    statements.push(database.prepare(`INSERT INTO reports
      (bot_id, update_id, received_at, expires_at, raw_update, classification, response_body)
      VALUES (123, ?, 100, 259300, ?, '{}', ?)`).bind(index, JSON.stringify({ ...report(), update_id: index }), outcome));
  }
  for (const sql of (await readFile('migrations/0002_report_progress.sql', 'utf8')).split(';').filter(sql => sql.trim())) {
    statements.push(database.prepare(sql));
  }
  await database.batch(statements);
  telegram.send(message(90));
  for (const update_id of [0, 1, 2]) {
    assert.equal((await dispatch({ ...report(), update_id })).status, 200);
    assert.equal(telegram.canJoin(22), true);
    assert.equal(telegram.has(90), true);
  }
  telegram.send(message());
  assert.equal((await dispatch({ ...report(), update_id: 3 })).status, 200);
  assert.equal(telegram.canJoin(22), false);
  assert.equal(telegram.has(90), false);
  for (const row of await evidence()) {
    assert.deepEqual(JSON.parse(row.raw_update), { ...report(), update_id: row.update_id });
    assert.equal(row.expires_at, 259300);
  }
});
