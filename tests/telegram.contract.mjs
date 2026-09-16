import assert from 'node:assert/strict';
import { test } from 'node:test';
import { chat, message, Telegram, token } from './telegram-fake.mjs';

// Documentation compatibility, not a live moderation test. No real bot token is used.
test('user checks fake assumptions: Given live Bot API documentation, When documented requests run against the fake, Then the modeled outcomes match the documented expectations', async () => {
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
      const valid = spec.type.includes('Integer') ? Number.isSafeInteger(value)
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
  assert.deepEqual(await call('banChatMember', { ...target, until_date: 0, revoke_messages: false }), { ok: true, result: true });
  assert.equal(telegram.canJoin(22), false);
  assert.equal(telegram.has(80), false);
  assert.equal((await call('getChatMember', target)).result.status, 'kicked');
  assert.deepEqual(telegram.violations, []);
});
