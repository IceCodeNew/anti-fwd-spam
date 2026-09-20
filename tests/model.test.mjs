import assert from 'node:assert/strict';
import { test } from 'node:test';
import { dispatch, model, telegram, database } from './worker-runtime.mjs';
import { message, report } from './telegram-fake.mjs';

model.enabled = true;

test('user: Given a spam probability at the threshold, When a new message arrives, Then that message disappears and its sender is permanently muted with history intact', async () => {
  const target = message();
  target.from.first_name = 'Alice';
  target.from.last_name = 'Example';
  target.text = 'Contact me for paid promotions';
  model.profile = { bio: 'Advertising service' };
  model.probability = 0.95;
  telegram.send(message(80));
  telegram.send(target);
  assert.equal((await dispatch({ update_id: 1, message: target })).status, 200);
  assert.equal(telegram.has(81), false);
  assert.equal(telegram.has(80), true);
  assert.equal(telegram.canSend(22), false);
  assert.equal(telegram.members.get(22).until_date, 0);
  assert.equal(telegram.canJoin(22), true);
  assert.equal((await database.prepare('SELECT COUNT(*) AS n FROM blacklisted_users').first()).n, 0);
  assert.equal(model.state.nickname, 'Alice Example');
  assert.equal(model.state.bio, 'Advertising service');
  assert.equal(model.state.message.text, target.text);
});

for (const status of ['creator', 'administrator', 'kicked']) {
  test(`user: Given a ${status} sender, When Jev flags their message, Then administrator messages and existing bans stay protected`, async () => {
    telegram.members.set(22, { status });
    model.probability = 1;
    telegram.send(message());
    assert.equal((await dispatch({ update_id: 1, message: message() })).status, 200);
    assert.equal(telegram.has(81), status !== 'kicked');
    assert.equal(telegram.members.get(22).status, status);
    assert.equal((await database.prepare('SELECT COUNT(*) AS n FROM blacklisted_users').first()).n, 0);
  });
}

test('user: Given a probability below the threshold or an invalid answer, When classified, Then the message remains', async () => {
  let id = 81;
  for (const probability of [0.9499, null, '0.99', true, -1, 1.01]) {
    model.probability = probability;
    const target = message(id++);
    telegram.send(target);
    assert.equal((await dispatch({ update_id: id, message: target })).status, 200);
    assert.equal(telegram.has(target.message_id), true);
  }
});

test('user: Given an unavailable model, When a message arrives, Then it remains without a webhook retry', async () => {
  model.response = new Response('unavailable', { status: 503 });
  telegram.send(message());
  assert.equal((await dispatch({ update_id: 3, message: message() })).status, 200);
  assert.equal(telegram.has(81), true);
});

test('user: Given a media caption and unavailable biography, When classified, Then nickname and caption remain usable', async () => {
  const target = message();
  delete target.text;
  target.caption = 'Paid promotion';
  target.photo = [{ file_id: 'private-file', file_unique_id: 'private-id', width: 100, height: 100 }];
  target.reply_to_message = message(79, 11);
  model.profile = { id: 99 };
  model.probability = 0.99;
  telegram.send(target);
  assert.equal((await dispatch({ update_id: 4, message: target })).status, 200);
  assert.equal(telegram.has(81), false);
  assert.equal(model.state.bio, null);
  assert.equal(model.state.message.caption, 'Paid promotion');
  assert.equal(model.state.message.reply_to_message, undefined);
  assert.equal(JSON.stringify(model.state).includes('private-file'), false);
});

test('user: Given an ordinary report or an edit, When the model would flag spam, Then existing behavior is preserved', async () => {
  model.probability = 1;
  const update = report();
  update.message.from = message(82, 22).from;
  telegram.send(update.message);
  telegram.send(update.message.reply_to_message);
  assert.equal((await dispatch(update)).status, 200);
  assert.equal(telegram.has(81), true);
  assert.equal(telegram.has(82), true);
  assert.equal((await dispatch({ update_id: 5, edited_message: message() })).status, 200);
  assert.equal(telegram.has(81), true);
});

test('user: Given a malformed model envelope, When a message is evaluated, Then it remains visible', async () => {
  let id = 81;
  for (const body of ['not JSON', '[]', '{}', '{"answers":[]}', '{"answers":{"spam":{"type":"text","noul":1}}}',
    '{"answers":{"spam":{"type":"noul","noul":NaN}}}', '{"answers":{"spam":{"type":"noul","noul":Infinity}}}',
    ' '.repeat(65537)]) {
    model.response = new Response(body);
    const target = message(id++);
    telegram.send(target);
    assert.equal((await dispatch({ update_id: id, message: target })).status, 200);
    assert.equal(telegram.has(target.message_id), true);
  }
});

test('user: Given a permanent deletion rejection, When spam is detected, Then redelivery does not delete it later', async () => {
  model.probability = 1;
  telegram.faults.set('deleteMessage', () => Response.json({ ok: false, error_code: 400 }, { status: 400 }));
  telegram.send(message());
  assert.equal((await dispatch({ update_id: 7, message: message() })).status, 200);
  telegram.faults.clear();
  assert.equal((await dispatch({ update_id: 7, message: message() })).status, 200);
  assert.equal(telegram.has(81), true);
});

test('user: Given service events or non-user senders, When a new update arrives, Then the model cannot remove it', async () => {
  model.probability = 1;
  const service = message();
  delete service.text;
  service.new_chat_members = [message().from];
  const channel = { ...message(), sender_chat: { id: -10099, type: 'channel' } };
  const bot = { ...message(), from: { ...message().from, is_bot: true } };
  for (const target of [service, channel, bot]) {
    telegram.send(target);
    assert.equal((await dispatch({ update_id: 8, message: target })).status, 200);
    assert.equal(telegram.has(81), true);
  }
});

test('user: Given a failed or empty biography lookup, When the message is spam, Then it is still evaluated with explicit missing information', async () => {
  model.probability = 0.96;
  let id = 81;
  for (const [profile, expected] of [
    [new Response('{"ok":false,"error_code":400}', { status: 400 }), null],
    [{}, ''],
  ]) {
    model.profile = profile;
    const target = message(id++);
    telegram.send(target);
    assert.equal((await dispatch({ update_id: id, message: target })).status, 200);
    assert.equal(telegram.has(target.message_id), false);
    assert.equal(model.state.bio, expected);
  }
});

test('user: Given structured text, When spam is detected, Then its text is evaluated without embedded media identifiers', async () => {
  let id = 81;
  for (const field of ['rich_message', 'checklist', 'poll']) {
    const target = message(id++);
    delete target.text;
    target[field] = field === 'rich_message'
      ? { blocks: [{ type: 'paragraph', text: { type: 'plain', text: 'Advertising offer' } },
        { type: 'photo', photo: [{ file_id: 'private-media' }] }] }
      : field === 'checklist'
        ? { title: 'Offers', tasks: [{ id: 1, text: 'Advertising offer', completed_by_user: { id: 11 } }] }
        : { question: 'Offers?', options: [{ text: 'Advertising offer', media: { photo: [{ file_id: 'private-media' }] } }] };
    model.probability = 1;
    telegram.send(target);
    assert.equal((await dispatch({ update_id: id, message: target })).status, 200);
    assert.equal(telegram.has(target.message_id), false);
    assert.ok(JSON.stringify(model.state.message).includes('Advertising offer'));
    assert.equal(JSON.stringify(model.state).includes('private-media'), false);
    assert.equal(JSON.stringify(model.state).includes('completed_by_user'), false);
  }
});

test('user: Given an oversized answer containing a spam score, When evaluated, Then the message remains', async () => {
  model.response = new Response('{"answers":{"spam":{"type":"noul","noul":1}}}'.padEnd(65537));
  telegram.send(message());
  assert.equal((await dispatch({ update_id: 11, message: message() })).status, 200);
  assert.equal(telegram.has(81), true);
});

test('user: Given a valid chunked answer exactly at the size limit, When it flags spam, Then the message is deleted', async () => {
  const bytes = new TextEncoder().encode('{"answers":{"spam":{"type":"noul","noul":0.95}}}'.padEnd(65536));
  model.response = new Response(new ReadableStream({
    start(controller) {
      controller.enqueue(bytes.slice(0, 32768));
      controller.enqueue(bytes.slice(32768));
      controller.close();
    },
  }));
  telegram.send(message());
  assert.equal((await dispatch({ update_id: 12, message: message() })).status, 200);
  assert.equal(telegram.has(81), false);
});
