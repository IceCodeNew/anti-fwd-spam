import assert from 'node:assert/strict';
import { test } from 'node:test';
import { dispatch, model, telegram, database, setBindings } from './worker-runtime.mjs';
import { message } from './telegram-fake.mjs';

model.enabled = true;

const campaign = '@safdhifobot campaign_001 g1789957985962f8909ecd';
const flood = ('💰'.repeat(23) + '\n').repeat(8) + '💰'.repeat(8);

for (const text of [campaign, flood, '@example_bot campaign_2', '💰'.repeat(4), '🔴 '.repeat(4),
  '🔴说明：💰 \n💰\t💰 💰结束🔴', '💰说明：🔴🔴🔴🔴结束💰',
  `请警惕这种垃圾消息：${campaign}，不要点击`, '@example_bot campaign_1 this is a discussion',
  '@example_bot campaign_1a', '有收款码的来，每天赚 5000，私聊']) {
  for (const field of ['text', 'caption']) {
    test(`user: Given ${field} containing ${text === flood ? 'the screenshot flood' : JSON.stringify(text)}, When Jev would accept it, Then only that message is deleted and its sender muted`, async () => {
      const previous = message(80);
      telegram.send(previous);
      await dispatch({ update_id: 1, message: previous });
      model.state = null;
      const target = message();
      delete target.text;
      target[field] = text;
      telegram.send(target);
      assert.equal((await dispatch({ update_id: 2, message: target })).status, 200);
      assert.equal(telegram.has(81), false);
      assert.equal(telegram.has(80), true);
      assert.equal(telegram.canSend(22), false);
      assert.equal(telegram.members.get(22).until_date, 0);
      assert.equal(telegram.canJoin(22), true);
      assert.equal((await database.prepare('SELECT COUNT(*) AS n FROM blacklisted_users').first()).n, 0);
      assert.deepEqual((await database.prepare('SELECT source_id FROM blacklisted_sources').all()).results,
        [{ source_id: 273234066 }]);
      assert.equal(model.state, null);
    });
  }
}

for (const [name, contact] of [
  ['the screenshot contact card', { first_name: '有收款码一天赚一万', phone_number: '6285198277256', user_id: 8247255987 }],
  ['a contact name split across first and last name', { first_name: '有收款码', last_name: '日入3000+', phone_number: '6285198277256' }],
]) {
  test(`user: Given ${name}, When Jev would accept it, Then only that message is deleted and its sender muted`, async () => {
    const target = message();
    delete target.text;
    target.contact = contact;
    telegram.send(target);
    assert.equal((await dispatch({ update_id: 1, message: target })).status, 200);
    assert.equal(telegram.has(81), false);
    assert.equal(telegram.canSend(22), false);
    assert.equal(telegram.members.get(22).until_date, 0);
  });
}

test('user: Given an ordinary contact card, When Jev accepts it, Then it stays visible and Jev receives the name without the phone number', async () => {
  const target = message();
  delete target.text;
  target.contact = { first_name: '王小明', phone_number: '8613800000000' };
  telegram.send(target);
  assert.equal((await dispatch({ update_id: 1, message: target })).status, 200);
  assert.equal(telegram.has(81), true);
  assert.equal(telegram.canSend(22), true);
  assert.deepEqual(model.state.message.contact, { first_name: '王小明' });
});

test('user: Given no model key, When a screenshot pattern arrives, Then local filtering still deletes it and mutes its sender', async () => {
  await setBindings({ EXPERIENTIAL_API_KEY: undefined });
  telegram.send({ ...message(), text: campaign });
  assert.equal((await dispatch({ update_id: 1, message: { ...message(), text: campaign } })).status, 200);
  assert.equal(telegram.has(81), false);
  assert.equal(telegram.canSend(22), false);
});

test('user: Given a bot sending the screenshot flood, When received, Then its current message disappears with earlier messages intact', async () => {
  const target = { ...message(), from: { ...message().from, is_bot: true }, text: flood };
  telegram.send(message(80));
  telegram.send(target);
  assert.equal((await dispatch({ update_id: 1, message: target })).status, 200);
  assert.equal(telegram.has(81), false);
  assert.equal(telegram.has(80), true);
  assert.equal(telegram.canJoin(22), true);
  assert.equal(telegram.canSend(22), true);
});

for (const status of ['administrator', 'creator', 'kicked']) {
  test(`user: Given a ${status} sender, When a local pattern matches, Then administrator protection and existing bans remain`, async () => {
    telegram.members.set(22, { status });
    const target = { ...message(), text: campaign };
    telegram.send(target);
    assert.equal((await dispatch({ update_id: 1, message: target })).status, 200);
    assert.equal(telegram.has(81), status !== 'kicked');
    assert.equal(telegram.members.get(22).status, status);
  });
}

test('user: Given an anonymous group administrator, When a local pattern matches, Then their message remains', async () => {
  const target = { ...message(), text: flood, sender_chat: message().chat };
  telegram.send(target);
  assert.equal((await dispatch({ update_id: 1, message: target })).status, 200);
  assert.equal(telegram.has(81), true);
  assert.equal(telegram.canSend(22), true);
});

test('user: Given ordinary discussion and short emoji runs, When Jev accepts them, Then local filtering leaves them visible', async () => {
  const examples = [
    '讨论公司福利', '讨论强奸案件', '请举报偷拍和禁忌资源', '网黄是什么？',
    `示例：${'💰'.repeat(3)}`, '@example_bot campaign_update',
    '💰'.repeat(3), '🔴 '.repeat(3),
    '💰'.repeat(2) + '🔴'.repeat(2), '💰💰💰文字💰', '🔴🔴💰🔴🔴', '普通消息',
    '收款码在哪里设置？', '收款码今天收了一万块的货款', '收款码今天收入1000多', '收款码今天赚了100块',
  ];
  for (const [index, text] of examples.entries()) {
    const target = { ...message(81 + index), text };
    telegram.send(target);
    assert.equal((await dispatch({ update_id: index + 1, message: target })).status, 200);
    assert.equal(telegram.has(target.message_id), true);
    assert.equal(telegram.canSend(22), true);
    assert.equal(model.state.message.text, text);
  }
});

test('user: Given spam outside local patterns, When Jev flags it, Then model moderation still deletes and mutes', async () => {
  model.probability = 1;
  const target = { ...message(), text: '福利推广，联系我购买' };
  telegram.send(target);
  assert.equal((await dispatch({ update_id: 1, message: target })).status, 200);
  assert.equal(telegram.has(81), false);
  assert.equal(telegram.canSend(22), false);
});

test('user: Given a reply quoting the screenshot flood, When its own text is ordinary, Then the reply and sender remain unaffected', async () => {
  const target = { ...message(), reply_to_message: { ...message(80), text: flood } };
  telegram.send(target);
  assert.equal((await dispatch({ update_id: 1, message: target })).status, 200);
  assert.equal(telegram.has(81), true);
  assert.equal(telegram.canSend(22), true);
});

test('user: Given a group message, When it is edited to match a local pattern, Then the edited message is deleted and its sender muted', async () => {
  model.probability = 1;
  const ordinary = message(80);
  telegram.send(ordinary);
  assert.equal((await dispatch({ update_id: 1, edited_message: ordinary })).status, 200);
  assert.equal(telegram.has(80), true);
  assert.equal(model.state, null);
  const target = { ...message(), text: campaign };
  telegram.send(target);
  assert.equal((await dispatch({ update_id: 2, edited_message: target })).status, 200);
  assert.equal(telegram.has(81), false);
  assert.equal(telegram.has(80), true);
  assert.equal(telegram.canSend(22), false);
  assert.equal(telegram.canJoin(22), true);
});

test('user: Given a private message, When its text matches a local pattern, Then group filtering leaves it alone', async () => {
  const target = { ...message(), text: campaign };
  const privateMessage = { ...target, chat: { id: 22, type: 'private' } };
  telegram.send(privateMessage);
  assert.equal((await dispatch({ update_id: 2, message: privateMessage })).status, 200);
  assert.equal(telegram.has(81, 22), true);
});

test('user: Given a forbidden deletion whose description mentions a missing message, When a local pattern matches, Then the message stays and its sender is not muted', async () => {
  const target = { ...message(), text: campaign };
  telegram.send(target);
  telegram.faults.set('deleteMessage', () => Response.json(
    { ok: false, error_code: 403, description: 'Forbidden: message to delete not found' }, { status: 403 }));
  assert.equal((await dispatch({ update_id: 1, message: target })).status, 200);
  assert.equal(telegram.has(81), true);
  assert.equal(telegram.canSend(22), true);
});

test('user: Given a temporarily rejected deletion, When Telegram redelivers the matching message, Then deletion and permanent mute finish', async () => {
  const target = { ...message(), text: campaign };
  telegram.send(target);
  telegram.faults.set('deleteMessage', () => Response.json({ ok: false, error_code: 429 }, { status: 429 }));
  assert.equal((await dispatch({ update_id: 1, message: target })).status, 503);
  assert.equal(telegram.has(81), true);
  assert.equal(telegram.canSend(22), true);
  telegram.faults.clear();
  assert.equal((await dispatch({ update_id: 1, message: target })).status, 200);
  assert.equal(telegram.has(81), false);
  assert.equal(telegram.canSend(22), false);
});

test('user: Given successful pattern deletion but rate-limited muting, When Telegram redelivers after recovery, Then the absent message stays absent and its sender is muted', async () => {
  const target = { ...message(), text: 'prefix 💰💰💰💰 suffix' };
  telegram.send(target);
  telegram.faults.set('restrictChatMember', () => Response.json({ ok: false, error_code: 429 }, { status: 429 }));
  assert.equal((await dispatch({ update_id: 1, message: target })).status, 503);
  assert.equal(telegram.has(81), false);
  assert.equal(telegram.canSend(22), true);
  telegram.faults.clear();
  assert.equal((await dispatch({ update_id: 1, message: target })).status, 200);
  assert.equal(telegram.has(81), false);
  assert.equal(telegram.canSend(22), false);
  assert.equal(telegram.members.get(22).until_date, 0);
  assert.equal(telegram.canJoin(22), true);
});

test('user: Given a manual unmute after a pattern match, When Telegram redelivers that update, Then the manual unmute remains', async () => {
  const target = { ...message(), text: campaign };
  telegram.send(target);
  assert.equal((await dispatch({ update_id: 1, message: target })).status, 200);
  assert.equal(telegram.canSend(22), false);
  telegram.members.set(22, { status: 'member' });
  assert.equal((await dispatch({ update_id: 1, message: target })).status, 200);
  assert.equal(telegram.has(81), false);
  assert.equal(telegram.canSend(22), true);
});
