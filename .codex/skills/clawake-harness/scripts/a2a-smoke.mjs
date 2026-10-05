// Run with podman exec -i clawcad-robotics-lead node --input-type=module < this file.
// Runtime verification only: no CAD/tool calls and no tokens in command arguments.
import {randomUUID} from 'node:crypto';
const marker = randomUUID();
const token = process.env.A2A_LEAD_TO_MECHANICAL;
if (!token) throw new Error('A2A token not configured');
const endpoint = 'http://clawcad-mechanical-engineer:18789/a2a/v1';
async function rpc(method, params) {
  const response = await fetch(endpoint, {
    method: 'POST', headers: {'Authorization': `Bearer ${token}`, 'Content-Type': 'application/json'},
    body: JSON.stringify({jsonrpc: '2.0', id: randomUUID(), method, params}),
    signal: AbortSignal.timeout(15000),
  });
  if (!response.ok) throw new Error(`HTTP ${response.status}`);
  const body = await response.json();
  if (body.error) throw new Error('A2A RPC rejected');
  return body.result?.task ?? body.result;
}
let task = await rpc('SendMessage', {
  configuration: {returnImmediately: true},
  message: {messageId: marker, role: 'ROLE_USER', parts: [{text:
    `Harness smoke test. task_id=${marker}. Return only task_id=${marker} HARNESS_OK. ` +
    'Perform no CAD, web, filesystem mutation or manufacturing action. Do not call any tools. ' +
    'Return your final answer in this original A2A task. Do not send a new message task.'}]},
});
if (!task?.id) throw new Error('No task ID returned');
const original = task.id;
for (let count = 0; count < 60; count++) {
  if (task.id !== original) throw new Error('Task ID changed');
  if (task.status?.state === 'TASK_STATE_COMPLETED') break;
  if (['TASK_STATE_FAILED', 'TASK_STATE_CANCELED', 'TASK_STATE_REJECTED'].includes(task.status?.state)) {
    throw new Error(`Task ${original} failed`);
  }
  await new Promise(resolve => setTimeout(resolve, 2000));
  task = await rpc('GetTask', {id: original});
}
const text = (task.artifacts ?? []).flatMap(a => a.parts ?? []).map(p => p.text ?? '').join('\n');
if (task.id !== original || task.status?.state !== 'TASK_STATE_COMPLETED' ||
    !text.includes(`task_id=${marker}`) || !text.includes('HARNESS_OK')) {
  throw new Error(`Task ${original}: final completion not verified`);
}
// Output only identifiers and the bounded success marker, never arbitrary agent text.
console.log(JSON.stringify({task_id: original, request_id: marker, result: 'HARNESS_OK'}));
