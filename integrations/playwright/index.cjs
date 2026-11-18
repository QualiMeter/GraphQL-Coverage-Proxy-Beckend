const {randomUUID} = require('node:crypto');

function settings(options = {}) {
  const base = options.baseURL || process.env.COVERAGE_URL;
  const key = options.apiKey || process.env.COVERAGE_API_KEY;
  if (!base || !key) throw new Error('Test Coverage requires a server URL and API key');
  const url = new URL(base);
  if (!['http:', 'https:'].includes(url.protocol) || url.username || url.password || url.search || url.hash) {
    throw new Error('Use a Coverage API URL without credentials, query or fragment');
  }
  return {base: url.toString().replace(/\/$/, ''), key};
}

async function metadata(config, method, path, body) {
  for (let attempt = 0; attempt < 3; attempt++) {
    let response;
    try {
      response = await fetch(config.base + path, {
        method, headers: {'Content-Type': 'application/json', 'X-Coverage-Api-Key': config.key},
        body: body === undefined ? undefined : JSON.stringify(body), signal: AbortSignal.timeout(10000), redirect: 'error',
      });
    } catch {
      if (attempt === 2) throw new Error('Coverage metadata request could not be completed');
      await new Promise(resolve => setTimeout(resolve, 100 * (attempt + 1)));
      continue;
    }
    if (response.status >= 500 && attempt < 2) continue;
    if (!response.ok) {
      let payload;
      try { payload = await response.json(); } catch { payload = {}; }
      const error = new Error(`Coverage metadata request returned HTTP ${response.status}`);
      error.code = payload.detail?.code;
      throw error;
    }
    try { return await response.json(); }
    catch { throw new Error('Coverage metadata response is not JSON'); }
  }
}

function confirm(data, id, runId, status) {
  if (data?.id !== id || data?.runId !== runId || (status && data.status !== status)) {
    throw new Error('Coverage API did not confirm the submitted execution');
  }
  return data;
}

async function finish(config, annotation, result) {
  const status = result.status === 'timedOut' ? 'failed' : result.status;
  const failureCode = status === 'interrupted' ? 'runner_interrupted'
    : status === 'failed' ? result.status === 'timedOut' ? 'timeout' : 'test_failed' : null;
  const payload = {status, durationMs: Math.round(result.duration), failureCode};
  const path = `/api/runs/${encodeURIComponent(annotation.runId)}/tests/${encodeURIComponent(annotation.id)}/finish`;
  for (let attempt = 0; attempt < 25; attempt++) {
    try { return confirm(await metadata(config, 'POST', path, payload), annotation.id, annotation.runId, status); }
    catch (error) {
      if (error.code !== 'test_requests_in_flight' || attempt === 24) throw error;
      await new Promise(resolve => setTimeout(resolve, 200));
    }
  }
}

class CoverageReporter {
  constructor(options = {}) {
    this.options = options;
    this.pending = [];
    this.errors = [];
    this.seen = new Set();
  }

  onTestEnd(test, result) {
    const annotations = result.annotations || test.annotations || [];
    const runId = this.options.runId || process.env.COVERAGE_TEST_RUN_ID;
    if (!annotations.some(item => item.type === 'coverage.execution') && runId) {
      const id = randomUUID();
      const attempt = result.retry + 1;
      const testId = test.id + (test.repeatEachIndex ? `:repeat=${test.repeatEachIndex}` : '');
      const name = Array.from(test.titlePath().join(' › ').trim()).slice(0, 512).join('');
      const promise = Promise.resolve().then(async () => {
        const config = settings(this.options);
        confirm(await metadata(config, 'POST', `/api/runs/${encodeURIComponent(runId)}/tests`, {
          id, testId, name, framework: 'playwright', attempt,
        }), id, runId);
        await finish(config, {id, runId}, result);
      }).catch(error => { this.errors.push(error.message); });
      this.pending.push(promise);
      return;
    }
    for (const item of annotations.filter(item => item.type === 'coverage.execution')) {
      let annotation;
      try { annotation = JSON.parse(item.description); }
      catch { this.errors.push('Invalid Coverage execution annotation'); continue; }
      if (annotation.attempt !== result.retry + 1 || this.seen.has(annotation.id)) continue;
      this.seen.add(annotation.id);
      const promise = Promise.resolve().then(() => finish(settings(this.options), annotation, result))
        .catch(error => { this.errors.push(error.message); });
      this.pending.push(promise);
    }
  }

  async onEnd() {
    await Promise.all(this.pending);
    if (this.errors.length) {
      for (const error of this.errors) console.error('Test Coverage result was not confirmed: ' + error);
      return {status: 'failed'};
    }
  }
}

function createCoverageTest(baseTest, options = {}) {
  return baseTest.extend({
    coverage: [async ({}, use, info) => {
      const executions = new Map();
      const coverage = {
        async trackRun(runId) {
          let entry = executions.get(runId);
          if (!entry) {
            entry = {id: randomUUID(), pending: null};
            executions.set(runId, entry);
          }
          if (!entry.pending) {
            // Install the promise before registration yields. Retain the UUID
            // even after a network failure: POST may already have committed.
            entry.pending = Promise.resolve().then(async () => {
              const config = settings(options);
              const id = entry.id;
              const testId = (info.testId || info.titlePath.join('::')) + (info.repeatEachIndex ? `:repeat=${info.repeatEachIndex}` : '');
              const name = Array.from(info.titlePath.join(' › ').trim()).slice(0, 512).join('');
              const attempt = info.retry + 1;
              confirm(await metadata(config, 'POST', `/api/runs/${encodeURIComponent(runId)}/tests`, {
                id, testId, name, framework: 'playwright', attempt,
              }), id, runId);
              const headers = {'X-Coverage-Run-Id': runId, 'X-Coverage-Test-Execution-Id': id};
              const execution = {id, runId, headers,
                post(request, data) {
                  return request.post(config.base + '/graphql', {
                    headers: {...headers, 'X-Coverage-Api-Key': config.key}, data, maxRedirects: 0,
                  });
                },
              };
              info.annotations.push({type: 'coverage.execution', description: JSON.stringify({id, runId, attempt})});
              return execution;
            }).catch(error => { entry.pending = null; throw error; });
          }
          return entry.pending;
        },
      };
      const runId = options.runId || process.env.COVERAGE_TEST_RUN_ID;
      if (runId) await coverage.trackRun(runId);
      await use(coverage);
    }, {auto: true}],
  });
}

module.exports = CoverageReporter;
module.exports.createCoverageTest = createCoverageTest;
