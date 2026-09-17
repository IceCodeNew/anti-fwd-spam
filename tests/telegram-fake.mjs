import assert from 'node:assert/strict';

export const chat = { id: -10012, type: 'supergroup', title: 'Test discussion' };
export const token = '123:test-token';
export const username = 'niuqu_icn_bot';

export function message(id = 81, sender = 22) {
  return {
    message_id: id, chat: { ...chat }, date: 1_789_500_000,
    from: { id: sender, is_bot: false, first_name: `User ${sender}` }, text: 'message',
  };
}

export function report(target = message()) {
  return {
    update_id: 71,
    message: {
      ...message(82, 11), reply_to_message: target, text: '😀 @niuqu_icn_bot',
      entities: [{ type: 'mention', offset: 3, length: 14 }],
    },
  };
}

// Contract: https://core.telegram.org/bots/api#available-methods
// No production constants are imported: this fake models Telegram's remote state.
export class Telegram {
  messages = new Map();
  members = new Map();
  faults = new Map();
  violations = [];

  reset() {
    this.messages.clear();
    this.members = new Map([[11, { status: 'administrator' }], [22, { status: 'member' }]]);
    this.faults.clear();
    this.violations = [];
  }

  send(message) {
    this.messages.set(`${message.chat.id}:${message.message_id}`, structuredClone(message));
  }

  has(id) { return this.messages.has(`${chat.id}:${id}`); }
  canJoin(id) { return this.members.get(id).status !== 'kicked'; }
  canSend(id, permission = 'can_send_messages') {
    const member = this.members.get(id);
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
      assert.ok(['deleteMessage', 'getChatMember', 'restrictChatMember', 'banChatMember'].includes(method));
      const params = await request.json();
      assert.ok(Number.isSafeInteger(params.chat_id));
      assert.ok(Number.isSafeInteger(params.message_id ?? params.user_id));
      const fault = this.faults.get(`${method}:${params.message_id ?? params.user_id}`) ?? this.faults.get(method);
      if (fault) return fault();

      if (method === 'deleteMessage') {
        const found = this.messages.delete(`${params.chat_id}:${params.message_id}`);
        return found ? Response.json({ ok: true, result: true }) : Response.json({
          ok: false, error_code: 400, description: 'Bad Request: message to delete not found',
        }, { status: 400 });
      }
      const member = this.members.get(params.user_id);
      assert.ok(member, 'The scenario must supply the Telegram member');
      if (method === 'getChatMember') {
        return Response.json({ ok: true, result: { ...member, user: message(1, params.user_id).from } });
      }
      if (['creator', 'administrator'].includes(member.status)) {
        return Response.json({ ok: false, error_code: 400, description: 'Bad Request: user is an administrator' }, { status: 400 });
      }
      assert.equal(params.chat_id, chat.id);
      // The scenarios use permanent restrictions; zero is Telegram's permanent date.
      assert.equal(params.until_date, 0);
      if (method === 'banChatMember') {
        assert.equal(typeof params.revoke_messages, 'boolean');
        this.members.set(params.user_id, { status: 'kicked', until_date: 0 });
        // Model the observed failure: a successful ban can leave messages visible.
        // Target deletion must succeed independently of Telegram's history cleanup.
      } else {
        assert.equal(params.use_independent_chat_permissions, true);
        assert.equal(typeof params.permissions, 'object');
        this.members.set(params.user_id, { status: 'restricted', permissions: params.permissions, until_date: 0 });
      }
      return Response.json({ ok: true, result: true });
    } catch (error) {
      this.violations.push(error.message);
      throw error;
    }
  }
}
