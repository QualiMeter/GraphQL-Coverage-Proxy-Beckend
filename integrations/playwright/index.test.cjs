const {test} = require('node:test');
const assert = require('node:assert/strict');
const http = require('node:http');
const {createCoverageTest} = require('./index.cjs');

async function fixture(options, use) {
  const fixtures = createCoverageTest({extend: fixtures => fixtures}, options);
  const info = {testId: 'test', titlePath: ['suite', 'test'], retry: 0, annotations: []};
  await fixtures.coverage[0]({}, coverage => use(coverage, info), info);
}

test('fixture registration sends team and primary suite', async () => {
  const original = global.fetch;
  const payloads = [];
  global.fetch = async (url, options) => {
    const body = JSON.parse(options.body);
    payloads.push(body);
    return new Response(JSON.stringify({id: body.id, runId: 'run'}), {status: 201});
  };
  try {
    await fixture({
      baseURL: 'http://coverage.test', apiKey: 'test-key', teamKey: 'checkout', suiteKey: 'smoke',
    }, coverage => coverage.trackRun('run'));
    assert.equal(payloads[0].teamKey, 'checkout');
    assert.equal(payloads[0].suiteKey, 'smoke');
  } finally { global.fetch = original; }
});

test('parallel trackRun shares registration and retry retains UUID', async () => {
  const original = global.fetch;
  const ids = [];
  let fail = true;
  global.fetch = async (url, options) => {
    const body = JSON.parse(options.body);
    ids.push(body.id);
    if (fail) throw new Error('temporary');
    return new Response(JSON.stringify({id: body.id, runId: 'run'}), {status: 201});
  };
  try {
    await fixture({baseURL: 'http://coverage.test', apiKey: 'test-key'}, async (coverage, info) => {
      const failed = await Promise.allSettled([coverage.trackRun('run'), coverage.trackRun('run')]);
      assert.ok(failed.every(result => result.status === 'rejected'));
      assert.equal(ids.length, 3); // one registration, with metadata retries
      fail = false;
      const [a, b] = await Promise.all([coverage.trackRun('run'), coverage.trackRun('run')]);
      assert.strictEqual(a, b);
      assert.equal(ids.length, 4);
      assert.equal(new Set(ids).size, 1);
      assert.equal(info.annotations.length, 1);
    });
  } finally { global.fetch = original; }
});

test('GraphQL helper never follows upstream Location with API key', async () => {
  const {request} = require('@playwright/test');
  const received = [];
  const server = http.createServer((req, res) => {
    received.push({path: req.url, key: req.headers['x-coverage-api-key']});
    if (req.url === '/graphql') {
      res.writeHead(307, {Location: `http://127.0.0.1:${server.address().port}/foreign`});
      res.end();
    } else if (req.url === '/api/runs/run/tests') {
      let body = '';
      req.on('data', chunk => body += chunk);
      req.on('end', () => {res.writeHead(201, {'Content-Type': 'application/json'}); res.end(JSON.stringify({id: JSON.parse(body).id, runId: 'run'}));});
    } else { res.end('unexpected redirect'); }
  });
  await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
  const context = await request.newContext();
  try {
    await fixture({baseURL: `http://127.0.0.1:${server.address().port}`, apiKey: 'test-only-key'}, async coverage => {
      const execution = await coverage.trackRun('run');
      const response = await execution.post(context, {query: '{ health }'});
      assert.equal(response.status(), 307);
      assert.deepEqual(received.map(row => row.path), ['/api/runs/run/tests', '/graphql']);
      assert.ok(received.every(row => row.key === 'test-only-key'));
    });
  } finally { await context.dispose(); await new Promise(resolve => server.close(resolve)); }
});

test('reporter recovers registration after every fixture response was lost after commit', async () => {
  const Reporter = require('./index.cjs');
  const original = global.fetch;
  const ids = [];
  const info = {testId: 'test', titlePath: ['suite', 'test'], retry: 0, annotations: []};
  let lost = true;
  let state;
  global.fetch = async (url, options) => {
    const body = JSON.parse(options.body);
    if (url.endsWith('/tests')) {
      ids.push(body.id);
      if (!state) state = {id: body.id, runId: 'run', status: 'running'};
      if (state.id !== body.id) return new Response('{}', {status: 409});
      if (lost) throw new Error('response lost after commit');
    } else {
      state.status = body.status;
    }
    return new Response(JSON.stringify(state), {status: 200});
  };
  try {
    const fixtures = createCoverageTest({extend: f => f}, {baseURL: 'http://coverage.test', apiKey: 'key', runId: 'run'});
    await assert.rejects(fixtures.coverage[0]({}, async () => {}, info));
    lost = false;
    const reporter = new Reporter({baseURL: 'http://coverage.test', apiKey: 'key', runId: 'run'});
    reporter.onTestEnd({id: 'test', titlePath: () => info.titlePath, annotations: info.annotations},
      {retry: 0, status: 'failed', duration: 1, annotations: info.annotations});
    assert.equal(await reporter.onEnd(), undefined);
    assert.equal(new Set(ids).size, 1);
    assert.equal(state.status, 'failed');
  } finally { global.fetch = original; }
});
