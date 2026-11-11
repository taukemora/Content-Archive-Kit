/**
 * Behaviour tests for the encrypted reader's media scheduler.
 *
 * These load scripts/encrypted_reader.js itself (through its CommonJS test
 * seam) and drive it with a simulated DOM, injected geometry, and controlled
 * load promises. No real browser, network, or cryptography is involved.
 *
 * Run standalone:
 *   node tests/reader_media_scheduler.mjs
 * Exit code 0 and a JSON line {"ok": true, ...} mean every case passed.
 */
import { createRequire } from "node:module";
import path from "node:path";
import { fileURLToPath } from "node:url";

const here = path.dirname(fileURLToPath(import.meta.url));
const repo = path.resolve(here, "..");
const require = createRequire(import.meta.url);
// CA_READER_JS lets the wrapper point at a baseline copy to prove RED.
const readerPath = process.env.CA_READER_JS || path.join(repo, "scripts", "encrypted_reader.js");
const { createMediaScheduler } = require(readerPath);

class FakeNode {
  constructor(tag = "IMG", attrs = {}) {
    this.tagName = tag.toUpperCase();
    this.attrs = new Map(Object.entries(attrs));
    this.loading = undefined;
    this.src = undefined;
    this.href = undefined;
    this.listeners = new Map();
  }
  getAttribute(name) {
    return this.attrs.has(name) ? this.attrs.get(name) : null;
  }
  setAttribute(name, value) {
    this.attrs.set(name, String(value));
  }
  removeAttribute(name) {
    this.attrs.delete(name);
  }
  addEventListener(type, fn) {
    if (!this.listeners.has(type)) this.listeners.set(type, []);
    this.listeners.get(type).push(fn);
  }
  removeEventListener(type, fn) {
    const list = this.listeners.get(type) || [];
    this.listeners.set(type, list.filter((entry) => entry !== fn));
  }
}

function eventTarget() {
  const listeners = new Map();
  return {
    addEventListener(type, fn) {
      if (!listeners.has(type)) listeners.set(type, []);
      listeners.get(type).push(fn);
    },
    removeEventListener(type, fn) {
      const list = listeners.get(type) || [];
      listeners.set(type, list.filter((entry) => entry !== fn));
    },
    fire(type, event = {}) {
      for (const fn of listeners.get(type) || []) fn(event);
    },
    count(type) {
      return (listeners.get(type) || []).length;
    },
  };
}

function frames() {
  let next = 0;
  const queue = new Map();
  return {
    request(fn) {
      next += 1;
      queue.set(next, fn);
      return next;
    },
    cancel(id) {
      queue.delete(id);
    },
    flush() {
      const ready = [...queue.values()];
      queue.clear();
      for (const fn of ready) fn();
    },
    get size() {
      return queue.size;
    },
  };
}

function tick() {
  return new Promise((resolve) => setImmediate(resolve));
}

function geometry(value) {
  return {
    top: value.top,
    bottom: value.bottom,
    width: value.width == null ? 100 : value.width,
    height: value.height == null ? value.bottom - value.top : value.height,
  };
}

/**
 * Build a scheduler over N fake jobs. ``rects`` holds one entry per ciphertext
 * object; an entry is either a single node rect or an array of rects when the
 * same object is mounted at several nodes. ``pane`` is the visible pane box.
 */
function harness({
  rects,
  pane = { top: 0, bottom: 1000 },
  missingViewport = false,
  fallbackHeight = 0,
  concurrency = 3,
  prefetchFactor = 2,
} = {}) {
  const frame = frames();
  const win = eventTarget();
  const scrollTarget = eventTarget();
  const requests = [];
  const applied = [];
  const failed = [];
  const state = { active: true };
  let paneBox = missingViewport ? undefined : pane;

  const geoms = rects.map((entry) =>
    (Array.isArray(entry) ? entry : [entry]).map(geometry));
  const jobs = geoms.map((list, index) => ({
    rel: `objects/${String(index).padStart(64, "0")}.bin`,
    mime: "image/png",
    oid: String(index).padStart(64, "0"),
    kind: "img",
    nodes: list.map(() => new FakeNode("IMG", { "data-ca-src": `objects/${index}.bin` })),
    index,
    state: "pending",
  }));

  const scheduler = createMediaScheduler({
    jobs,
    concurrency,
    prefetchFactor,
    fallbackHeight,
    frame,
    win,
    scrollTarget,
    observeTarget: null,
    measure(job) {
      const list = geoms[job.index];
      // The shipped adapter returns an array; a single-occurrence job keeps the
      // legacy single-rect shape so the baseline implementation stays loadable.
      return list.length === 1 ? list[0] : list;
    },
    viewport() {
      return paneBox;
    },
    isActive() {
      return state.active;
    },
    load(job, signal) {
      const call = { job, signal, rel: job.rel, index: job.index };
      requests.push(call);
      return new Promise((resolve, reject) => {
        call.resolve = resolve;
        call.reject = reject;
      });
    },
    apply(job, url) {
      applied.push({ index: job.index, url, nodes: job.nodes });
      for (const node of job.nodes) node.src = url;
    },
    fail(job) {
      failed.push({ index: job.index, nodes: job.nodes });
    },
  });

  return {
    scheduler,
    jobs,
    frame,
    win,
    scrollTarget,
    requests,
    applied,
    failed,
    state,
    get rects() {
      // Tests scroll by mutating these live single-node rects (the same objects
      // the injected measure() returns).
      return geoms.map((list) => list[0]);
    },
    setPane(next) {
      paneBox = next;
    },
    included() {
      return requests.map((call) => call.index);
    },
    start() {
      scheduler.start();
    },
  };
}

const cases = [];
function test(name, fn) {
  cases.push({ name, fn });
}

// ---------------------------------------------------------------------------
// Cases
// ---------------------------------------------------------------------------

test("concurrency is capped and the far image is not requested", async () => {
  const env = harness({
    rects: [
      { top: 0, bottom: 100 },
      { top: 1500, bottom: 1600 },
      { top: 2500, bottom: 2600 },
      { top: 3500, bottom: 3600 },
      { top: 6000, bottom: 6100 },
    ],
  });
  env.start();
  if (env.requests.length !== 3) throw new Error(`expected 3 in-flight, saw ${env.requests.length}`);
  if (env.scheduler.stats().inFlight !== 3) throw new Error("stats inFlight should be 3");
  if (env.included().join(",") !== "0,1,2") throw new Error(`unexpected request order ${env.included()}`);
  if (env.requests.some((call) => call.index >= 3)) throw new Error("far image was requested");

  if (env.jobs[0].nodes[0].loading !== "eager") {
    throw new Error("scheduled image was not marked eager");
  }

  env.requests[0].resolve("blob:first");
  await tick();
  if (env.requests.length !== 3) throw new Error("freed slot scheduled an out-of-window image");
});

test("scrolling releases the far image", async () => {
  const env = harness({
    rects: [
      { top: 0, bottom: 100 },
      { top: 500, bottom: 600 },
      { top: 1200, bottom: 1300 },
      { top: 1900, bottom: 2000 },
      { top: 2600, bottom: 2700 },
      { top: 4600, bottom: 4700 },
    ],
    concurrency: 6,
  });
  env.start();
  if (env.included().join(",") !== "0,1,2,3,4") {
    throw new Error(`unexpected prefetch set ${env.included()}`);
  }
  if (env.requests.some((call) => call.index === 5)) throw new Error("image three screens away was prefetched");

  // Scroll down by 3600px; every node moves up by the same amount.
  for (const rect of env.rects) {
    rect.top -= 3600;
    rect.bottom -= 3600;
  }
  env.scrollTarget.fire("scroll");
  if (env.frame.size !== 1) throw new Error("scroll did not coalesce into one frame");
  env.frame.flush();
  if (!env.requests.some((call) => call.index === 5)) {
    throw new Error("scrolling to the image did not schedule it");
  }
});

test("a freed slot re-sorts pending jobs by current visibility", async () => {
  const env = harness({
    rects: [
      { top: 0, bottom: 100 },
      { top: 600, bottom: 700 },
      { top: 3000, bottom: 3100 },
    ],
    concurrency: 1,
  });
  env.start();
  if (env.included().join(",") !== "0") throw new Error(`unexpected first pick ${env.included()}`);

  for (const rect of env.rects) {
    rect.top -= 2500;
    rect.bottom -= 2500;
  }
  env.scrollTarget.fire("scroll");
  env.frame.flush();
  env.requests[0].resolve("blob:first");
  await tick();
  if (env.included().join(",") !== "0,2") {
    throw new Error(`visible image did not win the freed slot: ${env.included()}`);
  }
});

test("completed jobs are never requested again", async () => {
  const env = harness({
    rects: [
      { top: 0, bottom: 100 },
      { top: 300, bottom: 400 },
    ],
    concurrency: 2,
  });
  env.start();
  env.requests[0].resolve("blob:a");
  env.requests[1].resolve("blob:b");
  await tick();
  const calls = env.requests.length;

  for (let i = 0; i < 5; i += 1) env.scheduler.refreshNow();
  env.scrollTarget.fire("scroll");
  env.frame.flush();
  env.win.fire("resize");
  env.frame.flush();

  if (env.requests.length !== calls) throw new Error("refresh/scroll re-requested finished images");
  if (env.applied.length !== 2) throw new Error("a finished job was applied twice");
  if (env.scheduler.stats().done !== 2) throw new Error("done count is wrong");
});

test("one failed image does not block the others", async () => {
  const env = harness({
    rects: [
      { top: 0, bottom: 100 },
      { top: 100, bottom: 200 },
      { top: 200, bottom: 300 },
      { top: 300, bottom: 400 },
    ],
    concurrency: 2,
  });
  env.start();
  env.requests[0].reject(new Error("tampered"));
  await tick();
  if (env.failed.length !== 1 || env.failed[0].index !== 0) throw new Error("failure was not reported");
  if (env.requests.length !== 3) throw new Error("freed slot was not refilled after a failure");

  env.requests[1].resolve("blob:1");
  env.requests[2].resolve("blob:2");
  await tick();
  env.requests[3].resolve("blob:3");
  await tick();

  if (env.applied.length !== 3) throw new Error(`expected 3 successes, saw ${env.applied.length}`);
  if (env.scheduler.stats().failed !== 1) throw new Error("failed count is wrong");
});

test("stop aborts in-flight loads and swallows late writes", async () => {
  const env = harness({
    rects: [
      { top: 0, bottom: 100 },
      { top: 100, bottom: 200 },
    ],
    concurrency: 2,
  });
  env.start();
  const signals = env.requests.map((call) => call.signal);
  if (signals.some((signal) => !signal)) throw new Error("loader did not receive an abort signal");
  env.scheduler.stop();
  if (!signals.every((signal) => signal.aborted)) throw new Error("stop did not abort in-flight signals");
  if (env.scrollTarget.count("scroll") !== 0) throw new Error("scroll listener survived stop");
  if (env.win.count("resize") !== 0) throw new Error("resize listener survived stop");

  env.requests[0].resolve("blob:late");
  env.requests[1].reject(new Error("aborted"));
  await tick();
  if (env.applied.length !== 0) throw new Error("late decrypt wrote a blob URL after stop");
  if (env.failed.length !== 0) throw new Error("late failure replaced a node after stop");
  if (env.requests.length !== 2) throw new Error("stopped scheduler started another request");
});

test("an epoch/token change discards a late decrypt", async () => {
  const env = harness({
    rects: [{ top: 0, bottom: 100 }],
    concurrency: 1,
  });
  env.start();
  env.state.active = false; // article switched or reader locked
  env.requests[0].resolve("blob:stale");
  await tick();
  if (env.applied.length !== 0) throw new Error("stale decrypt was applied");
  if (env.jobs[0].state !== "stale") throw new Error(`job state was ${env.jobs[0].state}`);
});

test("a hidden pane pauses scheduling until it is measurable again", async () => {
  const env = harness({
    rects: [
      { top: 0, bottom: 100 },
      { top: 900, bottom: 1000 },
      { top: 9000, bottom: 9100 },
    ],
    pane: { top: 0, bottom: 0 },
    fallbackHeight: 800,
    concurrency: 2,
  });
  env.start();
  if (env.requests.length !== 0) {
    throw new Error(`hidden pane still requested ${env.included()}`);
  }
  if (env.scheduler.stats().pending !== 3) throw new Error("hidden pane dropped pending jobs");

  env.setPane({ top: 0, bottom: 1000 });
  env.win.fire("resize");
  env.frame.flush();
  if (env.included().join(",") !== "0,1") {
    throw new Error(`pane did not resume after resize: ${env.included()}`);
  }
});

test("a missing viewport still falls back to the window height", async () => {
  const env = harness({
    rects: [
      { top: 0, bottom: 100 },
      { top: 900, bottom: 1000 },
      { top: 9000, bottom: 9100 },
    ],
    missingViewport: true,
    fallbackHeight: 800,
    concurrency: 2,
  });
  env.start();
  if (env.included().join(",") !== "0,1") {
    throw new Error(`missing viewport did not use the window fallback: ${env.included()}`);
  }
});

test("zero-size nodes are never treated as visible", async () => {
  const env = harness({
    rects: [
      { top: 0, bottom: 0, width: 0, height: 0 },
      { top: 500, bottom: 600 },
    ],
    concurrency: 2,
  });
  env.start();
  if (env.included().join(",") !== "1") {
    throw new Error(`zero-size node was scheduled: ${env.included()}`);
  }
});

test("a hidden pane pauses new downloads but keeps in-flight work", async () => {
  const env = harness({
    rects: [
      { top: 0, bottom: 100 },
      { top: 100, bottom: 200 },
    ],
    concurrency: 1,
  });
  env.start();
  if (env.included().join(",") !== "0") throw new Error(`unexpected start ${env.included()}`);
  env.setPane({ top: 0, bottom: 0 });
  env.requests[0].resolve("blob:a");
  await tick();
  if (env.requests.length !== 1) throw new Error("hidden pane started a new download after a completion");

  env.setPane({ top: 0, bottom: 1000 });
  env.win.fire("resize");
  env.frame.flush();
  if (env.included().join(",") !== "0,1") throw new Error(`resume did not schedule the next job: ${env.included()}`);
});

test("a job with one visible occurrence outranks its distant first node", async () => {
  const env = harness({
    rects: [
      [
        { top: -7513, bottom: -7108 },
        { top: 336, bottom: 741 },
      ],
    ],
  });
  env.start();
  if (env.included().join(",") !== "0") {
    throw new Error("a visible later occurrence of the object was not scheduled");
  }
});

test("a job whose occurrences are all far is not scheduled", async () => {
  const env = harness({
    rects: [
      [
        { top: -3000, bottom: -2900 },
        { top: 3300, bottom: 3400 },
      ],
    ],
  });
  env.start();
  if (env.requests.length !== 0) {
    throw new Error("a bounding box spanning the pane was mistaken for a visible node");
  }
});

test("duplicate occurrences share one request and hydrate every node", async () => {
  const env = harness({
    rects: [[
      { top: 0, bottom: 100 },
      { top: 0, bottom: 100 },
    ]],
    concurrency: 2,
  });
  env.start();
  if (env.requests.length !== 1) throw new Error(`duplicate object was requested ${env.requests.length} times`);
  env.requests[0].resolve("blob:dup");
  await tick();
  if (env.applied.length !== 1) throw new Error("duplicate object was applied more than once");
  if (env.applied[0].nodes.length !== 2) throw new Error("not every occurrence was applied");
  for (const node of env.jobs[0].nodes) {
    if (node.src !== "blob:dup") throw new Error("an occurrence did not receive the blob URL");
  }
  for (let i = 0; i < 5; i += 1) env.scheduler.refreshNow();
  env.scrollTarget.fire("scroll");
  env.frame.flush();
  if (env.requests.length !== 1) throw new Error("duplicate object was re-requested");
  if (env.scheduler.stats().done !== 1) throw new Error("duplicate object was not a single done job");
});

test("scroll and resize listeners are registered and removed", async () => {
  const env = harness({
    rects: [{ top: 0, bottom: 100 }],
    concurrency: 1,
  });
  env.start();
  if (env.scrollTarget.count("scroll") !== 1) throw new Error("scroll listener missing");
  if (env.win.count("resize") !== 1) throw new Error("resize listener missing");
  env.scheduler.stop();
  if (env.scrollTarget.count("scroll") !== 0) throw new Error("scroll listener not removed");
  if (env.win.count("resize") !== 0) throw new Error("resize listener not removed");
});

// ---------------------------------------------------------------------------

let failedCases = 0;
const results = [];
for (const entry of cases) {
  try {
    await entry.fn();
    results.push({ name: entry.name, ok: true });
  } catch (error) {
    failedCases += 1;
    results.push({ name: entry.name, ok: false, error: String(error && error.message ? error.message : error) });
  }
}

const report = { ok: failedCases === 0, total: cases.length, failed: failedCases, cases: results };
console.log(JSON.stringify(report));
process.exit(failedCases === 0 ? 0 : 1);
