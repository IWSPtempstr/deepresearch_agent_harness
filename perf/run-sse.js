import http from 'k6/http';
import { check, sleep } from 'k6';
import { Counter, Trend } from 'k6/metrics';

// P2-10 压测场景：N 并发提交研究 Run + SSE 订阅至终态。
// 用法：k6 run -e BASE_URL=http://127.0.0.1:8000 -e VUS=20 -e DURATION=3m perf/run-sse.js
// 演示拓扑建议：docker compose up --scale backend=2 --scale worker=4

const BASE_URL = __ENV.BASE_URL || 'http://127.0.0.1:8000';
const VUS = Number(__ENV.VUS || 10);
const DURATION = __ENV.DURATION || '2m';

const RUN_ACCEPTED = new Counter('dra_runs_accepted');
const RUN_COMPLETED = new Counter('dra_runs_completed');
const RUN_FAILED = new Counter('dra_runs_failed');
const SSE_FIRST_EVENT = new Trend('dra_sse_first_event_ms', true);
const RUN_E2E = new Trend('dra_run_e2e_ms', true);

export const options = {
  scenarios: {
    submit_and_stream: {
      executor: 'ramping-vus',
      startVUs: 1,
      stages: [
        { duration: '30s', target: VUS },
        { duration: DURATION, target: VUS },
        { duration: '30s', target: 0 },
      ],
      gracefulRampDown: '30s',
    },
  },
  thresholds: {
    // 评测门式的容量断言；低于即压测失败（README 记录结论）
    'http_req_failed{scenario:submit_and_stream}': ['rate<0.01'],
    'dra_run_e2e_ms': ['p(99)<120000'],
  },
};

function submitRun() {
  const session = http.post(
    `${BASE_URL}/api/v1/sessions`,
    JSON.stringify({ title: `k6-${__VU}-${__ITER}` }),
    { headers: { 'Content-Type': 'application/json' } },
  );
  check(session, { 'session created': (r) => r.status === 201 });
  const sessionId = session.json('session_id');

  const accepted = http.post(
    `${BASE_URL}/api/v1/sessions/${sessionId}/messages`,
    JSON.stringify({
      client_message_id: `k6-${__VU}-${__ITER}-${Date.now()}`,
      content: `压测问题 ${__ITER}：请给出简要研究结论`,
      source_mode: 'graphrag',
      workflow_mode: 'deep_research',
      report_type: 'brief',
    }),
    { headers: { 'Content-Type': 'application/json' } },
  );
  const ok = check(accepted, { 'run accepted': (r) => r.status === 202 });
  if (ok) RUN_ACCEPTED.add(1);
  return accepted.json('run_id');
}

function streamUntilTerminal(runId) {
  const started = Date.now();
  let firstEventAt = null;
  const res = http.get(`${BASE_URL}/api/v1/runs/${runId}/events`, {
    headers: { Accept: 'text/event-stream' },
    timeout: '180s',
  });
  const lines = res.body ? res.body.split('\n') : [];
  for (const line of lines) {
    if (line.startsWith('event:')) {
      if (firstEventAt === null) {
        firstEventAt = Date.now() - started;
        SSE_FIRST_EVENT.add(firstEventAt);
      }
      const type = line.slice(6).trim();
      if (type === 'run.completed') {
        RUN_COMPLETED.add(1);
        RUN_E2E.add(Date.now() - started);
        return 'completed';
      }
      if (type === 'run.failed' || type === 'run.cancelled' || type === 'run.budget_exhausted') {
        RUN_FAILED.add(1);
        RUN_E2E.add(Date.now() - started);
        return type;
      }
    }
  }
  return 'stream_ended';
}

export default function () {
  const runId = submitRun();
  if (!runId) {
    sleep(1);
    return;
  }
  const outcome = streamUntilTerminal(runId);
  check(outcome, { 'run reached terminal': (o) => o === 'completed' || o === 'stream_ended' });
  sleep(1);
}
