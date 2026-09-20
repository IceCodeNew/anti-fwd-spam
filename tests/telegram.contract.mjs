import assert from 'node:assert/strict';
import { test } from 'node:test';
import { chat, message, Telegram, token } from './telegram-fake.mjs';

// Documentation compatibility, not a live moderation test. No real bot token is used.
test('user checks API compatibility: Given live Bot API documentation, When requests run against the fake, Then parameters and membership outcomes match the documented contract', async () => {
  const response = await fetch('https://core.telegram.org/bots/api', { signal: AbortSignal.timeout(20_000) });
  assert.equal(response.status, 200);
  const html = await response.text();
  const text = value => value.replace(/<[^>]+>/g, ' ').replace(/\s+/g, ' ').trim();
  function section(name) {
    const start = html.indexOf(`name="${name.toLowerCase()}"`);
    assert.ok(start >= 0, `Missing Telegram contract for ${name}`);
    return html.slice(start, html.indexOf('<h4>', start));
  }
  const telegram = new Telegram();
  telegram.reset();
  async function call(method, params) {
    const contract = section(method);
    const fields = new Map([...contract.matchAll(/<tr>\s*<td>(.*?)<\/td>\s*<td>(.*?)<\/td>\s*<td>(.*?)<\/td>/gs)]
      .map(([, field, type, required]) => [text(field), { type: text(type), required: text(required) === 'Yes' }]));
    assert.ok(fields.size > 0, `No parameters parsed for ${method}`);
    for (const [field, spec] of fields) if (spec.required) assert.ok(field in params, `${method}.${field} is required`);
    for (const [field, value] of Object.entries(params)) {
      const spec = fields.get(field);
      assert.ok(spec, `${method}.${field} is not in the Bot API`);
      const valid = spec.type === 'Array of Integer' ? Array.isArray(value) && value.every(Number.isSafeInteger)
        : spec.type.includes('Integer') ? Number.isSafeInteger(value) || (spec.type.includes('String') && typeof value === 'string')
        : spec.type === 'String' ? typeof value === 'string'
        : spec.type === 'Boolean' ? typeof value === 'boolean' : typeof value === 'object';
      assert.ok(valid, `${method}.${field} expects ${spec.type}`);
    }
    return (await telegram.fetch(new Request(`https://api.telegram.org/bot${token}/${method}`, {
      method: 'POST', headers: { 'content-type': 'application/json' }, body: JSON.stringify(params),
    }))).json();
  }
  assert.match(text(section('banChatMember')), /user will not be able to return to the chat/);
  assert.match(text(section('banChatMember')), /Always True for supergroups and channels/);
  assert.match(text(section('banChatMember')), /delete all messages from the chat for the user/);
  assert.match(text(section('restrictChatMember')), /restrict a user in a supergroup/);
  assert.match(text(section('ChatMemberBanned')), /If 0, then the user is banned forever/);
  assert.match(text(section('ChatMemberRestricted')), /If 0, then the user is restricted forever/);
  assert.match(text(section('getChatMember')), /Returns a ChatMember object/);
  telegram.accounts.set('@botfather', { id: 93372553, username: 'BotFather', type: 'private' });
  assert.equal((await call('getChat', { chat_id: '@BotFather' })).result.id, 93372553);
  const reply = await call('sendMessage', { chat_id: chat.id, text: 'Source ID: 93372553',
    reply_parameters: { message_id: 81, allow_sending_without_reply: true } });
  assert.equal(reply.result.text, 'Source ID: 93372553');
  assert.equal(reply.result.reply_to_message.message_id, 81);
  // Discussion commenters can have Left status without joining the group.
  telegram.members.set(25, { status: 'left' });
  assert.deepEqual(await call('banChatMember', {
    chat_id: chat.id, user_id: 25, until_date: 0,
  }), { ok: true, result: true });
  assert.equal(telegram.canJoin(25), false);
  const target = { chat_id: chat.id, user_id: 22 };
  telegram.send(message(80));
  telegram.send(message(81));
  assert.deepEqual(await call('deleteMessage', { chat_id: chat.id, message_id: 81 }), { ok: true, result: true });
  assert.equal(telegram.has(81), false);
  assert.equal(telegram.has(80), true);
  assert.deepEqual(await call('restrictChatMember', {
    ...target, permissions: { can_send_messages: false }, until_date: 0, use_independent_chat_permissions: true,
  }), { ok: true, result: true });
  assert.equal(telegram.canSend(22), false);
  assert.equal(telegram.canJoin(22), true);
  assert.equal(telegram.has(80), true);
  assert.deepEqual(await call('banChatMember', { ...target, until_date: 0, revoke_messages: true }), { ok: true, result: true });
  assert.equal(telegram.canJoin(22), false);
  assert.equal(telegram.canSend(22), false);
  // History cleanup is intentionally not simulated; docs cannot verify its live result.
  assert.deepEqual(await call('deleteMessage', { chat_id: chat.id, message_id: 80 }), { ok: true, result: true });
  assert.equal(telegram.has(80), false);
  assert.equal((await call('getChatMember', target)).result.status, 'kicked');
  assert.match(text(section('deleteMessages')), /1-100/);
  assert.match(text(section('deleteMessages')), /See deleteMessage for limitations/);
  assert.match(text(section('deleteMessage')), /less than 48 hours ago/);
  const now = telegram.now;
  telegram.send({ ...message(90), date: now - 60 });
  assert.deepEqual(await call('deleteMessages', { chat_id: chat.id, message_ids: [90, 91] }), { ok: true, result: true });
  assert.equal(telegram.has(90), false);
  telegram.send({ ...message(92), date: now - 48 * 3600 });
  assert.equal((await call('deleteMessages', { chat_id: chat.id, message_ids: [92] })).ok, false);
  assert.equal(telegram.has(92), true);
  assert.match(text(section('deleteMessage')), /supergroup, channel, or forum topic creation can(?:'|&#39;)t be deleted/);
  for (const method of ['deleteMessage', 'deleteMessages']) {
    const params = method === 'deleteMessage' ? { message_id: 93 } : { message_ids: [93] };
    telegram.send({ ...message(93), date: now - 48 * 3600 + 1 });
    assert.equal((await call(method, { chat_id: chat.id, ...params })).ok, true);
    assert.equal(telegram.has(93), false);
    telegram.send({ ...message(93), date: now - 48 * 3600 + 1 });
    telegram.now = now + 1;
    assert.equal((await call(method, { chat_id: chat.id, ...params })).ok, false);
    assert.equal(telegram.has(93), true);
    telegram.now = now;
    for (const kind of ['forum_topic_created', 'supergroup_chat_created', 'channel_chat_created']) {
      telegram.send({ ...message(93), date: now, [kind]: true });
      assert.equal((await call(method, { chat_id: chat.id, ...params })).ok, false);
      assert.equal(telegram.has(93), true);
    }
  }
  assert.deepEqual(telegram.violations, []);
});

test('user: Given live Telegram access, When a known username is resolved, Then the fake matches the real numeric identity without sending messages', { skip: !process.env.BOT_TOKEN }, async () => {
  const response = await fetch(`https://api.telegram.org/bot${process.env.BOT_TOKEN}/getChat`, {
    method: 'POST', headers: { 'content-type': 'application/json' }, body: JSON.stringify({ chat_id: '@BotFather' }),
    signal: AbortSignal.timeout(20_000),
  });
  assert.equal(response.status, 200);
  const payload = await response.json();
  assert.equal(payload.ok, true);
  assert.equal(payload.result.id, 93372553);
  assert.equal(payload.result.username, 'BotFather');
  assert.equal(payload.result.type, 'private');
});
