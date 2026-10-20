import assert from 'node:assert/strict';
import { test } from 'node:test';
import { database, dispatch, model, setBindings, task, telegram, tick } from './worker-runtime.mjs';
import { message } from './telegram-fake.mjs';

model.enabled = true;
const member = (id = 22, first_name = 'New member', is_bot = false) => ({ id, first_name, is_bot });
function join(members = [member()], id = 81, actor = 11) {
  const notice = { ...message(id, actor), new_chat_members: members };
  delete notice.text;
  return { update_id: id, message: notice };
}

for (const field of ['nickname', 'bio']) {
  test(`user: Given a joining account advertising in its ${field}, When classified without authored text, Then only that account is permanently banned and its join notice removed`, async () => {
    model.probability = 0.96;
    model.profiles.set(22, { bio: field === 'bio' ? 'Private tutoring services, contact @example_contact' : '' });
    const update = join([member(22, field === 'nickname' ? 'Private tutor accepting clients' : 'New member')]);
    telegram.send(message(80, 11));
    telegram.send(update.message);
    assert.equal((await dispatch(update)).status, 200);
    assert.equal(telegram.canJoin(22), false);
    assert.equal(telegram.members.get(22).until_date, 0);
    assert.equal(telegram.members.get(11).status, 'administrator');
    assert.equal(telegram.has(81), false);
    assert.equal(telegram.has(80), true);
    assert.deepEqual((await database.prepare('SELECT bot_id, user_id FROM blacklisted_users').all()).results,
      [{ bot_id: 123, user_id: 22 }]);
    assert.equal(model.state.message, undefined, 'a joiner has not authored the service notice');
    assert.equal(model.state.nickname, update.message.new_chat_members[0].first_name);
    assert.equal((await task()).input_json, null);
  });
}

for (const profile of ['nickname', 'bio', 'blacklist']) {
  test(`user: Given a joining account matching existing ${profile} filtering, When no model is configured, Then it is banned and the inviter is untouched`, async () => {
    await setBindings({ EXPERIENTIAL_API_KEY: undefined });
    if (profile === 'blacklist') await database.prepare('INSERT INTO blacklisted_users (bot_id, user_id, added_at) VALUES (123,22,0)').run();
    model.profiles.set(22, { bio: profile === 'bio' ? '萝莉上门' : '' });
    const update = join([member(22, profile === 'nickname' ? '萝莉上门' : 'New member')]);
    telegram.send(update.message);
    assert.equal((await dispatch(update)).status, 200);
    assert.equal(telegram.canJoin(22), false);
    assert.equal(telegram.has(81), false);
    assert.equal(telegram.members.get(11).status, 'administrator');
    assert.equal(model.state, null);
  });
}

test('user: Given mixed new members in one notice, When their profiles differ, Then independent decisions ban only advertising members', async () => {
  telegram.members.set(33, { status: 'member' });
  telegram.members.set(44, { status: 'member' });
  model.response = () => Response.json({ answers: [{ name: 'spam', type: 'predicate', probability: model.state.nickname === 'Normal student' ? 0.1 : 0.96 }] });
  const update = join([member(22, 'Ad one'), member(33, 'Normal student'), member(44, 'Ad two')]);
  telegram.send(update.message);
  assert.equal((await dispatch(update)).status, 200);
  assert.equal(telegram.canJoin(22), false);
  assert.equal(telegram.canJoin(33), true);
  assert.equal(telegram.canJoin(44), false);
  assert.equal(telegram.has(81), false);
  assert.deepEqual((await database.prepare('SELECT user_id FROM blacklisted_users ORDER BY user_id').all()).results,
    [{ user_id: 22 }, { user_id: 44 }]);
  assert.equal((await dispatch(update)).status, 200);
  assert.equal(telegram.canJoin(33), true);
});

for (const status of ['administrator', 'creator']) {
  test(`user: Given a joining ${status} with an advertising profile, When the join notice is processed, Then their account and notice remain protected`, async () => {
    telegram.members.set(22, { status });
    model.probability = 1;
    const update = join([member(22, '萝莉上门')]);
    telegram.send(update.message);
    assert.equal((await dispatch(update)).status, 200);
    assert.equal(telegram.has(81), true);
    assert.equal(telegram.members.get(22).status, status);
    assert.equal((await database.prepare('SELECT COUNT(*) AS n FROM blacklisted_users').first()).n, 0);
  });
}

test('user: Given a blacklisted inviter but a benign joiner, When a notice arrives, Then the inviter is not mistaken for the joined account', async () => {
  await database.prepare('INSERT INTO blacklisted_users (bot_id, user_id, added_at) VALUES (123,22,0)').run();
  telegram.members.set(33, { status: 'member' });
  const update = join([member(33, 'Normal student')], 81, 22);
  telegram.send(update.message);
  assert.equal((await dispatch(update)).status, 200);
  assert.equal(telegram.canJoin(22), true);
  assert.equal(telegram.canJoin(33), true);
  assert.equal(telegram.has(81), true);
  assert.equal((await database.prepare('SELECT COUNT(*) AS n FROM recent_messages').first()).n, 0);
});

test('user: Given a transient join classification failure, When the scheduled retry succeeds, Then the member is banned without a chat message', async () => {
  model.response = new Response('busy', { status: 503 });
  const update = join();
  telegram.send(update.message);
  assert.equal((await dispatch(update)).status, 200);
  assert.equal(telegram.canJoin(22), true);
  const pending = await task();
  assert.ok(pending, 'the join decision must survive the webhook');
  model.response = null;
  model.probability = 0.96;
  await tick(pending.due_at);
  assert.equal(telegram.canJoin(22), false);
  assert.equal(telegram.has(81), false);
});

test('user: Given a completed join ban and failed notice deletion, When retried after manual unban, Then only the notice cleanup resumes', async () => {
  model.probability = 0.96;
  telegram.faults.set('deleteMessage:81', () => Response.json({ ok: false, error_code: 500 }, { status: 500 }));
  const update = join();
  telegram.send(update.message);
  assert.equal((await dispatch(update)).status, 200);
  assert.equal(telegram.canJoin(22), false);
  const pending = await task();
  assert.equal(pending.phase, 'delete');
  telegram.members.set(22, { status: 'member' });
  telegram.faults.clear();
  await tick(pending.due_at);
  assert.equal(telegram.canJoin(22), true);
  assert.equal(telegram.has(81), false);
});

for (const members of [{ id: 22 }, [], [member(0)], [member(true)], [member(22, 'New member', 'false')], [null]]) {
  test(`user: Given malformed joined identities ${JSON.stringify(members)}, When delivered, Then validation rejects them before moderation`, async () => {
    const update = join(members);
    telegram.send(update.message);
    assert.equal((await dispatch(update)).status, 400);
    assert.equal(telegram.canJoin(22), true);
    assert.equal(telegram.has(81), true);
  });
}

test('user: Given an edited join notice, When delivered, Then no joined account is reclassified or punished', async () => {
  model.probability = 1;
  const update = join([member(22, '萝莉上门')]);
  telegram.send(update.message);
  assert.equal((await dispatch({ update_id: 91, edited_message: update.message })).status, 200);
  assert.equal(telegram.canJoin(22), true);
  assert.equal(telegram.has(81), true);
  assert.equal(model.state, null);
});

test('user: Given a registered source bot joining without speaking, When its join notice arrives, Then only that bot is banned', async () => {
  telegram.members.set(7788, { status: 'member' });
  const update = join([member(7788, 'Example bot', true)]);
  telegram.send(update.message);
  assert.equal((await dispatch(update)).status, 200);
  assert.equal(telegram.canJoin(7788), false);
  assert.equal(telegram.has(81), false);
  assert.equal(telegram.members.get(11).status, 'administrator');
  assert.equal(model.state, null);
});
