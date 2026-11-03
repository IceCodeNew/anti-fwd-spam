import assert from 'node:assert/strict';
import { test } from 'node:test';
import { dispatch, model, telegram, database, setBindings } from './worker-runtime.mjs';
import { message } from './telegram-fake.mjs';

model.enabled = true;

const trigger = '/start 501 @example_bot @sample_bot @demo_bot';
const block = [
  '点我搜索 酒店偷拍', '看情侣实时直播', '清纯校花白虎被金主内射', '媚黑国女被狂草',
  '福利姬付费门槛免费看', '限时免费搜索', '暴力强奸', '万能检索 千万用户的选择',
].join('\n');
const resources = `点下方链接 限时免费看禁忌资源\n\n${(block + '\n\n').repeat(3)}点上方链接 限时免费看禁忌资源`;

for (const edited of [false, true]) {
  for (const text of [trigger, resources, '暴力强奸\n媚黑国女被狂草\n限时免费搜索']) {
    test(`user: Given an unregistered bot's ${text === trigger ? 'start trigger' : 'resource list'} reply, When ${edited ? 'edited' : 'delivered'}, Then local rules delete only its own message without Luna`, async () => {
      // Even a stale maturity row cannot grant a bot the human-only exemption.
      await database.prepare('INSERT INTO member_activity VALUES (123,-10012,22,10)').run();
      const original = { ...message(80, 11), text: 'sample_user' };
      const target = { ...message(), text, reply_to_message: original,
        from: { ...message().from, is_bot: true, username: 'different_resource_bot' } };
      telegram.send(original);
      telegram.send(target);
      const field = edited ? 'edited_message' : 'message';
      assert.equal((await dispatch({ update_id: 1, [field]: target })).status, 200);
      assert.equal(telegram.has(81), false);
      assert.equal(telegram.has(80), true);
      assert.equal(telegram.canSend(22), true);
      assert.equal(telegram.canJoin(22), true);
      assert.equal(model.state, null);
      assert.equal((await database.prepare("SELECT COUNT(*) AS n FROM model_tasks WHERE phase != 'done'").first()).n, 0);
      assert.equal((await database.prepare('SELECT COUNT(*) AS n FROM blacklisted_users').first()).n, 0);
      assert.deepEqual((await database.prepare('SELECT source_id FROM blacklisted_sources').all()).results,
        [{ source_id: 7788 }]);
    });
  }
}

test('user: Given an unregistered bot with no model key, When it sends the trigger and ten resource messages, Then regex removes every message without muting or blacklisting mentioned accounts', async () => {
  await setBindings({ EXPERIENTIAL_API_KEY: undefined });
  telegram.send(message(80, 11));
  for (let index = 0; index < 11; index++) {
    const target = { ...message(81 + index), text: index === 0 ? trigger : resources,
      from: { ...message().from, is_bot: true, username: 'another_resource_bot' } };
    telegram.send(target);
    assert.equal((await dispatch({ update_id: index + 1, message: target })).status, 200);
    assert.equal(telegram.has(target.message_id), false);
  }
  assert.equal(telegram.has(80), true);
  assert.equal(telegram.canSend(22), true);
  assert.equal(telegram.canJoin(22), true);
  assert.equal(model.state, null);
  assert.equal((await database.prepare('SELECT COUNT(*) AS n FROM blacklisted_users').first()).n, 0);
  assert.equal((await database.prepare('SELECT COUNT(*) AS n FROM model_tasks').first()).n, 0);
});

for (const text of [trigger, resources]) {
  for (const established of [false, true]) {
    test(`user: Given a ${established ? 'mature' : 'low-history'} human's resource ${text === trigger ? 'trigger' : 'list'}, When received as a reply, Then ${established ? 'Luna retains semantic control' : 'regex acts before Luna'}`, async () => {
      if (established) await database.prepare('INSERT INTO member_activity VALUES (123,-10012,22,10)').run();
      const original = message(80, 11);
      const target = { ...message(), text, reply_to_message: original };
      if (text === trigger) target.entities = [{ type: 'bot_command', offset: 0, length: 6 }];
      telegram.send(original);
      telegram.send(target);
      assert.equal((await dispatch({ update_id: 1, message: target })).status, 200);
      assert.equal(telegram.has(81), established);
      assert.equal(telegram.has(80), true);
      assert.equal(telegram.canSend(22), established);
      if (established) assert.equal(model.state.message.text, text);
      else assert.equal(model.state, null);
    });
  }
}

test('user: Given ordinary start commands, mentions, sensitive news and normal search discussions, When sent by an unregistered bot, Then nothing is deleted or sent to Luna', async () => {
  const samples = ['/start', '/start 501', '/start 501 @alice_bot', '@alice_bot @bobby_bot 看一下',
    '新闻讨论暴力强奸案件', '点我搜索 Python 文档', '正常搜索开源资源', '万能检索如何设计？'];
  for (const [index, text] of samples.entries()) {
    const target = { ...message(81 + index), text, from: { ...message().from, is_bot: true },
      reply_to_message: { ...message(80, 11), text: resources } };
    telegram.send(target);
    assert.equal((await dispatch({ update_id: index + 1, message: target })).status, 200);
    assert.equal(telegram.has(target.message_id), true);
  }
  assert.equal(telegram.canSend(22), true);
  assert.equal(model.state, null);
});

test('user: Given a protected bot administrator sending a matching resource template, When received, Then authority protection still keeps the message', async () => {
  telegram.members.set(22, { status: 'administrator' });
  const target = { ...message(), text: resources, from: { ...message().from, is_bot: true } };
  telegram.send(target);
  assert.equal((await dispatch({ update_id: 1, message: target })).status, 200);
  assert.equal(telegram.has(81), true);
  assert.equal(telegram.canSend(22), true);
  assert.equal(model.state, null);
});
