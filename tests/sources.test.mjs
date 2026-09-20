import assert from 'node:assert/strict';
import { test } from 'node:test';
import { database, dispatch, model, setBindings, telegram } from './worker-runtime.mjs';
import { chat, message } from './telegram-fake.mjs';

function command(text = '/ban example_bot', sender = 11) {
  return { update_id: 300, message: { ...message(300, sender), text,
    entities: [{ type: 'bot_command', offset: 0, length: text.split(' ')[0].length }] } };
}

test('user: Given an empty source table and an obsolete environment value, When an inline message arrives, Then the removed source is not blocked', async () => {
  await setBindings({ BLACKLIST_BOT_IDS: '273234066' });
  await database.prepare('DELETE FROM blacklisted_sources').run();
  const update = { message: { ...message(), via_bot: { id: 273234066, is_bot: true } } };
  telegram.send(update.message);
  assert.equal((await dispatch(update)).status, 200);
  assert.equal(telegram.has(81), true);
  assert.equal(telegram.canSend(22), true);
});

test('user: Given an authorized reporter, When a username is banned twice, Then its account stays banned and indexed history is cleared while later inline senders are only muted', async () => {
  telegram.accounts.set('@example_bot', { id: 777, type: 'private', username: 'example_bot' });
  telegram.members.set(777, { status: 'member' });
  const history = message(200, 777);
  telegram.send(history);
  await dispatch({ update_id: 299, message: history });
  const update = command();
  assert.equal((await dispatch(update)).status, 200);
  assert.equal(telegram.canJoin(777), false);
  assert.equal(telegram.has(200), false);
  assert.match(telegram.replies.at(-1).text, /777/);
  assert.equal(telegram.replies.at(-1).reply_to_message.message_id, 300);
  assert.equal((await dispatch(update)).status, 200);
  assert.equal((await dispatch({ ...update, update_id: 301 })).status, 200);
  assert.equal(telegram.canJoin(777), false);
  assert.equal((await database.prepare('SELECT count(*) AS n FROM blacklisted_sources WHERE source_id=777').first()).n, 1);
  const spam = { ...message(301), via_bot: { id: 777, is_bot: true } };
  telegram.send(message(299));
  telegram.send(spam);
  assert.equal((await dispatch({ message: spam })).status, 200);
  assert.equal(telegram.has(301), false);
  assert.equal(telegram.has(299), true);
  assert.equal(telegram.canSend(22), false);
  assert.equal(telegram.canJoin(22), true);
  assert.deepEqual((await database.prepare('SELECT user_id FROM blacklisted_users').all()).results, [{ user_id: 777 }]);
});

test('user: Given a listed private-chat reporter, When an ordinary username is added, Then its numeric ID is accepted without restricting that account directly', async () => {
  telegram.accounts.set('@ordinary', { id: 22, type: 'private', username: 'ordinary' });
  const update = command('/ban @ordinary');
  update.message.chat = { id: 11, type: 'private' };
  assert.equal((await dispatch(update)).status, 200);
  assert.match(telegram.replies.at(-1).text, /22/);
  assert.equal(telegram.replies.at(-1).chat.id, 11);
  assert.equal((await database.prepare('SELECT source_id FROM blacklisted_sources WHERE source_id=22').first()).source_id, 22);
  telegram.send(message());
  await dispatch({ message: message() });
  assert.equal(telegram.has(81), true);
  assert.equal(telegram.canSend(22), true);
});

test('user: Given an unlisted administrator or a forged anonymous compatibility user, When ban is requested, Then no source is added or model invoked', async () => {
  telegram.accounts.set('@example_bot', { id: 777, type: 'private', username: 'example_bot' });
  for (const update of [command('/ban example_bot', 22),
    { ...command(), message: { ...command().message, sender_chat: { ...chat, id: -10099 } } }]) {
    assert.equal((await dispatch(update)).status, 200);
  }
  assert.equal(await database.prepare('SELECT source_id FROM blacklisted_sources WHERE source_id=777').first(), null);
  assert.deepEqual(telegram.replies, []);
  assert.equal(model.state, null);
});

test('user: Given an authorized sender chat, When ban addresses this bot, Then the source is saved regardless of destination group', async () => {
  telegram.accounts.set('@example_bot', { id: 778, type: 'private', username: 'example_bot' });
  telegram.groups.set(-10099, new Map([[778, { status: 'kicked' }]]));
  const update = command('/ban@test_gate_bot example_bot');
  update.message.sender_chat = chat;
  update.message.chat = { ...chat, id: -10099 };
  assert.equal((await dispatch(update)).status, 200);
  assert.match(telegram.replies.at(-1).text, /778/);
  assert.equal(telegram.replies.at(-1).chat.id, -10099);
});

test('user: Given missing or malformed usernames and unresolved accounts, When ban is requested, Then a useful reply is returned without adding a source', async () => {
  for (const text of ['/ban', '/ban two names', '/ban https://example.com', '/ban missing']) {
    assert.equal((await dispatch(command(text))).status, 200);
    assert.ok(telegram.replies.at(-1).text.length > 0);
    assert.doesNotMatch(telegram.replies.at(-1).text, /Source added/);
  }
});

test('user: Given unavailable source storage, When ban is retried after recovery, Then success is announced only after the ID is durably saved', async () => {
  telegram.accounts.set('@example_bot', { id: 779, type: 'private', username: 'example_bot' });
  telegram.members.set(779, { status: 'member' });
  await database.prepare('ALTER TABLE blacklisted_sources RENAME TO unavailable_sources').run();
  try {
    assert.equal((await dispatch(command())).status, 503);
    assert.deepEqual(telegram.replies, []);
  } finally {
    await database.prepare('ALTER TABLE unavailable_sources RENAME TO blacklisted_sources').run();
  }
  assert.equal((await dispatch(command())).status, 200);
  assert.match(telegram.replies.at(-1).text, /779/);
});

test('user: Given edited commands or commands for another bot, When delivered, Then no source is added', async () => {
  telegram.accounts.set('@example_bot', { id: 780, type: 'private', username: 'example_bot' });
  assert.equal((await dispatch({ edited_message: command().message })).status, 200);
  assert.equal((await dispatch(command('/ban@other_bot example_bot'))).status, 200);
  assert.equal(await database.prepare('SELECT source_id FROM blacklisted_sources WHERE source_id=780').first(), null);
  assert.deepEqual(telegram.replies, []);
});

test('user: Given a previously banned account, When ban runs again, Then the account cannot rejoin and old indexed messages are still removed', async () => {
  telegram.accounts.set('@example_bot', { id: 22, type: 'private', username: 'example_bot' });
  telegram.send(message(200));
  await dispatch({ update_id: 199, message: message(200) });
  telegram.members.set(22, { status: 'kicked', until_date: 0 });
  assert.equal((await dispatch(command())).status, 200);
  assert.equal(telegram.canJoin(22), false);
  assert.equal(telegram.canSend(22), false);
  assert.equal(telegram.has(200), false);
});

for (const status of ['creator', 'administrator']) {
  test(`user: Given a target with ${status} privileges, When an authorized reporter bans it, Then membership and indexed history remain protected`, async () => {
    telegram.accounts.set('@example_bot', { id: 22, type: 'private', username: 'example_bot' });
    telegram.send(message(200));
    await dispatch({ update_id: 199, message: message(200) });
    telegram.members.set(22, { status });
    assert.equal((await dispatch(command())).status, 200);
    assert.equal(telegram.canJoin(22), true);
    assert.equal(telegram.canSend(22), true);
    assert.equal(telegram.has(200), true);
    assert.match(telegram.replies.at(-1).text, /protected administrator/);
    assert.equal((await database.prepare('SELECT count(*) AS n FROM blacklisted_users').first()).n, 0);
  });
}

test('user: Given a saved command and a renamed account during retry, When cleanup resumes, Then only the original account is banned and cleared', async () => {
  telegram.accounts.set('@example_bot', { id: 22, type: 'private', username: 'example_bot' });
  telegram.send(message(200));
  await dispatch({ update_id: 199, message: message(200) });
  telegram.faults.set('deleteMessages', () => Response.json({ ok: false, error_code: 429 }, { status: 429 }));
  assert.equal((await dispatch(command())).status, 503);
  telegram.accounts.set('@example_bot', { id: 33, type: 'private', username: 'example_bot' });
  telegram.members.set(33, { status: 'member' });
  telegram.faults.clear();
  assert.equal((await dispatch(command())).status, 200);
  assert.equal(telegram.canJoin(22), false);
  assert.equal(telegram.canJoin(33), true);
  assert.equal(telegram.has(200), false);
  assert.match(telegram.replies.at(-1).text, /ID: 22/);
  assert.equal(await database.prepare('SELECT source_id FROM blacklisted_sources WHERE source_id=33').first(), null);
});

test('user: Given a malformed or mismatched lookup response, When a username is banned, Then no unrelated account is recorded or punished', async () => {
  for (const result of [{ id: 22 }, { id: 22, type: 'private', username: 'someone_else' },
    { id: 22, type: 'unexpected', username: 'example_bot' }, { id: true, type: 'private', username: 'example_bot' }]) {
    telegram.faults.set('getChat', () => Response.json({ ok: true, result }));
    assert.equal((await dispatch(command())).status, 503);
    assert.equal(telegram.canJoin(22), true);
    assert.equal(telegram.canSend(22), true);
    assert.equal(await database.prepare('SELECT source_id FROM blacklisted_sources WHERE source_id=22').first(), null);
    assert.equal((await database.prepare('SELECT count(*) AS n FROM reports').first()).n, 0);
  }
});

test('user: Given an active username alias, When an authorized private-chat command resolves it, Then the account ID is saved', async () => {
  telegram.accounts.set('@alias', { id: 781, type: 'private', username: 'primary', active_usernames: ['primary', 'Alias'] });
  const update = command('/ban alias');
  update.message.chat = { id: 11, type: 'private' };
  assert.equal((await dispatch(update)).status, 200);
  assert.match(telegram.replies.at(-1).text, /781/);
});
