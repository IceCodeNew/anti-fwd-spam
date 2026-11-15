import assert from 'node:assert/strict';
import { test } from 'node:test';
import { message } from './telegram-fake.mjs';
import { database, dispatch, runtime, setBindings, telegram } from './worker-runtime.mjs';

const reviewSecret = 'review-test-secret';
const reviewHeaders = { 'x-review-secret': reviewSecret };

function unlistedReport(updateId = 800) {
  const target = { ...message(81, 33), text: 'An offer the filters missed' };
  return {
    update_id: updateId,
    message: {
      ...message(95, 42), reply_to_message: target, text: '@test_gate_bot spam',
      entities: [{ type: 'mention', offset: 0, length: 14 }],
    },
  };
}

async function pending(headers = reviewHeaders) {
  return runtime.dispatchFetch('https://worker.test/reviews/pending', { headers });
}

async function samples(headers = reviewHeaders) {
  return runtime.dispatchFetch('https://worker.test/reviews/samples', { headers });
}

function listedReport(updateId = 802) {
  const target = { ...message(81, 33), text: 'An offer the filters missed' };
  return {
    update_id: updateId,
    message: {
      ...message(95, 11), reply_to_message: target, text: '@test_gate_bot spam',
      entities: [{ type: 'mention', offset: 0, length: 14 }],
    },
  };
}

async function resolve(updateId, verdict) {
  return runtime.dispatchFetch('https://worker.test/reviews/resolve', {
    method: 'POST',
    headers: { ...reviewHeaders, 'content-type': 'application/json' },
    body: JSON.stringify({ update_id: updateId, verdict }),
  });
}

test('user: Given no review secret, When the review endpoints are called, Then they do not exist', async () => {
  await setBindings({ REVIEW_SECRET: undefined });
  assert.equal((await pending()).status, 404);
  assert.equal((await resolve(800, 'spam')).status, 404);
  assert.equal((await samples()).status, 404);
});

test('user: Given a review secret, When an unlisted member reports spam, Then a reviewer can fetch and resolve it', async () => {
  await setBindings({ REVIEW_SECRET: reviewSecret });
  assert.equal((await pending({ 'x-review-secret': 'wrong' })).status, 401);
  const update = unlistedReport();
  telegram.send(update.message.reply_to_message);
  telegram.send(update.message);
  telegram.members.set(33, { status: 'member' });
  assert.equal((await dispatch(update)).status, 200);
  assert.equal(telegram.has(81), true);

  const listed = await pending();
  assert.equal(listed.status, 200);
  const { reviews } = await listed.json();
  assert.equal(reviews.length, 1);
  assert.equal(reviews[0].update_id, 800);
  assert.equal(reviews[0].target_message_id, 81);
  assert.equal(reviews[0].payload.target.state.message.text, 'An offer the filters missed');

  const resolved = await resolve(800, 'spam');
  assert.equal(resolved.status, 200);
  assert.equal(telegram.has(81), false);
  assert.equal(telegram.has(95), false);
  assert.equal(telegram.canJoin(33), false);
  const row = await database.prepare('SELECT verdict, outcome FROM pending_reviews WHERE update_id = 800').first();
  assert.deepEqual({ verdict: row.verdict, outcome: row.outcome }, { verdict: 'spam', outcome: 'banned' });
  const { reviews: remaining } = await (await pending()).json();
  assert.equal(remaining.length, 0);
});

test('user: Given a queued report, When the verdict is not spam, Then the message remains', async () => {
  await setBindings({ REVIEW_SECRET: reviewSecret });
  const update = unlistedReport(801);
  telegram.send(update.message.reply_to_message);
  telegram.send(update.message);
  assert.equal((await dispatch(update)).status, 200);
  assert.equal((await resolve(801, 'not_spam')).status, 200);
  assert.equal(telegram.has(81), true);
  const row = await database.prepare('SELECT verdict, outcome FROM pending_reviews WHERE update_id = 801').first();
  assert.deepEqual({ verdict: row.verdict, outcome: row.outcome }, { verdict: 'not_spam', outcome: 'kept' });
});

test('user: Given a listed reporter reports spam, When it is acted on, Then it is exposed as a sample', async () => {
  await setBindings({ REVIEW_SECRET: reviewSecret });
  assert.equal((await samples({ 'x-review-secret': 'wrong' })).status, 401);
  const update = listedReport();
  telegram.send(update.message.reply_to_message);
  telegram.send(update.message);
  telegram.members.set(33, { status: 'member' });
  assert.equal((await dispatch(update)).status, 200);
  assert.equal(telegram.has(81), false);
  assert.equal(telegram.canJoin(33), false);

  const { reviews } = await (await pending()).json();
  assert.equal(reviews.length, 0);
  const listed = await samples();
  assert.equal(listed.status, 200);
  const { samples: rows } = await listed.json();
  assert.equal(rows.length, 1);
  assert.equal(rows[0].update_id, 802);
  assert.equal(rows[0].outcome, 'banned');
  assert.equal(rows[0].payload.target.state.message.text, 'An offer the filters missed');
});
