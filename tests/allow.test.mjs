import assert from 'node:assert/strict';
import { test } from 'node:test';
import { database, dispatch, model, setBindings, task, telegram, tick } from './worker-runtime.mjs';
import { chat, message, report } from './telegram-fake.mjs';

function command(text = '/allow 22', id = 300) {
  return { update_id: id, message: { ...message(id, 11), text,
    entities: [{ type: 'bot_command', offset: 0, length: text.split(/\s/u)[0].length }] } };
}

async function seed() {
  telegram.members.set(22, { status: 'kicked', until_date: 0 });
  telegram.members.set(33, { status: 'kicked', until_date: 0 });
  telegram.accounts.set('@ordinary', { id: 22, type: 'private', username: 'ordinary' });
  await database.batch([
    database.prepare('INSERT OR REPLACE INTO blacklisted_users (bot_id, user_id, added_at) VALUES (123, 22, 1), (999, 22, 2), (123, 33, 3)'),
    database.prepare('INSERT OR IGNORE INTO blacklisted_sources (source_id) VALUES (22), (33)'),
    database.prepare("INSERT OR REPLACE INTO blacklisted_sticker_sets (bot_id, set_name, added_at) VALUES (123, 'keep_set', 4)"),
    database.prepare("INSERT OR REPLACE INTO blacklisted_stickers (bot_id, file_unique_id, added_at) VALUES (123, 'keep_sticker', 5)"),
  ]);
}

async function blacklists() {
  const queries = [
    'SELECT * FROM blacklisted_users ORDER BY bot_id, user_id',
    'SELECT * FROM blacklisted_sources ORDER BY source_id',
    'SELECT * FROM blacklisted_sticker_sets ORDER BY bot_id, set_name',
    'SELECT * FROM blacklisted_stickers ORDER BY bot_id, file_unique_id',
  ];
  return Promise.all(queries.map(async sql => (await database.prepare(sql).all()).results));
}

function withoutTarget(rows) {
  return [rows[0].filter(row => row.bot_id !== 123 || row.user_id !== 22),
    rows[1].filter(row => row.source_id !== 22), rows[2], rows[3]];
}

const evidence = update => database.prepare('SELECT * FROM reports WHERE bot_id = 123 AND update_id = ? AND subject_id = 0')
  .bind(update.update_id).first();

test('allow: a private username observed on a joined account can recover its ban without unsupported public lookup', async () => {
  const notice = { ...message(280, 11), new_chat_members: [{ ...message().from, username: 'known_member', first_name: '萝莉上门' }] };
  delete notice.text;
  telegram.send(notice);
  assert.equal((await dispatch({ update_id: 280, message: notice })).status, 200);
  assert.equal(telegram.canJoin(22), false);
  model.profiles.set(22, { first_name: 'Normal member', username: 'known_member' });
  const update = command('/allow @known_member', 281);
  assert.equal((await dispatch(update)).status, 200);
  assert.equal(telegram.canJoin(22), true);
  assert.equal(await database.prepare('SELECT user_id FROM blacklisted_users WHERE bot_id=123 AND user_id=22').first(), null);
  assert.deepEqual(telegram.getChatCalls, [], 'retained account identity must not depend on getChat accepting a private @username');
});

async function dispatchWithUnbans(update) {
  const fetch = telegram.fetch, unbans = [];
  telegram.fetch = async request => {
    if (new URL(request.url).pathname.endsWith('/unbanChatMember')) unbans.push(await request.clone().json());
    return fetch.call(telegram, request);
  };
  try { return { response: await dispatch(update), unbans }; }
  finally { telegram.fetch = fetch; }
}

test('allow: a reassigned private username cannot release the formerly observed account', async () => {
  const notice = { ...message(282, 11), new_chat_members: [{ ...message().from, username: 'known_member', first_name: '萝莉上门' }] };
  delete notice.text;
  telegram.send(notice);
  assert.equal((await dispatch({ update_id: 282, message: notice })).status, 200);
  model.profiles.set(22, { username: 'renamed_member' });
  assert.equal((await dispatch(command('/allow @known_member', 283))).status, 200);
  assert.equal(telegram.canJoin(22), false);
  assert.match(telegram.replies.at(-1).text, /ID/);
  assert.equal((await dispatch(command('/allow 22', 284))).status, 200);
  assert.equal(telegram.canJoin(22), true);
});

// These contracts run through authenticated Worker dispatch, D1 and the Telegram transport.
// The fake controls remote outcomes only; the Worker must own authorization and durable progress.
test('allow: numeric and username commands remove both lists for this bot and unban only the current supergroup', async () => {
  const cases = [
    { text: '/allow 22', anonymous: false, destination: chat.id },
    { text: '/allow@test_gate_bot @Ordinary', anonymous: false, destination: chat.id },
    { text: '/allow@test_gate_bot 22', anonymous: true, destination: -10099 },
    { text: '/allow @ordinary', anonymous: true, destination: chat.id },
  ];
  for (const [index, scenario] of cases.entries()) {
    await seed();
    telegram.members.set(11, { status: 'member' }); // Trust comes from REPORTER_IDS, not admin rights.
    telegram.groups.set(-10099, new Map([[22, { status: 'kicked', until_date: 0 }]]));
    const before = await blacklists();
    const update = command(scenario.text, 300 + index);
    update.message.chat = { ...chat, id: scenario.destination };
    if (scenario.anonymous) {
      update.message.sender_chat = chat;
      update.message.from = { id: 1087968824, is_bot: true, first_name: 'Group' };
    }
    const history = { ...message(200 + index), chat: update.message.chat };
    telegram.send(history);
    telegram.send(update.message);
    const lookups = telegram.getChatCalls.length;
    const { response, unbans } = await dispatchWithUnbans(update);
    assert.equal(response.status, 200);
    assert.ok(unbans.some(call => call.user_id === 22 && call.chat_id === scenario.destination), 'allow must perform the remote unban');
    for (const call of unbans) assert.equal(call.only_if_banned, true, 'Telegram unban must never evict a current member');
    assert.equal(telegram.canJoin(22, scenario.destination), true, 'allow must let the selected account rejoin');
    const otherChat = scenario.destination === chat.id ? -10099 : chat.id;
    assert.equal(telegram.canJoin(22, otherChat), false, 'another group must remain banned');
    assert.equal(telegram.has(history.message_id, scenario.destination), true, 'allow does not delete account history');
    assert.deepEqual(await blacklists(), withoutTarget(before), 'other accounts, other bots and stickers must remain');
    assert.equal(telegram.getChatCalls.length - lookups, scenario.text.includes('@Ordinary') || scenario.text.endsWith('@ordinary') ? 1 : 0,
      'a numeric account ID must not use username resolution');
    const saved = await evidence(update);
    assert.equal(saved?.command_source_id, 22, 'the resolved account must be pinned in command evidence');
    assert.equal(saved.response_status, 200);
    assert.deepEqual(JSON.parse(saved.raw_update), update);
  }
});

test('allow: unlisted owners, bot senders and forged sender-chat compatibility users cannot resolve or mutate accounts', async () => {
  await seed();
  telegram.members.set(44, { status: 'creator' });
  const before = await blacklists();
  const cases = [
    { from: message(1, 44).from },
    { from: { ...message(1, 11).from, is_bot: true } },
    { from: message(1, 11).from, sender_chat: { ...chat, id: -10099 } },
  ];
  for (const [index, sender] of cases.entries()) {
    const update = command('/allow @ordinary', 320 + index);
    Object.assign(update.message, sender);
    assert.equal((await dispatch(update)).status, 200);
    assert.deepEqual(await blacklists(), before);
    assert.equal(telegram.canJoin(22), false);
    assert.equal(await evidence(update), null);
  }
  assert.deepEqual(telegram.getChatCalls, []);
  assert.deepEqual(telegram.replies, []);
});

test('allow: invalid arguments, non-supergroups, edited commands and commands for another bot are inert', async () => {
  await seed();
  const before = await blacklists();
  const cases = [
    ...['', '0', '-22', '+22', '22.0', '22abc', '22 33', 'ordinary', '@', '4503599627370496']
      .map(argument => ({ text: `/allow ${argument}`, usage: true })),
    { text: '/allow 22', destination: { id: 11, type: 'private' } },
    { text: '/allow 22', destination: { id: -12, type: 'group' } },
    { text: '/allow 22', edited: true },
    { text: '/allow@other_bot @ordinary' },
    { text: '/allow 22', noEntity: true },
  ];
  for (const [index, scenario] of cases.entries()) {
    const update = command(scenario.text, 340 + index);
    if (scenario.destination) update.message.chat = scenario.destination;
    if (scenario.noEntity) delete update.message.entities;
    const delivered = scenario.edited ? { update_id: update.update_id, edited_message: update.message } : update;
    const replies = telegram.replies.length;
    assert.equal((await dispatch(delivered)).status, 200, scenario.text);
    assert.deepEqual(await blacklists(), before, scenario.text);
    assert.equal(telegram.canJoin(22), false, scenario.text);
    if (scenario.usage) {
      assert.equal(telegram.replies.length, replies + 1, 'recognized invalid syntax must give usage');
      assert.match(telegram.replies.at(-1).text, /\/allow.*username|\/allow.*(?:ID|id)/);
    }
  }
  assert.deepEqual(telegram.getChatCalls, []);
});

test('allow: restores a permanently restricted member without removing them or changing administrator privileges', async () => {
  for (const [index, member] of [
    { status: 'restricted', permissions: { can_send_messages: false, can_react_to_messages: false, can_edit_tag: false }, until_date: 0 },
    { status: 'member' },
    { status: 'administrator' },
    { status: 'creator' },
    { status: 'left' },
  ].entries()) {
    await seed();
    telegram.members.set(22, structuredClone(member));
    telegram.defaultPermissions.set(chat.id, { can_change_info: false, can_invite_users: false });
    telegram.groups.set(-10099, new Map([[22, { status: 'restricted', permissions: { can_send_messages: false }, until_date: 0 }]]));
    const before = await blacklists();
    const fetch = telegram.fetch, restores = [];
    telegram.fetch = async request => {
      if (new URL(request.url).pathname.endsWith('/restrictChatMember')) restores.push(await request.clone().json());
      return fetch.call(telegram, request);
    };
    try {
      const { response, unbans } = await dispatchWithUnbans(command('/allow 22', 370 + index));
      assert.equal(response.status, 200);
      assert.deepEqual(await blacklists(), withoutTarget(before));
      if (member.status === 'restricted') {
        assert.equal(telegram.members.get(22).status, 'member', 'all personal restrictions must be lifted');
        assert.equal(telegram.canSend(22), true);
        assert.equal(telegram.canSend(22, 'can_invite_users'), false, 'group defaults still apply after personal restoration');
        assert.equal(restores.length, 1);
        assert.equal(restores[0].until_date, 0);
        assert.equal(restores[0].use_independent_chat_permissions, true);
        assert.equal(restores[0].permissions.can_react_to_messages, true);
        assert.equal(restores[0].permissions.can_edit_tag, true);
        assert.ok(Object.values(restores[0].permissions).every(value => value === true));
      } else {
        assert.deepEqual(restores, [], 'ordinary members and privileged accounts need no permission operation');
        assert.deepEqual(telegram.members.get(22), member);
      }
      assert.deepEqual(unbans, [], 'non-banned accounts must not be unbanned');
      assert.equal(telegram.canSend(22, 'can_send_messages', -10099), false, 'another group remains restricted');
      assert.deepEqual(telegram.defaultPermissions.get(chat.id), { can_change_info: false, can_invite_users: false });
    } finally { telegram.fetch = fetch; }
  }
});

test('allow: rejected lookups and failed unbans stay retryable with blacklist rows retained until confirmation', async () => {
  const cases = [
    { method: 'getChat', text: '/allow @ordinary', response: { ok: false, error_code: 429 }, status: 429 },
    { method: 'getChat', text: '/allow @ordinary', response: { ok: true, result: { id: 22, type: 'private', username: 'someone_else' } }, status: 200 },
    { method: 'unbanChatMember:22', text: '/allow 22', response: { ok: false, error_code: 429 }, status: 429 },
    { method: 'unbanChatMember:22', text: '/allow 22', response: { ok: false, error_code: 500 }, status: 500 },
    { method: 'unbanChatMember:22', text: '/allow 22', response: { ok: true, result: false }, status: 200 },
  ];
  for (const [index, scenario] of cases.entries()) {
    await seed();
    const before = await blacklists();
    const update = command(scenario.text, 390 + index);
    let calls = 0, atUnban;
    telegram.faults.set(scenario.method, async () => {
      calls += 1;
      if (scenario.method.startsWith('unban')) atUnban = { saved: await evidence(update), lists: await blacklists() };
      return Response.json(scenario.response, { status: scenario.status });
    });
    assert.equal((await dispatch(update)).status, 503, 'the failed operation must request redelivery');
    assert.equal(calls, 1);
    if (scenario.method.startsWith('unban')) {
      assert.equal(atUnban.saved?.command_source_id, 22, 'pin and requested fence must exist before Telegram unban');
      assert.equal(atUnban.saved.response_status, null, 'allow must not finish before unban confirmation');
      assert.deepEqual(atUnban.lists, before, 'neither blacklist can be removed before unban confirmation');
    }
    assert.equal(telegram.canJoin(22), false);
    assert.deepEqual(await blacklists(), before);
    telegram.faults.clear();
    assert.equal((await dispatch(update)).status, 200);
    if (scenario.method.startsWith('unban') && scenario.status === 500) {
      assert.equal(telegram.canJoin(22), false, 'an unknown remote outcome must not be retried over a later ban');
      assert.deepEqual(await blacklists(), before);
      assert.match(telegram.replies.at(-1).text, /not completed|uncertain|check membership/i);
      assert.equal((await evidence(update)).moderation_result, 'access recovering');
    } else {
      assert.equal(telegram.canJoin(22), true);
      assert.deepEqual(await blacklists(), withoutTarget(before));
    }
  }
});

test('allow: failed or malformed membership checks and unmute confirmations retain both lists until retry succeeds', async () => {
  const cases = [
    { method: 'getChatMember:22', response: { ok: false, error_code: 429 }, status: 429 },
    { method: 'getChatMember:22', response: { ok: false, error_code: 400 }, status: 400 },
    { method: 'getChatMember:22', response: { ok: true, result: { status: 'restricted', user: { id: 33 } } }, status: 200 },
    { method: 'getChatMember:22', response: { ok: true, result: { status: 'unknown', user: { id: 22 } } }, status: 200 },
    { method: 'restrictChatMember:22', response: { ok: false, error_code: 429 }, status: 429 },
    { method: 'restrictChatMember:22', response: { ok: false, error_code: 400 }, status: 400 },
    { method: 'restrictChatMember:22', response: { ok: true, result: false }, status: 200 },
    { method: 'restrictChatMember:22', response: { ok: true, result: 1 }, status: 200 },
  ];
  for (const [index, scenario] of cases.entries()) {
    await seed();
    telegram.members.set(22, { status: 'restricted', permissions: { can_send_messages: false }, until_date: 0 });
    const before = await blacklists(), update = command('/allow 22', 800 + index);
    telegram.faults.set(scenario.method, async () => {
      assert.deepEqual(await blacklists(), before, 'blacklists remain before remote confirmation');
      assert.equal((await evidence(update))?.response_status, null);
      return Response.json(scenario.response, { status: scenario.status });
    });
    assert.equal((await dispatch(update)).status, 503);
    assert.equal(telegram.canSend(22), false);
    assert.deepEqual(await blacklists(), before);
    telegram.faults.clear();
    assert.equal((await dispatch(update)).status, 200);
    if (scenario.method.startsWith('restrict') && scenario.response.result === 1) {
      assert.equal(telegram.members.get(22).status, 'restricted');
      assert.deepEqual(await blacklists(), before);
      assert.match(telegram.replies.at(-1).text, /not completed|uncertain|check membership/i);
      assert.equal((await evidence(update)).moderation_result, 'access recovering');
    } else {
      assert.equal(telegram.members.get(22).status, 'member');
      assert.deepEqual(await blacklists(), withoutTarget(before));
    }
  }
});

test('allow: blacklist deletions are atomic and a storage retry after confirmed unban preserves a rejoined member', async () => {
  const cases = [
    { table: 'blacklisted_sources', condition: 'OLD.source_id = 22' },
    { table: 'blacklisted_users', condition: 'OLD.bot_id = 123 AND OLD.user_id = 22' },
  ];
  for (const [index, scenario] of cases.entries()) {
    await seed();
    const before = await blacklists();
    const update = command('/allow 22', 410 + index);
    await database.prepare(`CREATE TRIGGER allow_reject_list_delete BEFORE DELETE ON ${scenario.table}
      WHEN ${scenario.condition} BEGIN SELECT RAISE(ABORT, 'blacklist storage unavailable'); END`).run();
    try {
      assert.equal((await dispatch(update)).status, 503);
      assert.equal(telegram.canJoin(22), true, 'unban must be confirmed before atomic list removal');
      assert.deepEqual(await blacklists(), before, 'failed deletion of either list must roll back both');
      assert.equal((await evidence(update))?.response_status, null);
    } finally {
      await database.prepare('DROP TRIGGER allow_reject_list_delete').run();
    }
    telegram.members.set(22, { status: 'member' });
    assert.equal((await dispatch(update)).status, 200);
    assert.deepEqual(telegram.members.get(22), { status: 'member' });
    assert.deepEqual(await blacklists(), withoutTarget(before));
  }
});

test('allow: retry after confirmed unmute and failed list cleanup cannot undo a fresh mute', async () => {
  await seed();
  telegram.members.set(22, { status: 'restricted', permissions: { can_send_messages: false }, until_date: 0 });
  const before = await blacklists(), update = command('/allow 22', 820);
  await database.prepare(`CREATE TRIGGER reject_unmute_cleanup BEFORE DELETE ON blacklisted_sources
    WHEN OLD.source_id=22 BEGIN SELECT RAISE(ABORT, 'blacklist unavailable'); END`).run();
  try {
    assert.equal((await dispatch(update)).status, 503);
    assert.equal(telegram.members.get(22).status, 'member');
    assert.equal((await evidence(update))?.moderation_result, 'unbanned', 'existing recovery checkpoint stays compatible');
    assert.deepEqual(await blacklists(), before);
  } finally { await database.prepare('DROP TRIGGER reject_unmute_cleanup').run(); }
  const freshMute = { status: 'restricted', permissions: { can_send_messages: false, can_send_polls: false }, until_date: 0 };
  telegram.members.set(22, structuredClone(freshMute));
  let repeatedRestores = 0;
  telegram.faults.set('restrictChatMember:22', () => { repeatedRestores += 1; return Response.json({ ok: true, result: true }); });
  assert.equal((await dispatch(update)).status, 200);
  assert.equal(repeatedRestores, 0, 'confirmed membership recovery is not repeated on a D1 cleanup retry');
  assert.deepEqual(telegram.members.get(22), freshMute);
  assert.deepEqual(await blacklists(), withoutTarget(before));
});

test('allow: old rejected local mute is fenced, new spam can mute and completed allow cannot undo it', async () => {
  const older = { update_id: 830, message: { ...message(830), text: '@campaign_bot campaign_1' } };
  telegram.send(older.message);
  telegram.faults.set('restrictChatMember:22', () => Response.json({ ok: false, error_code: 429 }, { status: 429 }));
  assert.equal((await dispatch(older)).status, 503);
  assert.equal(telegram.has(830), false);
  assert.equal(telegram.canSend(22), true);
  telegram.faults.clear();
  const update = command('/allow 22', 831);
  assert.equal((await dispatch(update)).status, 200);
  assert.equal((await dispatch(older)).status, 200);
  assert.equal(telegram.canSend(22), true, 'allow fences the rejected old mute even after its claim was released');
  const fresh = { update_id: 832, message: { ...message(832), text: '@campaign_bot campaign_2' } };
  const history = message(829);
  telegram.send(history);
  telegram.send(fresh.message);
  assert.equal((await dispatch(fresh)).status, 200);
  assert.equal(telegram.canSend(22), false, 'new advertising is not permanently exempt');
  assert.equal(telegram.has(history.message_id), true, 'only the new triggering message is removed');
  assert.equal((await dispatch(update)).status, 200);
  assert.equal(telegram.canSend(22), false, 'completed allow redelivery must not undo a new mute');
});

for (const restriction of ['automatic', 'manual']) {
  test(`allow: concurrent redelivery cannot issue a stale second restoration after a fresh ${restriction} mute`, async () => {
    telegram.members.set(22, { status: 'restricted', permissions: { can_send_messages: false }, until_date: 0 });
    const update = command('/allow 22', 850);
    const entered = Promise.withResolvers(), release = Promise.withResolvers();
    let checks = 0;
    telegram.faults.set('getChatMember:22', async () => {
      checks += 1;
      if (checks === 2) { entered.resolve(); await release.promise; }
      return Response.json({ ok: true, result: { status: 'restricted', user: message().from } });
    });
    const first = dispatch(update), second = dispatch(update);
    await Promise.race([entered.promise, Promise.all([first, second])]);
    telegram.faults.clear();
    try {
      // Either delivery may reach the first membership check.
      assert.equal((await Promise.race([first, second])).status, 200);
      if (restriction === 'automatic') {
        const fresh = { update_id: 851, message: { ...message(851), text: '@campaign_bot campaign_9' } };
        telegram.send(fresh.message);
        assert.equal((await dispatch(fresh)).status, 200);
      } else {
        telegram.members.set(22, { status: 'restricted', permissions: { can_send_messages: false }, until_date: 0 });
      }
      assert.equal(telegram.canSend(22), false);
    } finally {
      release.resolve();
      await Promise.all([first, second]);
      telegram.faults.clear();
    }
    assert.equal(telegram.canSend(22), false, 'a stale concurrent command must not lift a fresh restriction');
  });
}

test('allow: an incomplete old recovery cannot supersede a newer confirmed local mute', async () => {
  const update = command('/allow 22', 860);
  telegram.members.set(22, { status: 'restricted', permissions: { can_send_messages: false }, until_date: 0 });
  telegram.faults.set('getChatMember:22', () => Response.json({ ok: false, error_code: 429 }, { status: 429 }));
  assert.equal((await dispatch(update)).status, 503);
  telegram.faults.clear();
  const fresh = { update_id: 861, message: { ...message(861), text: '@campaign_bot campaign_9' } };
  telegram.send(fresh.message);
  assert.equal((await dispatch(fresh)).status, 200);
  assert.equal(telegram.canSend(22), false);
  assert.equal((await dispatch(update)).status, 200);
  assert.equal(telegram.canSend(22), false);
  assert.match(telegram.replies.at(-1).text, /not completed|superseded|check membership/i);
});

test('allow: a lost recovery checkpoint does not repeat restoration over a fresh manual restriction', async () => {
  const update = command('/allow 22', 870);
  telegram.members.set(22, { status: 'restricted', permissions: { can_send_messages: false }, until_date: 0 });
  await database.prepare("CREATE TRIGGER reject_access_checkpoint BEFORE UPDATE OF moderation_result ON reports WHEN NEW.moderation_result='unbanned' BEGIN SELECT RAISE(ABORT,'checkpoint unavailable'); END").run();
  try { assert.equal((await dispatch(update)).status, 503); }
  finally { await database.prepare('DROP TRIGGER reject_access_checkpoint').run(); }
  assert.equal(telegram.canSend(22), true);
  const fresh = { status: 'restricted', permissions: { can_send_messages: false, can_send_polls: false }, until_date: 0 };
  telegram.members.set(22, structuredClone(fresh));
  assert.equal((await dispatch(update)).status, 200);
  assert.deepEqual(telegram.members.get(22), fresh);
  assert.match(telegram.replies.at(-1).text, /not completed|uncertain|check membership/i);
});

test('allow: an older separate recovery cannot restore over a newer completed recovery and manual restriction', async () => {
  const entered = Promise.withResolvers(), release = Promise.withResolvers();
  telegram.members.set(22, { status: 'restricted', permissions: { can_send_messages: false }, until_date: 0 });
  telegram.faults.set('getChatMember:22', async () => {
    entered.resolve(); await release.promise;
    return Response.json({ ok: true, result: { status: 'restricted', user: message().from } });
  });
  const older = dispatch(command('/allow 22', 880));
  await entered.promise;
  telegram.faults.clear();
  assert.equal((await dispatch(command('/allow 22', 881))).status, 200);
  const fresh = { status: 'restricted', permissions: { can_send_messages: false, can_send_polls: false }, until_date: 0 };
  telegram.members.set(22, structuredClone(fresh));
  release.resolve();
  assert.equal((await older).status, 200);
  assert.deepEqual(telegram.members.get(22), fresh);
  assert.match(telegram.replies.at(-1).text, /not completed|superseded|check membership/i);
});

test('allow: an older in-flight mute requests manual inspection without undoing a fresh mute', async () => {
  const entered = Promise.withResolvers(), release = Promise.withResolvers();
  const older = { update_id: 840, message: { ...message(840), text: '@campaign_bot campaign_1' } };
  telegram.send(older.message);
  telegram.faults.set('restrictChatMember:22', async () => {
    entered.resolve();
    await release.promise;
    telegram.members.set(22, { status: 'restricted', permissions: { can_send_messages: false }, until_date: 0 });
    return Response.json({ ok: true, result: true });
  });
  const pending = dispatch(older);
  await Promise.race([entered.promise, pending.then(() => { throw new Error('old mute was never issued'); })]);
  let response;
  try {
    telegram.faults.clear();
    assert.equal((await dispatch(command('/allow 22', 841))).status, 200);
    const fresh = { update_id: 842, message: { ...message(842), text: '@campaign_bot campaign_2' } };
    telegram.send(fresh.message);
    assert.equal((await dispatch(fresh)).status, 200);
    assert.equal(telegram.canSend(22), false);
  } finally {
    release.resolve();
    response = await pending;
    telegram.faults.clear();
  }
  assert.equal(response.status, 200);
  assert.match(await response.text(), /late mute.*check membership/i, 'late success cannot be reported as settled recovery');
  assert.equal(telegram.canSend(22), false, 'automatic compensation must not undo a fresh mute');
});

test('allow: retry rechecks reporter authorization and remains pinned when the username changes ownership', async () => {
  await seed();
  const before = await blacklists();
  const update = command('/allow @ordinary', 430);
  telegram.faults.set('unbanChatMember:22', () => Response.json({ ok: false, error_code: 429 }, { status: 429 }));
  assert.equal((await dispatch(update)).status, 503);
  const saved = await evidence(update);
  assert.equal(saved?.command_source_id, 22);
  telegram.faults.clear();
  telegram.accounts.set('@ordinary', { id: 33, type: 'private', username: 'ordinary' });
  await setBindings({ REPORTER_IDS: '' });
  assert.equal((await dispatch(update)).status, 200);
  assert.deepEqual(await evidence(update), saved, 'revocation must not advance command progress');
  assert.deepEqual(await blacklists(), before);
  assert.equal(telegram.canJoin(22), false);
  await setBindings({ REPORTER_IDS: '11,-10012' });
  assert.equal((await dispatch(update)).status, 200);
  assert.equal(telegram.canJoin(22), true);
  assert.equal(telegram.canJoin(33), false);
  assert.deepEqual(await blacklists(), withoutTarget(before));
  assert.deepEqual(telegram.getChatCalls, ['@ordinary'], 'redelivery must not resolve a reassigned username again');
  const completed = await evidence(update);
  assert.equal(completed.command_source_id, 22);
  assert.equal(completed.raw_update, saved.raw_update);
});

test('allow: completion precedes acknowledgement and redelivery cannot undo a later new report ban', async () => {
  await seed();
  const update = command('/allow 22', 450);
  telegram.faults.set('sendMessage', () => Response.json({ ok: false, error_code: 429 }, { status: 429 }));
  assert.equal((await dispatch(update)).status, 503, 'acknowledgement failure may request retry');
  assert.equal(telegram.canJoin(22), true);
  assert.equal((await evidence(update))?.response_status, 200, 'membership changes must be completed before acknowledgement');
  telegram.faults.clear();
  telegram.members.set(22, { status: 'member' });
  const later = report(message(460));
  later.update_id = 461;
  later.message.message_id = 461;
  telegram.send(later.message.reply_to_message);
  telegram.send(later.message);
  assert.equal((await dispatch(later)).status, 200);
  assert.equal(telegram.canJoin(22), false, 'allow is not a permanent exemption from a new report');
  const banned = await blacklists();
  assert.equal((await dispatch(update)).status, 200);
  assert.equal(telegram.canJoin(22), false, 'a completed older allow must not lift the new ban');
  assert.deepEqual(await blacklists(), banned);
});

test('allow: redelivery of an older bs command cannot register the allowed source again', async () => {
  telegram.accounts.set('@ordinary', { id: 22, type: 'private', username: 'ordinary' });
  const older = command('/bs @ordinary', 470);
  assert.equal((await dispatch(older)).status, 200);
  assert.deepEqual(await database.prepare('SELECT source_id FROM blacklisted_sources WHERE source_id = 22').first(), { source_id: 22 });
  telegram.members.set(22, { status: 'kicked' });
  assert.equal((await dispatch(command('/allow 22', 471))).status, 200);
  assert.equal(await database.prepare('SELECT source_id FROM blacklisted_sources WHERE source_id = 22').first(), null);
  assert.equal((await dispatch(older)).status, 200);
  assert.equal(await database.prepare('SELECT source_id FROM blacklisted_sources WHERE source_id = 22').first(), null,
    'old source registration must remain fenced after allow');
  assert.equal(telegram.canJoin(22), true);
});

test('allow: a stale Action 2 cleanup retry cannot re-add a blacklist or ban the allowed member', async () => {
  const history = message(480);
  telegram.send(history);
  assert.equal((await dispatch({ update_id: 480, message: history })).status, 200);
  const older = report(message(481));
  older.update_id = 482;
  older.message.message_id = 482;
  telegram.send(older.message.reply_to_message);
  telegram.send(older.message);
  telegram.faults.set('deleteMessages', () => Response.json({ ok: false, error_code: 429 }, { status: 429 }));
  assert.equal((await dispatch(older)).status, 503);
  assert.equal(telegram.canJoin(22), false);
  assert.equal(telegram.has(480), true);
  telegram.faults.clear();
  assert.equal((await dispatch(command('/allow 22', 483))).status, 200);
  assert.equal(telegram.canJoin(22), true);
  telegram.members.set(22, { status: 'member' });
  const before = await blacklists();
  assert.equal((await dispatch(older)).status, 200);
  assert.equal(telegram.canJoin(22), true);
  assert.deepEqual(await blacklists(), before, 'an old confirmed-ban checkpoint must not reinsert the allowed account');
});

test('allow: an older in-flight ban is compensated after allow and cannot restore either blacklist', async () => {
  const entered = Promise.withResolvers(), release = Promise.withResolvers();
  const older = report();
  telegram.send(older.message.reply_to_message);
  telegram.send(older.message);
  telegram.faults.set('banChatMember:22', async () => {
    entered.resolve();
    await release.promise;
    telegram.members.set(22, { status: 'kicked', until_date: 0 });
    return Response.json({ ok: true, result: true });
  });
  const pending = dispatch(older);
  await Promise.race([entered.promise, pending.then(() => { throw new Error('scenario never reached the older ban'); })]);
  try {
    await database.prepare('INSERT INTO blacklisted_sources (source_id) VALUES (22)').run();
    const update = command('/allow 22', 500);
    assert.equal((await dispatch(update)).status, 200);
    assert.equal((await evidence(update))?.command_source_id, 22, 'allow must publish a durable fence against the pending ban');
    assert.equal(await database.prepare('SELECT source_id FROM blacklisted_sources WHERE source_id = 22').first(), null);
  } finally {
    release.resolve();
    await pending;
  }
  telegram.faults.clear();
  assert.equal(telegram.canJoin(22), true, 'the late older ban must be compensated with a safe unban');
  assert.equal(await database.prepare('SELECT user_id FROM blacklisted_users WHERE bot_id = 123 AND user_id = 22').first(), null);
  assert.equal(await database.prepare('SELECT source_id FROM blacklisted_sources WHERE source_id = 22').first(), null);
  assert.equal((await dispatch(older)).status, 200);
  assert.equal(telegram.canJoin(22), true);
});

test('allow: a newer report arriving during unban retains its blacklist and requires truthful manual inspection', async () => {
  await seed();
  const entered = Promise.withResolvers(), release = Promise.withResolvers();
  telegram.faults.set('unbanChatMember:22', async () => { entered.resolve(); await release.promise; return Response.json({ ok: true, result: true }); });
  const update = command('/allow 22', 610);
  telegram.send(update.message);
  const pending = dispatch(update);
  await entered.promise;
  const later = report(message(611));
  later.update_id = 612;
  later.message.message_id = 612;
  telegram.send(later.message.reply_to_message);
  telegram.send(later.message);
  assert.equal((await dispatch(later)).status, 200);
  release.resolve();
  assert.equal((await pending).status, 200);
  assert.equal(telegram.canJoin(22), false);
  assert.deepEqual(await database.prepare('SELECT user_id FROM blacklisted_users WHERE bot_id=123 AND user_id=22').first(), { user_id: 22 });
  assert.match(telegram.replies.at(-1).text, /superseded|not completed|check membership/i);
  assert.equal(telegram.has(update.message.message_id), true, 'an uncertain recovery command must remain');
});

test('allow: a newer automatic blacklist ban targets the replying sender rather than the referenced author', async () => {
  await seed();
  const update = command('/allow 22', 620);
  await database.prepare("CREATE TRIGGER block_allow_delete BEFORE DELETE ON blacklisted_sources WHEN OLD.source_id=22 BEGIN SELECT RAISE(ABORT,'unavailable'); END").run();
  try { assert.equal((await dispatch(update)).status, 503); }
  finally { await database.prepare('DROP TRIGGER block_allow_delete').run(); }
  const later = { ...message(621), reply_to_message: message(619, 33) };
  telegram.send(later);
  assert.equal((await dispatch({ update_id: 622, message: later })).status, 200);
  assert.equal(telegram.canJoin(22), false);
  assert.equal((await dispatch(update)).status, 200);
  assert.deepEqual(await database.prepare('SELECT user_id FROM blacklisted_users WHERE bot_id=123 AND user_id=22').first(), { user_id: 22 });
  assert.match(telegram.replies.at(-1).text, /superseded|not completed|check membership/i);
});

test('allow: late ban compensation never lifts membership bans in another supergroup', async () => {
  await seed();
  telegram.groups.set(-10099, new Map([[11, { status: 'administrator' }], [22, { status: 'member' }]]));
  const entered = Promise.withResolvers(), release = Promise.withResolvers();
  telegram.faults.set('banChatMember:22', async () => { entered.resolve(); await release.promise;
    telegram.groups.get(-10099).set(22, { status: 'kicked', until_date: 0 }); return Response.json({ ok: true, result: true }); });
  const older = report();
  older.update_id = 630;
  older.message.chat = { ...chat, id: -10099 };
  older.message.reply_to_message.chat = older.message.chat;
  telegram.send(older.message.reply_to_message);
  telegram.send(older.message);
  const pending = dispatch(older);
  await entered.promise;
  assert.equal((await dispatch(command('/allow 22', 631))).status, 200);
  release.resolve();
  assert.equal((await pending).status, 200);
  assert.equal(telegram.canJoin(22), true);
  assert.equal(telegram.canJoin(22, -10099), false, 'allow only changes its invoking group');
});

test('allow: ambiguous retained usernames require numeric identity without choosing a cached account', async () => {
  await seed();
  await database.prepare("UPDATE blacklisted_users SET username='ambiguous' WHERE bot_id=123 AND user_id IN (22,33)").run();
  telegram.accounts.set('@ambiguous', { id: 22, type: 'private', username: 'ambiguous' });
  const before = await blacklists();
  assert.equal((await dispatch(command('/allow @ambiguous', 650))).status, 200);
  assert.equal(telegram.canJoin(22), false);
  assert.equal(telegram.canJoin(33), false);
  assert.deepEqual(await blacklists(), before);
  assert.deepEqual(telegram.getChatCalls, []);
  assert.match(telegram.replies.at(-1).text, /ID/);
});

test('allow: an unfinished join decision cannot ban an allowed account when the provider later recovers', async () => {
  await setBindings({ EXPERIENTIAL_API_KEY: 'test-model-key' });
  model.response = new Response('busy', { status: 503 });
  const notice = { ...message(81, 11), new_chat_members: [message().from] };
  delete notice.text;
  telegram.send(notice);
  const older = { update_id: 700, message: notice };
  assert.equal((await dispatch(older)).status, 200);
  const pending = await task();
  assert.equal(pending.phase, 'classify');
  assert.equal((await dispatch(command('/allow 22', 701))).status, 200);
  assert.equal((await task()).phase, 'done');
  assert.equal((await task()).input_json, null);
  model.response = null;
  model.probability = 0.96;
  await tick(pending.due_at);
  assert.equal((await dispatch(older)).status, 200);
  assert.equal(telegram.canJoin(22), true);
  assert.equal(telegram.has(81), true);
  const fresh = { update_id: 702, message: { ...notice, message_id: 703 } };
  telegram.send(fresh.message);
  assert.equal((await dispatch(fresh)).status, 200);
  assert.equal(telegram.canJoin(22), false, 'a later join is not permanently exempt');
});

test('allow: unavailable durable command evidence prevents unban and all blacklist mutations', async () => {
  await seed();
  const before = await blacklists();
  let unbans = 0;
  telegram.faults.set('unbanChatMember:22', () => {
    unbans += 1;
    return Response.json({ ok: true, result: true });
  });
  await database.prepare(`CREATE TRIGGER allow_reject_evidence BEFORE INSERT ON reports
    BEGIN SELECT RAISE(ABORT, 'command evidence unavailable'); END`).run();
  try {
    assert.equal((await dispatch(command('/allow 22', 520))).status, 503);
    assert.equal(unbans, 0, 'membership cannot change without a durable requested fence');
    assert.equal(telegram.canJoin(22), false);
    assert.deepEqual(await blacklists(), before);
    assert.deepEqual(telegram.replies, []);
  } finally {
    await database.prepare('DROP TRIGGER allow_reject_evidence').run();
  }
  telegram.faults.clear();
  assert.equal((await dispatch(command('/allow 22', 520))).status, 200);
  assert.equal(telegram.canJoin(22), true);
  assert.deepEqual(await blacklists(), withoutTarget(before));
});
