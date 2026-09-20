import assert from 'node:assert/strict';

export const chat = { id: -10012, type: 'supergroup', title: 'Test discussion' };
export const token = '123:test-token';
export const username = 'test_gate_bot';
const messageDate = Math.floor(Date.now() / 1000) - 60;

export function message(id = 81, sender = 22) {
  return {
    message_id: id, chat: { ...chat }, date: messageDate,
    from: { id: sender, is_bot: false, first_name: `User ${sender}` }, text: 'message',
  };
}

export function report(target = message()) {
  return {
    update_id: 71,
    message: {
      ...message(82, 11), reply_to_message: target, text: '😀 @test_gate_bot',
      entities: [{ type: 'mention', offset: 3, length: 14 }],
    },
  };
}

// Contract: https://core.telegram.org/bots/api#available-methods
// No production constants are imported: this fake models Telegram's remote state.
export class Telegram {
  messages = new Map();
  members = new Map();
  groups = new Map();
  faults = new Map();
  accounts = new Map();
  replies = [];
  violations = [];
  now = Math.floor(Date.now() / 1000);

  reset() {
    this.messages.clear();
    this.members = new Map([[11, { status: 'administrator' }], [22, { status: 'member' }]]);
    this.groups.clear();
    this.faults.clear();
    this.accounts.clear();
    this.replies = [];
    this.violations = [];
    this.now = Math.floor(Date.now() / 1000);
  }

  send(message) {
    this.messages.set(`${message.chat.id}:${message.message_id}`, structuredClone(message));
  }

  has(id, chatId = chat.id) { return this.messages.has(`${chatId}:${id}`); }
  membersIn(chatId) { return chatId === chat.id ? this.members : this.groups.get(chatId); }
  canJoin(id, chatId = chat.id) { return this.membersIn(chatId).get(id).status !== 'kicked'; }
  canSend(id, permission = 'can_send_messages', chatId = chat.id) {
    const member = this.membersIn(chatId).get(id);
    return member.status !== 'kicked' && member.permissions?.[permission] !== false;
  }

  async fetch(request) {
    try {
      const url = new URL(request.url);
      assert.equal(url.origin, 'https://api.telegram.org');
      assert.equal(request.method, 'POST');
      assert.equal(request.headers.get('content-type'), 'application/json');
      assert.ok(url.pathname.startsWith(`/bot${token}/`));
      const method = url.pathname.slice(`/bot${token}/`.length);
      assert.ok(['deleteMessage', 'deleteMessages', 'getChatMember', 'restrictChatMember', 'banChatMember', 'getChat', 'sendMessage'].includes(method));
      const params = await request.json();
      if (method === 'getChat') {
        assert.match(params.chat_id, /^@[A-Za-z0-9_]{1,32}$/);
        const fault = this.faults.get(method);
        if (fault) return fault();
        const account = this.accounts.get(params.chat_id.toLowerCase());
        return account ? Response.json({ ok: true, result: account })
          : Response.json({ ok: false, error_code: 400, description: 'Bad Request: chat not found' }, { status: 400 });
      }
      assert.ok(Number.isSafeInteger(params.chat_id));
      if (method === 'sendMessage') {
        assert.equal(typeof params.text, 'string');
        assert.ok(params.text.length > 0 && params.text.length <= 4096);
        assert.ok(Number.isSafeInteger(params.reply_parameters.message_id));
        const fault = this.faults.get(method);
        if (fault) return fault();
        const reply = { message_id: 1000 + this.replies.length, chat: { id: params.chat_id }, text: params.text,
          reply_to_message: { message_id: params.reply_parameters.message_id } };
        this.replies.push(reply);
        return Response.json({ ok: true, result: reply });
      }
      if (method === 'deleteMessages') {
        assert.ok(Array.isArray(params.message_ids));
        assert.ok(params.message_ids.length >= 1 && params.message_ids.length <= 100);
        assert.ok(params.message_ids.every(Number.isSafeInteger));
      } else {
        assert.ok(Number.isSafeInteger(params.message_id ?? params.user_id));
      }
      const fault = this.faults.get(`${method}:${params.message_id ?? params.user_id}`) ?? this.faults.get(method);
      if (fault) return fault();

      if (method === 'deleteMessage' || method === 'deleteMessages') {
        const ids = method === 'deleteMessages' ? params.message_ids : [params.message_id];
        const undeletable = ids.some(id => {
          const msg = this.messages.get(`${params.chat_id}:${id}`);
          return msg && (msg.date <= this.now - 48 * 3600 ||
            msg.forum_topic_created || msg.supergroup_chat_created || msg.channel_chat_created);
        });
        if (undeletable) return Response.json({
          ok: false, error_code: 400, description: "Bad Request: message can't be deleted",
        }, { status: 400 });
        if (method === 'deleteMessage' && !this.messages.has(`${params.chat_id}:${params.message_id}`)) {
          return Response.json({ ok: false, error_code: 400, description: 'Bad Request: message to delete not found' }, { status: 400 });
        }
        for (const id of ids) this.messages.delete(`${params.chat_id}:${id}`);
        return Response.json({ ok: true, result: true });
      }
      const members = this.membersIn(params.chat_id);
      const member = members.get(params.user_id);
      assert.ok(member, 'The scenario must supply the Telegram member');
      if (method === 'getChatMember') {
        return Response.json({ ok: true, result: { ...member, user: {
          ...message(1, params.user_id).from, is_bot: params.user_id === 123,
        } } });
      }
      if (['creator', 'administrator'].includes(member.status)) {
        return Response.json({ ok: false, error_code: 400, description: 'Bad Request: user is an administrator' }, { status: 400 });
      }
      // The scenarios use permanent restrictions; zero is Telegram's permanent date.
      assert.equal(params.until_date, 0);
      if (method === 'banChatMember') {
        if ('revoke_messages' in params) assert.equal(typeof params.revoke_messages, 'boolean');
        members.set(params.user_id, { status: 'kicked', until_date: 0 });
        // Model the observed failure: a successful ban can leave messages visible.
        // Target deletion must succeed independently of Telegram's history cleanup.
      } else {
        assert.equal(params.use_independent_chat_permissions, true);
        assert.equal(typeof params.permissions, 'object');
        members.set(params.user_id, { status: 'restricted', permissions: params.permissions, until_date: 0 });
      }
      return Response.json({ ok: true, result: true });
    } catch (error) {
      this.violations.push(error.message);
      throw error;
    }
  }
}
