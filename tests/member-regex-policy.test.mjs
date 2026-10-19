import assert from 'node:assert/strict';
import { test } from 'node:test';
import { database, dispatch, model, setBindings, telegram } from './worker-runtime.mjs';
import { message, report } from './telegram-fake.mjs';

model.enabled = true;
const campaign = '@synthetic_bot campaign_001 synthetic_marker';
const bulkOffer = '承接广*告*戴*發，十万精准各行业裙\n包效果 包效果 @sample_contact';
const distributionOffers = [
  '出+86裙發号 搭配运控端口 不封浩\n2秒發一个裙  日蕟4W @sample_contact',
  '接TG厂吿𠰺发/𠰺查同行裙\n日发千万 统统高质量 @sample_contact',
];
const salesMenu = { inline_keyboard: [[{ text: '1️⃣🔥卖薇信号/手机咔/接码等-啥都有', callback_data: 'menu' }]] };
function referenced(current, kind) {
  const earlier = { ...message(80, 11), text: 'ordinary discussion' };
  if (kind === 'same_chat') current.reply_to_message = earlier;
  else if (kind === 'external') current.external_reply = { origin: { type: 'hidden_user' } };
  else current.quote = { text: earlier.text };
  return current;
}

for (const text of [campaign, bulkOffer, ...distributionOffers, '💰'.repeat(192), '萝莉上门', '看头像q <contact>', '本群已停用，请加入新群 https://example.invalid/group']) {
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
  for (const text of [campaign, bulkOffer, ...distributionOffers, '💰'.repeat(192), `请警惕 ${campaign}，不要点击`, '本群已停用，请加入新群 https://example.invalid/group']) {
    test(`user: ten matured observations send ${JSON.stringify(Array.from(text).slice(0, 20).join(''))} in ${reference}, edited=${edited} to Jev instead of direct regex punishment`, async () => {
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

for (const [bot, edited] of [[false, false], [false, true], [true, false], [true, true]]) {
  test(`user: the exact sales menu with a numeric body is locally removed for bot=${bot}, edited=${edited}`, async () => {
    await setBindings({ EXPERIENTIAL_API_KEY: undefined });
    const current = referenced({ ...message(), text: '1792391006137', reply_markup: salesMenu }, 'same_chat');
    current.from.is_bot = bot;
    telegram.send(current);
    telegram.send(current.reply_to_message);
    assert.equal((await dispatch({ update_id: 1, [edited ? 'edited_message' : 'message']: current })).status, 200);
    assert.equal(telegram.has(81), false);
    assert.equal(telegram.has(80), true);
    assert.equal(telegram.canSend(22), bot);
    assert.equal(model.state, null);
  });
}

for (const protection of ['mature', 'administrator', 'creator', 'anonymous', 'warning', 'rich_warning', 'reference']) {
  test(`user: the sales menu preserves ${protection} source and authority boundaries`, async () => {
    const current = { ...message(), text: '123', reply_markup: salesMenu };
    if (protection === 'mature') await database.prepare('INSERT INTO member_activity VALUES (123,-10012,22,10)').run();
    else if (protection === 'anonymous') current.sender_chat = current.chat;
    else if (protection === 'warning') current.text = '请举报这个售号菜单';
    else if (protection === 'rich_warning') current.rich_message = { text: '请举报这个售号菜单' };
    else if (protection === 'reference') {
      delete current.reply_markup;
      current.text = '请核实';
      current.reply_to_message = { ...message(80, 11), text: '123', reply_markup: salesMenu };
    } else telegram.members.set(22, { status: protection });
    telegram.send(current);
    assert.equal((await dispatch({ update_id: 1, message: current })).status, 200);
    assert.equal(telegram.has(81), true);
    assert.equal(telegram.canSend(22), true);
    if (protection === 'mature') assert.ok(model.state.message.reply_markup.includes(salesMenu.inline_keyboard[0][0].text));
  });
}

test('user: an established member can still be moderated when Jev confirms an advertisement', async () => {
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
