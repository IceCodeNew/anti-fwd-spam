import assert from 'node:assert/strict';
import { test } from 'node:test';
import { readFile } from 'node:fs/promises';
import { database } from './worker-runtime.mjs';

test('user: Given an in-flight message task before join support, When the task migration applies, Then its progress survives and another member can own the same notice', async () => {
  await database.prepare('DROP TABLE model_tasks').run();
  for (const name of ['0008_model_tasks.sql', '0012_model_task_revisions.sql']) {
    for (const sql of (await readFile(`migrations/${name}`, 'utf8')).split(';').filter(sql => sql.trim())) {
      await database.prepare(sql).run();
    }
  }
  const old = { bot_id: 123, chat_id: -10012, message_id: 81, phase: 'delete', input_json: '{"target":{}}',
    attempts: 2, generation: 8, due_at: 100, lease_until: 200, stop_at: 300,
    created_at: 0, expires_at: 500, revision: 7, edit_pending: 1 };
  await database.prepare('INSERT INTO model_tasks (' + Object.keys(old).join(',') + ') VALUES ('
    + Object.keys(old).map(() => '?').join(',') + ')').bind(...Object.values(old)).run();
  for (const sql of (await readFile('migrations/0015_join_tasks.sql', 'utf8')).split(';').filter(sql => sql.trim())) {
    await database.prepare(sql).run();
  }
  assert.deepEqual(await database.prepare('SELECT * FROM model_tasks WHERE subject_id=0').first(), { ...old, subject_id: 0 });
  await database.prepare("INSERT INTO model_tasks (bot_id,chat_id,message_id,subject_id,phase,due_at,stop_at,created_at,expires_at) VALUES(123,-10012,81,22,'classify',0,300,0,500)").run();
  assert.equal((await database.prepare('SELECT COUNT(*) AS n FROM model_tasks').first()).n, 2);
  const indexes = (await database.prepare("SELECT name FROM sqlite_master WHERE type='index' AND tbl_name='model_tasks'").all()).results;
  assert.ok(indexes.some(row => row.name === 'model_tasks_due'));
  assert.ok(indexes.some(row => row.name === 'model_tasks_expiry'));
});
