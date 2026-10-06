import assert from 'node:assert/strict';
import { test } from 'node:test';
import { database, dispatch, setBindings, telegram, tick } from './worker-runtime.mjs';
import { chat, message, report } from './telegram-fake.mjs';

const pack = 'Campaign_Pack_by_ExampleBot';
const normalizedPack = pack.toLowerCase();
const packUrl = `https://t.me/addstickers/${pack}`;

function sticker(id = 81, sender = 22, setName = pack) {
  const result = { ...message(id, sender), sticker: {
    set_name: setName, file_id: `file-${id}`, file_unique_id: `unique-${id}`,
  } };
  delete result.text;
  return result;
}

function command(text = '/bs', target, id = 82) {
  return { update_id: id, message: { ...message(id, 11), text,
    entities: [{ type: 'bot_command', offset: 0, length: text.split(/\s/u)[0].length }],
    ...(target ? { reply_to_message: target } : {}),
  } };
}

async function deliver(update) {
  telegram.send(update.message ?? update.edited_message);
  return dispatch(update);
}

async function history(sender = 22, id = 70) {
  const earlier = message(id, sender);
  assert.equal((await deliver({ message: earlier })).status, 200);
}

const savedPacks = () => database.prepare('SELECT bot_id, set_name FROM blacklisted_sticker_sets ORDER BY bot_id, set_name').all();

test('user: Given an unknown sticker pack, When a reporter registers its URL, Then subsequent different stickers are deleted and muted without banning or clearing history', async () => {
  await history();
  assert.equal((await deliver({ message: sticker() })).status, 200);
  assert.equal(telegram.has(81), true);
  assert.equal(telegram.canSend(22), true);
  assert.equal((await deliver(command(`/bs ${packUrl}`))).status, 200);
  assert.equal(telegram.canSend(22), true);
  telegram.members.set(33, { status: 'member' });
  await history(33, 71);
  const later = sticker(83, 33, normalizedPack);
  assert.equal((await deliver({ message: later })).status, 200);
  assert.equal(telegram.has(83), false, 'the newly registered pack must be filtered immediately');
  assert.equal(telegram.canSend(33), false);
  assert.equal(telegram.canJoin(33), true);
  assert.equal(telegram.canSend(22), true);
  assert.equal(telegram.has(71), true);
  for (const id of [70, 81]) assert.equal(telegram.has(id), true);
  assert.deepEqual((await savedPacks()).results, [{ bot_id: 123, set_name: normalizedPack }]);
  assert.equal((await database.prepare('SELECT COUNT(*) AS n FROM blacklisted_users').first()).n, 0);
});

test('user: Given a sticker sender with indexed history, When a reporter replies with bare bs, Then the whole pack is registered and the sender is banned and cleaned', async () => {
  await history();
  const target = sticker();
  telegram.send(target);
  assert.equal((await deliver(command('/bs', target))).status, 200);
  assert.equal(telegram.canJoin(22), false, 'bare bs must ban the sticker sender');
  for (const id of [70, 81, 82]) assert.equal(telegram.has(id), false);
  assert.deepEqual((await savedPacks()).results, [{ bot_id: 123, set_name: normalizedPack }]);
  assert.deepEqual((await database.prepare('SELECT user_id FROM blacklisted_users').all()).results, [{ user_id: 22 }]);
});

test('user: Given a sticker delivered through an inline bot, When a reporter replies with bare bs, Then both sources are registered and both accounts are banned and cleaned', async () => {
  telegram.members.set(777, { status: 'member' });
  await history();
  await history(777, 71);
  const target = { ...sticker(), via_bot: { id: 777, is_bot: true } };
  telegram.send(target);
  assert.equal((await deliver(command('/bs', target))).status, 200);
  for (const id of [22, 777]) assert.equal(telegram.canJoin(id), false);
  for (const id of [70, 71, 81, 82]) assert.equal(telegram.has(id), false);
  // Probe the registered pack through another sender before inspecting new storage.
  telegram.members.set(33, { status: 'member' });
  assert.equal((await deliver({ message: sticker(83, 33) })).status, 200);
  assert.equal(telegram.canSend(33), false, 'the sticker pack must be registered alongside via_bot');
  assert.deepEqual((await savedPacks()).results, [{ bot_id: 123, set_name: normalizedPack }]);
  assert.equal((await database.prepare('SELECT source_id FROM blacklisted_sources WHERE source_id=777').first()).source_id, 777);
});

test('user: Given a registered individual sticker, When its ID differs only in case or the message has a reference, Then unrelated or contextual stickers remain visible', async () => {
  const target = sticker();
  delete target.sticker.set_name;
  target.sticker.file_unique_id = 'Unique-81';
  telegram.send(target);
  await deliver(command('/bs', target));
  telegram.members.set(33, { status: 'member' });
  for (const [index, metadata, reference] of [
    [0, { file_unique_id: 'unique-81' }, false],
    [1, { file_unique_id: 'Unique-81', set_name: 'Ordinary_Pack' }, true],
    [2, { file_id: 'file-81' }, false],
  ]) {
    const current = sticker(90 + index, 33);
    current.sticker = metadata;
    if (reference) current.reply_to_message = message(70, 11);
    assert.equal((await deliver({ message: current })).status, 200);
    assert.equal(telegram.has(current.message_id), true);
    assert.equal(telegram.canSend(33), true);
  }
  const current = sticker(94, 33, 'Ordinary_Pack');
  current.sticker.file_unique_id = 'Unique-81';
  assert.equal((await deliver({ message: current })).status, 200);
  assert.equal(telegram.has(94), false);
  assert.equal(telegram.canSend(33), false);
});

for (const identity of ['unlisted', 'administrator target', 'other bot list', 'expired', 'external quote']) {
  test(`user: Given an individual sticker and ${identity}, When registration or filtering occurs, Then shared authorization and protection stay intact`, async () => {
    const target = sticker();
    delete target.sticker.set_name;
    telegram.send(target);
    const update = command('/bs', target);
    if (identity === 'unlisted') update.message.from.id = 33;
    if (identity === 'administrator target') telegram.members.set(22, { status: 'administrator' });
    assert.equal((await deliver(update)).status, 200);
    assert.equal(telegram.canJoin(22), identity === 'unlisted' || identity === 'administrator target');
    telegram.members.set(33, { status: 'member' });
    if (identity === 'other bot list') {
      await database.prepare('UPDATE blacklisted_stickers SET bot_id=456').run();
    }
    const current = sticker(83, 33);
    delete current.sticker.set_name;
    current.sticker.file_unique_id = 'unique-81';
    if (identity === 'expired') current.date -= 49 * 3600;
    if (identity === 'external quote') current.quote = { text: 'This sticker is an ad' };
    const filtered = identity === 'administrator target';
    assert.equal((await deliver({ message: current })).status, 200);
    assert.equal(telegram.has(83), !filtered);
    assert.equal(telegram.canSend(33), !filtered);
  });
}

for (const uniqueId of [undefined, '', 123, 'bad id', 'x'.repeat(129)]) {
  test(`user: Given no pack and unusable sticker identity ${JSON.stringify(uniqueId)}, When bare bs replies, Then usage does not register or punish`, async () => {
    const target = sticker();
    target.sticker = { file_id: 'file-81', file_unique_id: uniqueId };
    telegram.send(target);
    assert.equal((await deliver(command('/bs', target))).status, 200);
    assert.equal(telegram.canJoin(22), true);
    assert.equal(telegram.has(81), true);
    assert.equal((await database.prepare('SELECT COUNT(*) AS n FROM blacklisted_stickers').first()).n, 0);
  });
}

for (const failure of ['registration', 'lookup']) {
  test(`user: Given unavailable individual sticker ${failure} storage, When delivery retries after recovery, Then no punishment precedes durable registration or lookup`, async () => {
    const target = sticker();
    delete target.sticker.set_name;
    telegram.send(target);
    const update = command('/bs', target);
    if (failure === 'lookup') {
      await deliver(update);
      telegram.members.set(22, { status: 'member' });
      telegram.send(target);
    }
    telegram.members.set(33, { status: 'member' });
    const probe = sticker(83, 33);
    delete probe.sticker.set_name;
    probe.sticker.file_unique_id = 'unique-81';
    const pending = failure === 'registration' ? update : { update_id: 83, message: probe };
    await unavailable('blacklisted_stickers', async () => {
      assert.equal((await deliver(pending)).status, 503);
      assert.equal(telegram.has(failure === 'registration' ? 81 : 83), true);
      assert.equal(telegram.canSend(failure === 'registration' ? 22 : 33), true);
    });
    assert.equal((await dispatch(pending)).status, 200);
    assert.equal(telegram.has(failure === 'registration' ? 81 : 83), false);
    assert.equal(failure === 'registration' ? telegram.canJoin(22) : telegram.canSend(33), false);
  });
}

async function registerPack() {
  assert.equal((await deliver(command(`/bs ${packUrl}`, undefined, 300))).status, 200);
}

const noPacks = async () => assert.deepEqual((await savedPacks()).results, []);
const retryable = () => Response.json({ ok: false, error_code: 429 }, { status: 429 });

async function unavailable(table, run) {
  await database.prepare(`ALTER TABLE ${table} RENAME TO unavailable_${table}`).run();
  try { await run(); }
  finally { await database.prepare(`ALTER TABLE unavailable_${table} RENAME TO ${table}`).run(); }
}

for (const botSender of [false, true]) {
  test(`user: Given a ${botSender ? 'bot' : 'human'} sticker sender, When a reporter mentions the bot, Then only the sender is banned without registering either source`, async () => {
    telegram.members.set(777, { status: 'member' });
    await history();
    const target = { ...sticker(), via_bot: { id: 777, is_bot: true } };
    target.from.is_bot = botSender;
    telegram.send(target);
    assert.equal((await deliver(report(target))).status, 200);
    assert.equal(telegram.canJoin(22), false);
    assert.equal(telegram.canJoin(777), true);
    for (const id of [70, 81, 82]) assert.equal(telegram.has(id), false);
    await noPacks();
    assert.equal(await database.prepare('SELECT source_id FROM blacklisted_sources WHERE source_id=777').first(), null);
    telegram.members.set(33, { status: 'member' });
    assert.equal((await deliver({ message: sticker(83, 33) })).status, 200);
    assert.equal(telegram.has(83), true);
    assert.equal(telegram.canSend(33), true);
  });
}

test('user: Given the same bot as sticker sender and inline source, When bare bs reports it, Then the pack and bot are registered with one account ban', async () => {
  const target = { ...sticker(), via_bot: { id: 22, is_bot: true } };
  target.from.is_bot = true;
  telegram.send(target);
  assert.equal((await deliver(command('/bs@test_gate_bot', target))).status, 200);
  assert.equal(telegram.canJoin(22), false);
  assert.equal(telegram.has(81), false);
  assert.deepEqual((await database.prepare('SELECT user_id FROM blacklisted_users').all()).results, [{ user_id: 22 }]);
  assert.equal((await database.prepare('SELECT source_id FROM blacklisted_sources WHERE source_id=22').first()).source_id, 22);
  assert.deepEqual((await savedPacks()).results, [{ bot_id: 123, set_name: normalizedPack }]);
});

test('user: Given a sticker and inline bot in the reply target, When an explicit pack URL is registered, Then registration leaves both accounts and the target untouched', async () => {
  telegram.members.set(777, { status: 'member' });
  await history();
  const target = { ...sticker(), via_bot: { id: 777, is_bot: true } };
  telegram.send(target);
  assert.equal((await deliver(command(`/bs ${packUrl}`, target))).status, 200);
  for (const id of [22, 777]) assert.equal(telegram.canJoin(id), true);
  for (const id of [70, 81]) assert.equal(telegram.has(id), true);
  assert.equal(telegram.has(82), false);
  assert.deepEqual((await savedPacks()).results, [{ bot_id: 123, set_name: normalizedPack }]);
  assert.equal(await database.prepare('SELECT source_id FROM blacklisted_sources WHERE source_id=777').first(), null);
});

for (const [identity, configure] of [
  ['unlisted administrator', async update => { update.message.from.id = 33; telegram.members.set(33, { status: 'administrator' }); }],
  ['empty reporter list', async () => { await setBindings({ REPORTER_IDS: '' }); }],
  ['unlisted sender-chat with a listed compatibility user', async update => { update.message.sender_chat = { ...chat, id: -10099 }; }],
]) {
  test(`user: Given an ${identity}, When they reply to a sticker with bare bs, Then only evidence is saved without registering or punishing`, async () => {
    const target = sticker();
    const update = command('/bs', target);
    await configure(update);
    telegram.send(target);
    assert.equal((await deliver(update)).status, 200);
    for (const id of [81, 82]) assert.equal(telegram.has(id), true);
    assert.equal(telegram.canJoin(22), true);
    assert.deepEqual(telegram.replies, []);
    await noPacks();
    assert.equal((await database.prepare('SELECT COUNT(*) AS n FROM reports').first()).n, 1);
  });
}

test('user: Given a listed ordinary member, When they register a sticker in reply, Then registration and the ban do not require reporter administrator status', async () => {
  telegram.members.set(11, { status: 'member' });
  const target = sticker();
  telegram.send(target);
  assert.equal((await deliver(command('/bs', target))).status, 200);
  assert.equal(telegram.canJoin(22), false);
  assert.deepEqual((await savedPacks()).results, [{ bot_id: 123, set_name: normalizedPack }]);
});

for (const [label, configure, status] of [
  ['a forum topic root', update => { update.message.reply_to_message.forum_topic_created = { name: 'Topic' }; }, 200],
  ['a different group target', update => { update.message.reply_to_message.chat.id = -10099; }, 400],
  ['a private chat', update => { update.message.chat = { id: 11, type: 'private' }; update.message.reply_to_message.chat = { id: 11, type: 'private' }; }, 200],
  ['an edited command', update => { update.edited_message = update.message; delete update.message; }, 200],
  ['a command addressed to another bot', update => { update.message.text = '/bs@other_bot'; update.message.entities[0].length = 13; }, 200],
]) {
  test(`user: Given ${label}, When bare bs refers to a sticker, Then no pack is registered or sender punished`, async () => {
    const update = command('/bs', sticker());
    configure(update);
    const target = (update.message ?? update.edited_message).reply_to_message;
    telegram.send(target);
    assert.equal((await deliver(update)).status, status);
    assert.equal(telegram.has(81, target.chat.id), true);
    assert.equal(telegram.canJoin(22), true);
    await noPacks();
  });
}

for (const argument of [
  'http://t.me/addstickers/Valid_Pack', 'https://t.me.example.invalid/addstickers/Valid_Pack',
  'https://t.me/addstickers/Valid_Pack/extra', 'https://t.me/addstickers/Valid_Pack?x=1',
  'https://t.me/addstickers/Valid_Pack#x', '@https://t.me/addstickers/Valid_Pack',
  'https://t.me/addstickers/', 'https://t.me/addstickers/Invalid-Name',
  'https://t.me/addstickers/包名', 'https://t.me/addstickers/1invalid',
  'https://t.me/addstickers/Invalid__Name', `https://t.me/addstickers/${'a'.repeat(65)}`,
]) {
  test(`user: Given the invalid explicit argument ${argument}, When bs replies to a valid sticker and inline bot, Then it shows usage without falling back to source reporting`, async () => {
    telegram.members.set(777, { status: 'member' });
    const target = { ...sticker(), via_bot: { id: 777, is_bot: true } };
    telegram.send(target);
    assert.equal((await deliver(command(`/bs ${argument}`, target))).status, 200);
    for (const id of [22, 777]) assert.equal(telegram.canJoin(id), true);
    assert.equal(telegram.has(81), true);
    assert.match(telegram.replies.at(-1).text, /\/bs/);
    await noPacks();
    assert.equal(await database.prepare('SELECT source_id FROM blacklisted_sources WHERE source_id=777').first(), null);
  });
}

for (const viaBot of [false, true]) {
  test(`user: Given a sticker without a pack ${viaBot ? 'and a valid inline source' : 'or inline source'}, When bare bs reports it, Then ${viaBot ? 'the sticker and bot source are registered and both accounts banned' : 'the individual sticker is registered and its sender banned'}`, async () => {
    const target = sticker();
    delete target.sticker.set_name;
    if (viaBot) {
      telegram.members.set(777, { status: 'member' });
      target.via_bot = { id: 777, is_bot: true };
    }
    telegram.send(target);
    assert.equal((await deliver(command('/bs', target))).status, 200);
    assert.equal(telegram.canJoin(22), false);
    assert.equal(telegram.has(81), false);
    await noPacks();
    if (viaBot) {
      assert.equal(telegram.canJoin(777), false);
      assert.equal((await database.prepare('SELECT source_id FROM blacklisted_sources WHERE source_id=777').first()).source_id, 777);
    }
    telegram.members.set(33, { status: 'member' });
    const repeated = sticker(83, 33);
    delete repeated.sticker.set_name;
    repeated.sticker.file_unique_id = target.sticker.file_unique_id;
    assert.equal((await deliver({ message: repeated })).status, 200);
    assert.equal(telegram.has(83), false, 'a different file_id must not bypass the individual sticker list');
    assert.equal(telegram.canSend(33), false);
    assert.equal(telegram.canJoin(33), true);
    assert.deepEqual((await database.prepare('SELECT bot_id, file_unique_id FROM blacklisted_stickers').all()).results,
      [{ bot_id: 123, file_unique_id: 'unique-81' }]);
  });
}

test('user: Given a permanent individual sticker entry, When reports repeat, evidence expires, and the entry is removed, Then its first timestamp survives until removal and subsequent stickers become allowed', async () => {
  const target = sticker();
  delete target.sticker.set_name;
  telegram.send(target);
  await deliver(command('/bs', target));
  await database.prepare('UPDATE blacklisted_stickers SET added_at=1').run();
  await deliver(command('/bs', target, 301));
  await tick(Math.floor(Date.now() / 1000) + 73 * 3600);
  assert.deepEqual((await database.prepare('SELECT added_at FROM blacklisted_stickers').all()).results, [{ added_at: 1 }]);
  assert.equal((await database.prepare('SELECT COUNT(*) AS n FROM reports').first()).n, 0);
  await database.prepare('DELETE FROM blacklisted_stickers WHERE bot_id=123').run();
  telegram.members.set(33, { status: 'member' });
  const current = sticker(83, 33);
  delete current.sticker.set_name;
  current.sticker.file_unique_id = 'unique-81';
  assert.equal((await deliver({ message: current })).status, 200);
  assert.equal(telegram.has(83), true);
  assert.equal(telegram.canSend(33), true);
});

test('user: Given a scoped pack registration, When its record is removed, Then subsequent messages are immediately allowed even if another bot still blocks the same pack', async () => {
  await registerPack();
  await database.prepare('INSERT INTO blacklisted_sticker_sets (bot_id, set_name, added_at) VALUES (456, ?, 1)').bind(normalizedPack).run();
  assert.equal((await deliver({ message: sticker() })).status, 200);
  assert.equal(telegram.canSend(22), false);
  assert.equal(telegram.has(81), false);
  await database.prepare('DELETE FROM blacklisted_sticker_sets WHERE bot_id=123 AND set_name=?').bind(normalizedPack).run();
  telegram.members.set(33, { status: 'member' });
  assert.equal((await deliver({ message: sticker(83, 33) })).status, 200);
  assert.equal(telegram.canSend(33), true);
  assert.equal(telegram.has(83), true);
  assert.deepEqual((await savedPacks()).results, [{ bot_id: 456, set_name: normalizedPack }]);
});

test('user: Given a registered pack, When commands repeat and temporary records expire, Then the first registration time and permanent entry survive', async () => {
  await registerPack();
  await database.prepare('UPDATE blacklisted_sticker_sets SET added_at=1 WHERE bot_id=123').run();
  assert.equal((await deliver(command(`/bs ${packUrl.toLowerCase()}`, undefined, 301))).status, 200);
  assert.deepEqual((await database.prepare('SELECT added_at FROM blacklisted_sticker_sets WHERE bot_id=123').all()).results, [{ added_at: 1 }]);
  await tick(Math.floor(Date.now() / 1000) + 73 * 3600);
  assert.equal((await database.prepare('SELECT COUNT(*) AS n FROM reports').first()).n, 0);
  assert.deepEqual((await savedPacks()).results, [{ bot_id: 123, set_name: normalizedPack }]);
});

for (const [label, configure, filtered] of [
  ['a same-chat reply', current => { current.reply_to_message = message(70); }, false],
  ['an external reply', current => { current.external_reply = { origin: { type: 'hidden_user', sender_user_name: 'Other', date: current.date } }; }, false],
  ['a quote', current => { current.quote = { text: 'quoted', position: 0 }; }, false],
  ['a topic root attachment', current => { current.reply_to_message = { ...message(70), forum_topic_created: { name: 'Topic' } }; }, true],
  ['an expired message', current => { current.date -= 49 * 3600; }, false],
  ['a future message', current => { current.date += 3600; }, false],
]) {
  test(`user: Given a registered sticker with ${label}, When it arrives, Then ${filtered ? 'the topic attachment does not bypass filtering' : 'the local pack rule leaves it unchanged'}`, async () => {
    await registerPack();
    const current = sticker();
    configure(current);
    assert.equal((await deliver({ message: current })).status, 200);
    assert.equal(telegram.has(81), !filtered);
    assert.equal(telegram.canSend(22), !filtered);
  });
}

test('user: Given an unlisted current sticker replying to a listed pack, When delivered, Then the referenced pack does not punish the current sender', async () => {
  await registerPack();
  const current = sticker(81, 22, 'Normal_Pack');
  current.reply_to_message = sticker(70, 11);
  assert.equal((await deliver({ message: current })).status, 200);
  assert.equal(telegram.has(81), true);
  assert.equal(telegram.canSend(22), true);
});

test('user: Given an already received sticker, When an unreferenced edit changes it to a registered pack, Then the edit is deleted and the sender muted', async () => {
  await registerPack();
  assert.equal((await deliver({ message: sticker(81, 22, 'Normal_Pack') })).status, 200);
  assert.equal(telegram.has(81), true);
  assert.equal((await deliver({ update_id: 90, edited_message: sticker() })).status, 200);
  assert.equal(telegram.has(81), false);
  assert.equal(telegram.canSend(22), false);
});

for (const [label, metadata] of [
  ['a prefix', { set_name: 'Campaign_Pack' }],
  ['a longer name', { set_name: `${pack}_Extra` }],
  ['a missing name', {}], ['a non-string name', { set_name: 123 }],
  ['a non-ASCII name', { set_name: 'Ｃampaign_Pack_by_ExampleBot' }],
  ['malformed optional metadata', []],
]) {
  test(`user: Given a blocked pack and a sticker with ${label}, When delivered, Then whole-name matching leaves it unchanged despite identical file identifiers`, async () => {
    await registerPack();
    const current = sticker();
    current.sticker = Array.isArray(metadata) ? metadata : { file_id: 'file-81', file_unique_id: 'unique-81', ...metadata };
    assert.equal((await deliver({ message: current })).status, 200);
    assert.equal(telegram.has(81), true);
    assert.equal(telegram.canSend(22), true);
  });
}

for (const [label, configure, automaticDelete] of [
  ['administrator', () => { telegram.members.set(22, { status: 'administrator' }); }, false],
  ['owner', () => { telegram.members.set(22, { status: 'creator' }); }, false],
  ['anonymous destination group', current => { current.sender_chat = { ...chat }; }, false],
  ['bot sender', current => { current.from.is_bot = true; }, true],
  ['ordinary group', current => { current.chat.type = 'group'; }, true],
]) {
  test(`user: Given a listed pack sent by an ${label}, When automatically matched, Then shared protection allows ${automaticDelete ? 'deletion only' : 'neither deletion nor restriction'}`, async () => {
    await registerPack();
    const current = sticker();
    configure(current);
    const member = structuredClone(telegram.members.get(22));
    assert.equal((await deliver({ message: current })).status, 200);
    assert.equal(telegram.has(81), !automaticDelete);
    assert.deepEqual(telegram.members.get(22), member);
    assert.equal((await database.prepare('SELECT COUNT(*) AS n FROM blacklisted_users').first()).n, 0);
  });
}

for (const [label, configure] of [
  ['administrator', () => { telegram.members.set(22, { status: 'administrator' }); }],
  ['owner', () => { telegram.members.set(22, { status: 'creator' }); }],
  ['sender-chat', target => { target.sender_chat = { ...chat }; }],
  ['ordinary group', target => { target.chat.type = 'group'; }],
]) {
  test(`user: Given an ${label} sticker target, When an authorized bare bs reports it, Then the pack is saved and explicit target deleted without banning or clearing history`, async () => {
    await history();
    const target = sticker();
    configure(target);
    const update = command('/bs', target);
    update.message.chat = { ...target.chat };
    const member = structuredClone(telegram.members.get(22));
    telegram.send(target);
    assert.equal((await deliver(update)).status, 200);
    assert.equal(telegram.has(81), false);
    assert.equal(telegram.has(70), true);
    assert.deepEqual(telegram.members.get(22), member);
    assert.deepEqual((await savedPacks()).results, [{ bot_id: 123, set_name: normalizedPack }]);
  });
}
for (const table of ['reports', 'blacklisted_sticker_sets', 'blacklisted_sources']) {
  test(`user: Given unavailable ${table} storage, When a sticker with an inline source is reported, Then punishment waits until all registrations succeed on retry`, async () => {
    telegram.members.set(777, { status: 'member' });
    await history();
    const target = { ...sticker(), via_bot: { id: 777, is_bot: true } };
    const update = command('/bs', target);
    telegram.send(target);
    await unavailable(table, async () => {
      assert.equal((await deliver(update)).status, 503);
      for (const id of [22, 777]) assert.equal(telegram.canJoin(id), true);
      for (const id of [70, 81, 82]) assert.equal(telegram.has(id), true);
      assert.deepEqual(telegram.replies, []);
    });
    assert.equal((await dispatch(update)).status, 200);
    for (const id of [22, 777]) assert.equal(telegram.canJoin(id), false);
    for (const id of [70, 81, 82]) assert.equal(telegram.has(id), false);
    assert.deepEqual((await savedPacks()).results, [{ bot_id: 123, set_name: normalizedPack }]);
    assert.equal((await database.prepare('SELECT source_id FROM blacklisted_sources WHERE source_id=777').first()).source_id, 777);
  });
}

test('user: Given unavailable pack lookup storage, When a sticker arrives, Then the webhook requests retry and recovery filters it', async () => {
  await registerPack();
  const update = { update_id: 81, message: sticker() };
  await unavailable('blacklisted_sticker_sets', async () => {
    assert.equal((await deliver(update)).status, 503);
    assert.equal(telegram.has(81), true);
    assert.equal(telegram.canSend(22), true);
  });
  assert.equal((await dispatch(update)).status, 200);
  assert.equal(telegram.has(81), false);
  assert.equal(telegram.canSend(22), false);
});

for (const method of ['sendMessage', 'deleteMessage:82']) {
  test(`user: Given a sticker report with a temporary ${method} failure, When delivery resumes after manual unban, Then the command completes without another ban`, async () => {
    await history();
    const target = sticker();
    const update = command('/bs', target);
    telegram.send(target);
    telegram.faults.set(method, retryable);
    assert.equal((await deliver(update)).status, 503);
    assert.equal(telegram.canJoin(22), false);
    assert.equal(telegram.has(81), false);
    assert.equal(telegram.has(82), true);
    telegram.members.set(22, { status: 'member' });
    telegram.faults.clear();
    assert.equal((await dispatch(update)).status, 200);
    assert.equal(telegram.canJoin(22), true);
    assert.equal(telegram.has(82), false);
    assert.match(telegram.replies.at(-1).text, new RegExp(normalizedPack));
  });
}

test('user: Given an uncertain sticker sender ban, When the same report resumes, Then the command remains for inspection and no new ban occurs', async () => {
  const target = sticker();
  const update = command('/bs', target);
  telegram.send(target);
  telegram.faults.set('banChatMember:22', () => Response.json({ ok: false, error_code: 500 }, { status: 500 }));
  assert.equal((await deliver(update)).status, 503);
  telegram.faults.clear();
  assert.equal((await dispatch(update)).status, 200);
  assert.equal(telegram.canJoin(22), true);
  assert.equal(telegram.has(82), true);
  assert.match(telegram.replies.at(-1).text, /check membership/i);
  assert.deepEqual((await savedPacks()).results, [{ bot_id: 123, set_name: normalizedPack }]);
});

test('user: Given a pending sticker report, When reporter authorization is revoked before retry, Then the sender and command remain untouched', async () => {
  const target = sticker();
  const update = command('/bs', target);
  telegram.send(target);
  telegram.faults.set('banChatMember:22', retryable);
  assert.equal((await deliver(update)).status, 503);
  telegram.faults.clear();
  await setBindings({ REPORTER_IDS: '' });
  assert.equal((await dispatch(update)).status, 200);
  assert.equal(telegram.canJoin(22), true);
  for (const id of [81, 82]) assert.equal(telegram.has(id), true);
  assert.deepEqual(telegram.replies, []);
});
