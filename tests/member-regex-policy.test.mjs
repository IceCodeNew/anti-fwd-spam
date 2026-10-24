import assert from 'node:assert/strict';
import { test } from 'node:test';
import { database, dispatch, model, setBindings, telegram } from './worker-runtime.mjs';
import { message, report } from './telegram-fake.mjs';

model.enabled = true;
const campaign = '@synthetic_bot campaign_001 synthetic_marker';
const bulkOffer = '承接广*告*戴*發，十万精准各行业裙\n包效果 包效果 @sample_contact';
function referenced(current, kind) {
  const earlier = { ...message(80, 11), text: 'ordinary discussion' };
  if (kind === 'same_chat') current.reply_to_message = earlier;
  else if (kind === 'external') current.external_reply = { origin: { type: 'hidden_user' } };
  else current.quote = { text: earlier.text };
  return current;
}

for (const text of [campaign, bulkOffer, '💰'.repeat(192), '萝莉上门', '看头像q <contact>', '本群已停用，请加入新群 https://example.invalid/group']) {
  for (const context of ['same_chat', 'external', 'quote']) {
    test(`user: a low-history member's own ${JSON.stringify(Array.from(text).slice(0, 25).join(''))} in ${context} is locally moderated under the shared policy`, async () => {
      await setBindings({ EXPERIENTIAL_API_KEY: undefined });
      const current = referenced({ ...message(), text }, context);
      telegram.send(message(70));
      telegram.send(message(80, 11));
      telegram.send(current);
      assert.equal((await dispatch({ update_id: 1, message: current })).status, 200);
      assert.equal(telegram.has(81), false);
      assert.equal(telegram.has(80), true);
      assert.equal(telegram.has(70), true);
      assert.equal(telegram.canSend(22), false);
      assert.equal(telegram.canJoin(22), true);
      assert.equal(model.state, null);
      assert.equal((await database.prepare('SELECT COUNT(*) AS n FROM blacklisted_users').first()).n, 0);
    });
  }
}

for (const [field, edited] of [['caption', false], ['caption', true], ['nickname', false], ['biography', false]]) {
  test(`user: the same low-history gate covers ${field} with edited=${edited} and a reference`, async () => {
    const current = referenced(message(), 'same_chat');
    if (field === 'nickname') current.from.first_name = campaign;
    else if (field === 'biography') model.profile = { bio: campaign };
    else { delete current.text; current.caption = campaign; }
    telegram.send(current);
    assert.equal((await dispatch({ update_id: 1, [edited ? 'edited_message' : 'message']: current })).status, 200);
    assert.equal(telegram.has(81), false);
    assert.equal(telegram.canSend(22), false);
    assert.equal(model.state, null);
  });
}

for (const [reference, edited] of [['none', false], ['same_chat', false], ['external', true], ['none', true]]) {
  for (const text of [campaign, bulkOffer, '💰'.repeat(192), `请警惕 ${campaign}，不要点击`, '本群已停用，请加入新群 https://example.invalid/group']) {
    test(`user: ten matured observations send ${JSON.stringify(Array.from(text).slice(0, 20).join(''))} in ${reference}, edited=${edited} to Luna instead of direct regex punishment`, async () => {
      await database.prepare('INSERT INTO member_activity VALUES (123,-10012,22,10)').run();
      model.profiles.set(22, { bio: campaign });
      const current = { ...message(), text };
      if (reference !== 'none') referenced(current, reference);
      telegram.send(current);
      assert.equal((await dispatch({ update_id: 1, [edited ? 'edited_message' : 'message']: current })).status, 200);
      assert.equal(telegram.has(81), true);
      assert.equal(telegram.canSend(22), true);
      assert.equal(model.state.message.text, text);
      assert.equal(model.state.bio, campaign);
    });
  }
}

test('user: an established member can still be moderated when Luna confirms an advertisement', async () => {
  await database.prepare('INSERT INTO member_activity VALUES (123,-10012,22,10)').run();
  model.probability = 0.96;
  const current = { ...message(), text: campaign };
  telegram.send(message(70));
  telegram.send(current);
  assert.equal((await dispatch({ update_id: 1, message: current })).status, 200);
  assert.equal(telegram.has(81), false);
  assert.equal(telegram.has(70), true);
  assert.equal(telegram.canSend(22), false);
  assert.equal(telegram.canJoin(22), true);
});

for (const status of ['administrator', 'creator', 'anonymous']) {
  test(`user: low-history regex matches preserve ${status} authority protection`, async () => {
    const current = referenced({ ...message(), text: campaign }, 'same_chat');
    if (status === 'anonymous') current.sender_chat = current.chat;
    else telegram.members.set(22, { status });
    telegram.send(current);
    assert.equal((await dispatch({ update_id: 1, message: current })).status, 200);
    assert.equal(telegram.has(81), true);
  });
}

test('user: an unlisted reply report remains evidence-only before the low-history filter', async () => {
  const update = report({ ...message(80, 33), text: campaign });
  update.message.from = message().from;
  telegram.send(update.message);
  telegram.send(update.message.reply_to_message);
  assert.equal((await dispatch(update)).status, 200);
  assert.equal(telegram.has(82), true);
  assert.equal(telegram.has(80), true);
  assert.equal(telegram.canSend(22), true);
});

test('user: a temporary low-history deletion failure retries only the current message', async () => {
  const current = referenced({ ...message(), text: '💰'.repeat(192) }, 'same_chat');
  telegram.send(current);
  telegram.send(current.reply_to_message);
  telegram.faults.set('deleteMessage', () => Response.json({ ok: false, error_code: 429 }, { status: 429 }));
  assert.equal((await dispatch({ update_id: 1, message: current })).status, 503);
  assert.equal(telegram.has(81), true);
  assert.equal(telegram.canSend(22), true);
  telegram.faults.clear();
  assert.equal((await dispatch({ update_id: 1, message: current })).status, 200);
  assert.equal(telegram.has(81), false);
  assert.equal(telegram.canSend(22), false);
  assert.equal(telegram.has(80), true);
});

// No local matches: fake scores test routing independently of advertising intent.
const modelOnlyMessages = [
  ['text offer', { text: '出+86裙發号 搭配运控端口 不封浩\n2秒發一个裙  日蕟4W @sample_contact' }],
  ['caption offer', { caption: '接TG厂吿𠰺发/𠰺查同行裙\n日发千万 统统高质量 @sample_contact' }],
  ['button offer', { text: '123', reply_markup: { inline_keyboard: [
    [{ text: '1️⃣🔥卖薇信号/手机咔/接码等-啥都有', url: 'https://example.invalid/menu' }],
  ] } }],
];
for (const [name, content] of modelOnlyMessages) {
  for (const mature of [false, true]) {
    for (const edited of [false, true]) {
      for (const probability of [0, 0.90]) {
        test(`user: a ${name} without a local match follows score=${probability}, mature=${mature}, edited=${edited}`, async () => {
          if (mature) await database.prepare('INSERT INTO member_activity VALUES (123,-10012,22,10)').run();
          model.probability = probability;
          const current = referenced({ ...message(), ...structuredClone(content) }, 'same_chat');
          if (!Object.hasOwn(content, 'text')) delete current.text;
          telegram.send(message(70));
          telegram.send(current.reply_to_message);
          telegram.send(current);
          assert.equal((await dispatch({ update_id: 1, [edited ? 'edited_message' : 'message']: current })).status, 200);
          assert.equal(model.state.nickname, current.from.first_name);
          assert.equal(model.state.message.text, current.text);
          assert.equal(model.state.message.caption, current.caption);
          if (content.reply_markup) assert.ok(model.state.message.reply_markup.includes(content.reply_markup.inline_keyboard[0][0].text));
          assert.equal(telegram.has(81), probability === 0);
          assert.equal(telegram.canSend(22), probability === 0);
          assert.equal(telegram.has(70), true);
          assert.equal(telegram.has(80), true);
          assert.equal(telegram.canSend(11), true);
          assert.equal(telegram.canJoin(22), true);
          assert.equal((await database.prepare('SELECT COUNT(*) AS n FROM blacklisted_users').first()).n, 0);
        });
      }
    }
  }

  for (const edited of [false, true]) {
    test(`user: a bot's ${name} without a local match stays outside model classification, edited=${edited}`, async () => {
      model.probability = 0.90;
      const current = referenced({ ...message(), ...structuredClone(content) }, 'same_chat');
      if (!Object.hasOwn(content, 'text')) delete current.text;
      current.from.is_bot = true;
      telegram.send(current.reply_to_message);
      telegram.send(current);
      assert.equal((await dispatch({ update_id: 1, [edited ? 'edited_message' : 'message']: current })).status, 200);
      assert.equal(model.state, null);
      assert.equal(telegram.has(81), true);
      assert.equal(telegram.has(80), true);
      assert.equal(telegram.canSend(22), true);
      assert.equal(telegram.canJoin(22), true);
      assert.equal((await database.prepare('SELECT COUNT(*) AS n FROM blacklisted_users').first()).n, 0);
    });
  }
}

for (const source of ['caption', 'button', 'reference']) {
  test(`user: a warning in ${source} keeps its source when the model allows the message`, async () => {
    model.probability = 0;
    const warning = '请举报这个广告样本，不要购买';
    const current = referenced({ ...message(), ...structuredClone(modelOnlyMessages[2][1]) }, 'same_chat');
    if (source === 'caption') current.caption = warning;
    else if (source === 'button') current.reply_markup.inline_keyboard.unshift([{ text: warning }]);
    else {
      current.reply_to_message.reply_markup = current.reply_markup;
      delete current.reply_markup;
      current.text = warning;
    }
    telegram.send(current.reply_to_message);
    telegram.send(current);
    assert.equal((await dispatch({ update_id: 1, message: current })).status, 200);
    if (source === 'caption') assert.equal(model.state.message.caption, warning);
    else if (source === 'button') assert.ok(model.state.message.reply_markup.includes(warning));
    else {
      assert.equal(model.state.message.reply_markup, undefined);
      assert.ok(model.state.context.reply.reply_markup.includes(modelOnlyMessages[2][1].reply_markup.inline_keyboard[0][0].text));
    }
    assert.equal(telegram.has(81), true);
    assert.equal(telegram.has(80), true);
    assert.equal(telegram.canSend(22), true);
  });
}

const sexualResource = '成人cos私密写真资源，完整版进频道观看';
function resourceReference(current, kind) {
  const earlier = { ...message(80, 11), text: sexualResource };
  if (kind === 'reply') current.reply_to_message = earlier;
  else if (kind === 'caption') {
    delete earlier.text;
    earlier.caption = sexualResource;
    current.reply_to_message = earlier;
  } else {
    current.quote = { text: sexualResource };
    if (kind === 'external') current.external_reply = { origin: { type: 'channel', chat: { title: '资源分享' } } };
  }
  return current;
}

for (const [kind, edited] of [['reply', false], ['caption', false], ['external', true], ['quote', false]]) {
  test("user: exact sexual endorsement with " + kind + ", edited=" + edited + " is locally moderated without a model", async () => {
    await setBindings({ EXPERIENTIAL_API_KEY: undefined });
    const current = resourceReference({ ...message(), text: '射了' }, kind);
    telegram.send(message(70));
    telegram.send(message(80, 11));
    telegram.send(current);
    assert.equal((await dispatch({ update_id: 1, [edited ? 'edited_message' : 'message']: current })).status, 200);
    assert.equal(telegram.has(81), false);
    assert.equal(telegram.canSend(22), false);
    assert.equal(telegram.canJoin(22), true);
    assert.equal(telegram.has(80), true);
    assert.equal(telegram.has(70), true);
    assert.equal(telegram.canSend(11), true);
    assert.equal(model.state, null);
    assert.equal((await database.prepare('SELECT COUNT(*) AS n FROM blacklisted_users').first()).n, 0);
  });
}

for (const kind of ['reply', 'external']) {
  for (const text of ['这是什么？', '举报了', '这种广告用“射了”附和来引流，请处理']) {
    test("user: a neutral reply or report of sexual resources in " + kind + " remains without a model: " + text, async () => {
      await setBindings({ EXPERIENTIAL_API_KEY: undefined });
      const current = resourceReference({ ...message(), text }, kind);
      telegram.send(current);
      assert.equal((await dispatch({ update_id: 1, message: current })).status, 200);
      assert.equal(telegram.has(81), true);
      assert.equal(telegram.canSend(22), true);
      assert.equal(model.state, null);
    });
  }
  test("user: a below-threshold model answer still protects an established sender of a local match in " + kind, async () => {
    await database.prepare('INSERT INTO member_activity VALUES (123,-10012,22,10)').run();
    const current = resourceReference({ ...message(), text: '射了' }, kind);
    model.probability = 0;
    telegram.send(current);
    assert.equal((await dispatch({ update_id: 1, message: current })).status, 200);
    assert.equal(telegram.has(81), true);
    assert.equal(telegram.canSend(22), true);
    assert.equal(model.state.message.text, '射了');
    assert.ok(model.state.context);
  });
}

for (const field of ['text', 'caption']) {
  test('user: a partial quote cannot strip the warning from its visible reply ' + field, async () => {
    await setBindings({ EXPERIENTIAL_API_KEY: undefined });
    const earlier = { ...message(80, 11) };
    delete earlier.text;
    earlier[field] = '请举报' + sexualResource + '，不要传播。';
    const current = { ...message(), text: '射了', reply_to_message: earlier, quote: { text: sexualResource } };
    telegram.send(earlier);
    telegram.send(current);
    assert.equal((await dispatch({ update_id: 1, message: current })).status, 200);
    assert.equal(telegram.has(81), true);
    assert.equal(telegram.canSend(22), true);
    assert.equal(telegram.has(80), true);
    assert.equal(telegram.canSend(11), true);
    assert.equal(model.state, null);
  });
}

for (const authority of ['administrator', 'creator', 'anonymous']) {
  test("user: the sexual endorsement rule preserves " + authority + " protection", async () => {
    await setBindings({ EXPERIENTIAL_API_KEY: undefined });
    const current = resourceReference({ ...message(), text: '射了' }, 'reply');
    if (authority === 'anonymous') current.sender_chat = current.chat;
    else telegram.members.set(22, { status: authority });
    telegram.send(current);
    assert.equal((await dispatch({ update_id: 1, message: current })).status, 200);
    assert.equal(telegram.has(81), true);
  });
}

test('user: a bot sexual endorsement is deleted without muting its guest caller or banning its reference', async () => {
  await setBindings({ EXPERIENTIAL_API_KEY: undefined });
  const current = resourceReference({ ...message(), text: '射了' }, 'reply');
  current.from.is_bot = true;
  current.guest_bot_caller_user = { id: 11, is_bot: false, first_name: 'Caller' };
  telegram.send(message(80, 11));
  telegram.send(current);
  assert.equal((await dispatch({ update_id: 1, message: current })).status, 200);
  assert.equal(telegram.has(81), false);
  assert.equal(telegram.canSend(22), true);
  assert.equal(telegram.has(80), true);
  assert.equal(telegram.canSend(11), true);
  assert.equal(model.state, null);
  assert.equal((await database.prepare('SELECT COUNT(*) AS n FROM blacklisted_users').first()).n, 0);
});

for (const kind of ['reply', 'caption', 'external']) {
  for (const edited of [false, true]) {
    for (const [text, probability] of [['射了', 0.90], ['射了？', 0], ['这是什么？', 0], ['举报了', 0]]) {
      test("user: an established member's " + kind + ", edited=" + edited + " is decided by the model without bypassing reference protection: " + text, async () => {
        await database.prepare('INSERT INTO member_activity VALUES (123,-10012,22,10)').run();
        const current = resourceReference({ ...message(), text }, kind);
        model.probability = probability;
        telegram.send(message(70));
        telegram.send(message(80, 11));
        telegram.send(current);
        assert.equal((await dispatch({ update_id: 1, [edited ? 'edited_message' : 'message']: current })).status, 200);
        assert.equal(model.state.message.text, text, 'an established sender must reach the model');
        const context = model.state.context;
        const referencedText = kind === 'external' ? context.quoted_text
          : kind === 'caption' ? context.reply.caption : context.reply.text;
        assert.equal(referencedText, sexualResource);
        assert.equal(telegram.has(81), probability < 0.90);
        assert.equal(telegram.canSend(22), probability < 0.90);
        assert.equal(telegram.canJoin(22), true);
        assert.equal(telegram.has(80), true);
        assert.equal(telegram.has(70), true);
        assert.equal(telegram.canSend(11), true);
        assert.equal((await database.prepare('SELECT COUNT(*) AS n FROM blacklisted_users').first()).n, 0);
      });
    }
  }
}
