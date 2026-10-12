import assert from 'node:assert/strict';
import { test } from 'node:test';
import { database, dispatch, model, setBindings, telegram } from './worker-runtime.mjs';
import { message, report } from './telegram-fake.mjs';

model.enabled = true;
const campaign = '@synthetic_bot campaign_001 synthetic_marker';
function referenced(current, kind) {
  const earlier = { ...message(80, 11), text: 'ordinary discussion' };
  if (kind === 'same_chat') current.reply_to_message = earlier;
  else if (kind === 'external') current.external_reply = { origin: { type: 'hidden_user' } };
  else current.quote = { text: earlier.text };
  return current;
}

for (const context of ['same_chat', 'external', 'quote']) {
  for (const field of ['text', 'caption']) {
    for (const edited of [false, true]) {
      test(`user: explicit campaign in ${field}, ${context}, edited=${edited} is moderated without model approval`, async () => {
        if (!edited) await setBindings({ EXPERIENTIAL_API_KEY: undefined });
        const earlier = message(80, 11);
        telegram.send(earlier);
        const current = referenced(message(), context);
        delete current.text;
        current[field] = `  ${campaign}\n`;
        telegram.send(current);
        assert.equal((await dispatch({ update_id: 1, [edited ? 'edited_message' : 'message']: current })).status, 200);
        assert.equal(telegram.has(81), false);
        assert.equal(telegram.canSend(22), false);
        assert.equal(telegram.canJoin(22), true);
        assert.equal(telegram.has(80), true);
        assert.equal(telegram.canSend(11), true);
        assert.equal(model.state, null);
        assert.equal(await database.prepare('SELECT 1 FROM blacklisted_users').first(), null);
        assert.deepEqual((await database.prepare('SELECT source_id FROM blacklisted_sources').all()).results,
          [{ source_id: 7788 }]);
      });
    }
  }
}

for (const text of ['@synthetic_bot campaign_1', '@abcde campaign_0 marker_2',
  `@${'a'.repeat(32)} campaign_123`, '@synthetic_bot\tcampaign_2\tmarker']) {
  test(`user: complete campaign syntax ${JSON.stringify(text)} is handled in a reply`, async () => {
    const current = referenced({ ...message(), text }, 'same_chat');
    telegram.send(current);
    assert.equal((await dispatch({ update_id: 1, message: current })).status, 200);
    assert.equal(telegram.has(81), false);
    assert.equal(telegram.canSend(22), false);
  });
}

for (const text of ['@synthetic_bot', '这是广告，别点', `请警惕 ${campaign}，不要点击`,
  `${campaign} 这是举报`, '@synthetic_bot campaign_1 this is a discussion',
  '@synthetic_bot campaign_1a', '@synthetic_bot campaign_١', '@abcd campaign_1',
  `@${'a'.repeat(33)} campaign_1`, '@synthetic_bot campaign_1 marker-with-hyphens']) {
  test(`user: discussion or incomplete campaign ${JSON.stringify(text)} stays on the model path`, async () => {
    const current = referenced({ ...message(), text }, 'same_chat');
    current.reply_to_message.text = campaign;
    telegram.send(current.reply_to_message);
    telegram.send(current);
    assert.equal((await dispatch({ update_id: 1, message: current })).status, 200);
    assert.equal(telegram.has(81), true);
    assert.equal(telegram.has(80), true);
    assert.equal(telegram.canSend(22), true);
    assert.equal(model.state.message.text, text);
  });
}

for (const status of ['administrator', 'creator', 'anonymous']) {
  test(`user: campaign reply from ${status} retains existing protection`, async () => {
    const current = referenced({ ...message(), text: campaign }, 'same_chat');
    if (status === 'anonymous') current.sender_chat = current.chat;
    else telegram.members.set(22, { status });
    telegram.send(current);
    assert.equal((await dispatch({ update_id: 1, message: current })).status, 200);
    assert.equal(telegram.has(81), true);
  });
}

test('user: unlisted reporter quoting a campaign remains evidence-only', async () => {
  const update = report({ ...message(80, 33), text: campaign });
  update.message.from = message().from;
  telegram.send(update.message);
  telegram.send(update.message.reply_to_message);
  assert.equal((await dispatch(update)).status, 200);
  assert.equal(telegram.has(82), true);
  assert.equal(telegram.has(80), true);
  assert.equal(telegram.canSend(22), true);
});

test('user: temporary deletion failure retries the current campaign reply without punishing its reference', async () => {
  const current = referenced({ ...message(), text: campaign }, 'same_chat');
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
