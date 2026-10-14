// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
"use strict";
// Synthetic setup flows. No network scan, robot actions, audio or device calls.
const test = require("node:test");
const assert = require("node:assert/strict");
const {reachyControlOptions, ReachySetupController, validateReachyAddress, requestReachySetup,
  renderReachyDiscoveries} = require("../web/app.js");

const snapshot = overrides => ({available: true, configured: false, skipped: false,
  address: "", bridge_prepared: true, busy: false, ...overrides});
const deferred = () => { let resolve; const promise = new Promise(r => { resolve = r; }); return {promise, resolve}; };
function fixture(initial = snapshot()) {
  let saved = initial;
  const calls = [], events = [];
  const model = new ReachySetupController({
    request: async (path, body) => {
      calls.push({path, body});
      if (path.endsWith("/discover")) return {devices: [{name: "Demo robot", address: "reachy-demo.local"}]};
      if (path.endsWith("/configure")) saved = snapshot({configured: true, address: body.address});
      if (path.endsWith("/skip")) saved = snapshot({skipped: true});
      return saved;
    },
    changed: () => events.push("render"), beforeChange: () => events.push("stop robot source"),
    afterChange: async () => events.push("refresh controls and services"),
  });
  return {model, calls, events};
}

test("an undecided visit discovers once, offers choices, and never attaches automatically", async () => {
  const {model, calls, events} = fixture();
  await model.refresh(true); await model.refresh(true);
  assert.equal(calls.filter(x => x.path.endsWith("/discover")).length, 1);
  assert.equal(calls.filter(x => x.path.endsWith("/configure")).length, 0);
  assert.equal(model.configured, false); assert.equal(model.devices.length, 1);
  assert.ok(!events.includes("stop robot source"));
});

for (const config of [{skipped: true}, {configured: true, address: "robot.local"}, {available: false}, {busy: true}, {status: "error", message: "Configuration needs repair"}]) {
  test(`saved choice or unavailable setup prevents automatic discovery: ${JSON.stringify(config)}`, async () => {
    const {model, calls} = fixture(snapshot(config)); await model.refresh(true);
    assert.equal(calls.length, 1);
  });
}

test("skip persists across visits, with explicit discover and connect still available later", async () => {
  const {model, calls, events} = fixture(); await model.refresh(); await model.change("skip");
  assert.equal(model.snapshot.skipped, true); assert.equal(model.configured, false);
  await model.refresh(true);
  assert.ok(!calls.some(x => x.path.endsWith("/discover")));
  await model.discover(); await model.change("configure", "  robot.local  ");
  assert.equal(model.snapshot.address, "robot.local"); assert.equal(model.snapshot.skipped, false);
  assert.equal(model.configured, true);
  assert.deepEqual(events.filter(x => x !== "render"), ["stop robot source", "refresh controls and services", "stop robot source", "refresh controls and services"]);
});

test("manual addresses reject URL credentials, paths, ports and malformed IPv4 before any setup change", async () => {
  const {model, calls, events} = fixture(); await model.refresh();
  for (const address of ["", "http://robot.local", "robot.local:8000", "robot.local/path", "user@robot.local", "robot;touch", "999.1.1.1", "192.168.1", "-robot.local", "192.168.01.2"]) {
    assert.throws(() => validateReachyAddress(address)); await model.change("configure", address);
  }
  assert.equal(calls.length, 1); assert.ok(!events.includes("stop robot source"));
  assert.equal(validateReachyAddress("  Reachy-1.local  "), "reachy-1.local");
  assert.equal(validateReachyAddress("192.0.2.50"), "192.0.2.50");
});

test("unavailable or blocked discovery remains a manual/skip path without attachment", async () => {
  const {model, calls} = fixture(); await model.refresh();
  model.request = async () => { throw new Error("Multicast unavailable"); };
  await model.discover();
  assert.equal(model.busy, false); assert.equal(model.failed, true);
  assert.match(model.message, /address.*skip/i); assert.deepEqual(model.devices, []);
  assert.equal(calls.length, 1);
});

test("setup actions cannot overlap discovery and failures refresh actual saved state", async () => {
  const {model, events} = fixture(snapshot({configured: true, address: "original.local"})); await model.refresh();
  const waiting = deferred(), calls = [];
  model.request = async (path, body) => {
    calls.push(path);
    if (path.endsWith("/discover")) return waiting.promise;
    if (path.endsWith("/configure")) throw new Error("Selected robot could not be verified");
    return snapshot({configured: true, address: "original.local"});
  };
  const scan = model.discover(); await model.change("skip");
  assert.equal(model.busy, true); assert.deepEqual(calls, ["/api/reachy/setup/discover"]);
  waiting.resolve({devices: []}); await scan;
  await model.change("configure", "replacement.local");
  assert.equal(model.snapshot.address, "original.local"); assert.equal(model.failed, true);
  assert.match(model.message, /could not be verified/); assert.equal(model.busy, false);
  assert.equal(events.at(-1), "refresh controls and services");
});

test("a late pre-change status cannot restore the previous robot", async () => {
  const {model} = fixture(snapshot({configured: true, address: "old.local"})); await model.refresh();
  const old = deferred(); let gets = 0;
  model.request = async (path) => {
    if (path.endsWith("/skip")) return snapshot({skipped: true});
    gets += 1; return gets === 1 ? old.promise : snapshot({skipped: true});
  };
  const poll = model.refresh(); await model.change("skip");
  old.resolve(snapshot({configured: true, address: "old.local"})); await poll;
  assert.equal(model.configured, false); assert.equal(model.snapshot.skipped, true);
});

test("unknown setup cannot auto-connect and a successful retry clears the status failure", async () => {
  const {model} = fixture(); model.request = async () => ({available: true});
  await model.refresh(true); assert.equal(model.fresh, false); assert.equal(model.failed, true);
  model.request = async () => snapshot({skipped: true});
  await model.refresh(true); assert.equal(model.fresh, true); assert.equal(model.failed, false);
});

test("setup writes use same-origin token and JSON, and a rejected token is discarded", async () => {
  const calls = []; let invalidated = 0;
  const deps = {loadAccess: async () => ({reachy_token: "synthetic-test-token-only"}),
    unauthorized: () => { invalidated += 1; }, fetcher: async (path, options) => {
      calls.push({path, options}); return {ok: true, status: 200, json: async () => snapshot()};
    }};
  await requestReachySetup("/api/reachy/setup/discover", {}, deps);
  const options = calls[0].options;
  assert.equal(options.credentials, "same-origin"); assert.equal(options.method, "POST");
  assert.equal(options.headers["Content-Type"], "application/json");
  assert.equal(options.headers["X-Reachy-Token"], "synthetic-test-token-only");
  assert.deepEqual(JSON.parse(options.body), {});
  deps.fetcher = async () => ({ok: false, status: 401, json: async () => ({error: {message: "Expired access"}})});
  await assert.rejects(requestReachySetup("/api/reachy/setup/skip", {}, deps), /Expired access/);
  assert.equal(invalidated, 1);
});

function element(document) {
  return {ownerDocument: document, children: [], events: {}, attributes: {},
    setAttribute(k, v) { this.attributes[k] = v; }, addEventListener(k, v) { this.events[k] = v; },
    replaceChildren(...nodes) { this.children = nodes; }, set innerHTML(_) { throw Error("Unsafe HTML"); }};
}
test("discovered robot names render as text and selecting never runs Connect", () => {
  const document = {createElement: () => element(document)}, container = element(document), selected = [];
  const devices = [{name: '<img src=x onerror="bad()">', address: "robot.local"}];
  renderReachyDiscoveries(container, devices, "", false, address => selected.push(address));
  const button = container.children[0];
  assert.equal(button.textContent, `${devices[0].name} · robot.local`);
  assert.equal(button.attributes["aria-pressed"], "false");
  button.events.click(); assert.deepEqual(selected, ["robot.local"]);
  renderReachyDiscoveries(container, devices, "robot.local", true, address => selected.push(address));
  container.children[0].events.click(); assert.equal(selected.length, 1);
  assert.equal(container.children[0].attributes["aria-pressed"], "true");
});


test("a malformed saved configuration blocks changes and clears only after a healthy status", async () => {
  const {model, calls} = fixture(snapshot({status: "error", message: "Repair the saved configuration"}));
  await model.refresh(true); await model.change("configure", "robot.local"); await model.change("skip");
  assert.equal(model.usable, false); assert.equal(model.failed, true); assert.equal(calls.length, 1);
  assert.equal(model.message, "Repair the saved configuration");
  model.request = async () => snapshot({skipped: true}); await model.refresh();
  assert.equal(model.usable, true); assert.equal(model.failed, false);
});


for (const state of [
  {status: "error", message: "Saved configuration needs repair"},
  {available: false, message: "Reachy preparation is unavailable"},
  {configured: true, address: "different-robot.local"},
  {configured: true, address: "robot.local", busy: true},
]) {
  test(`POST success cannot hide an unconfirmed refreshed state: ${JSON.stringify(state)}`, async () => {
    const {model} = fixture(); await model.refresh();
    model.request = async path => path.endsWith("/configure")
      ? snapshot({configured: true, address: "robot.local"}) : snapshot(state);
    await model.change("configure", "robot.local");
    assert.equal(model.failed, true); assert.doesNotMatch(model.message, /Robot address saved/);
    if (state.message) assert.equal(model.message, state.message);
  });
}

test("a resolved and pinned robot IP confirms its hostname configuration", async () => {
  const {model} = fixture(); await model.refresh();
  model.request = async () => snapshot({configured: true, address: "192.0.2.50"});
  await model.change("configure", "robot.local");
  assert.equal(model.failed, false); assert.match(model.message, /Robot address saved/);
});

test("skip success requires the refreshed skipped state", async () => {
  const {model} = fixture(); await model.refresh();
  model.request = async () => snapshot({configured: true, address: "other.local"});
  await model.change("skip");
  assert.equal(model.failed, true); assert.doesNotMatch(model.message, /Reachy skipped/);
});

test("robot controls always bind to the saved target without dropping JSON metadata", () => {
  const action = reachyControlOptions("robot-one.local");
  assert.equal(action.headers["X-Reachy-Address"], "robot-one.local");
  assert.equal(action.method, "POST");
  const speech = reachyControlOptions("robot-two.local", {text: "Synthetic test", rate: 1});
  assert.equal(speech.headers["X-Reachy-Address"], "robot-two.local");
  assert.equal(speech.headers["Content-Type"], "application/json");
  assert.deepEqual(JSON.parse(speech.body), {text: "Synthetic test", rate: 1});
  assert.equal(action.headers["X-Reachy-Address"], "robot-one.local");
});
