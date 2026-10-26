const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");
const vm = require("node:vm");

function deferred() {
  let resolve;
  const promise = new Promise((done) => { resolve = done; });
  return {promise, resolve};
}

function harness() {
  const nodes = new Map();
  class Element {
    constructor(tag = "div") {
      this.tag = tag;
      this.children = [];
      this.handlers = {};
      this.style = {};
      this.classList = {toggle() {}};
      this.value = "5";
    }
    setAttribute(key, value) { this[key] = value; if (key === "id") nodes.set(value, this); }
    addEventListener(type, handler) { this.handlers[type] = handler; }
    append(...children) { this.children.push(...children); }
    replaceChildren(...children) { this.children = children; }
    close() { this.handlers.close?.(); }
    emit(type) { return this.handlers[type]({preventDefault() {}}); }
  }
  const requests = new Map();
  const context = vm.createContext({
    Node: Element, console,
    document: {
      querySelector: () => ({content: "test-token"}),
      getElementById(id) { if (!nodes.has(id)) nodes.set(id, new Element()); return nodes.get(id); },
      createElement: (tag) => new Element(tag), createTextNode: (text) => text,
    },
    setTimeout: () => 1, clearTimeout() {},
    fetch: async (url, options) => {
      const key = `${options.method || "GET"} ${url}`;
      if (!requests.has(key)) throw new Error(`Unexpected request: ${key}`);
      return {ok: true, json: async () => await requests.get(key)};
    },
  });
  const source = fs.readFileSync(path.join(__dirname, "../videotag/gui_assets/app.js"), "utf8");
  vm.runInContext(source.replace(/\nstart\(\);\s*$/, "\n"), context);
  const run = (code) => vm.runInContext(code, context);
  run(`renderAccountButton = () => {}; renderHealth = () => {}; renderRun = () => {};
       renderOverview = () => {}; renderVideoList = () => {}; renderRuns = () => {}; updateVideoControls = () => {};
       app.account = {current: {url: "https://old.example", username: "old", label: "old"}, owner: {label: "old"}};
       wireAccountDialog();`);
  const nextAccount = {current: {url: "https://new.example", username: "new", label: "new"}, owner: {label: "new"}};
  requests.set("POST /api/account", nextAccount);
  async function switchAccount() {
    run("poll = async () => {};");
    await nodes.get("account-form").emit("submit");
  }
  function draw() {
    const section = run("spotSection()");
    const button = section.children.find((child) => child.children?.some((node) => node.tag === "button"))
      .children.find((child) => child.tag === "button");
    return button.emit("click");
  }
  return {run, nodes, requests, switchAccount, draw};
}

const sample = [{name: "Old Alice", confident: {total: 1, sample: [{video_id: 12, folder_id: 3, filename: "old-private.mp4"}]},
  suggested: {total: 0, sample: []}}];

test("switching accounts clears a displayed sample immediately", async () => {
  const h = harness();
  h.requests.set("POST /api/spotcheck", sample);
  await h.draw();
  assert.equal(h.run("app.spot[0].name"), "Old Alice");
  await h.switchAccount();
  assert.equal(h.run("app.spot"), null);
  assert.equal(h.nodes.get("spot-result").children.length, 0);
});

test("a sample requested for the old account cannot return under the new account", async () => {
  const h = harness(), pending = deferred();
  h.requests.set("POST /api/spotcheck", pending.promise);
  const drawing = h.draw();
  await h.switchAccount();
  pending.resolve(sample);
  await drawing;
  assert.equal(h.run("app.spot"), null);
  assert.equal(h.nodes.get("spot-result").children.length, 0);
});

test("an old library response cannot repopulate the page after switching accounts", async () => {
  const h = harness(), pending = deferred();
  h.requests.set("GET /api/library", pending.promise);
  h.requests.set("GET /api/history", []);
  h.requests.set("GET /api/accuracy", null);
  const loading = h.run("loadData()");
  await h.switchAccount();
  pending.resolve({report: {name: "old-report"}, videos: [{filename: "old-private.mp4"}]});
  await loading;
  assert.equal(h.run("app.library"), null);
});

test("an old status response cannot overwrite the new account's status", async () => {
  const h = harness(), pending = deferred();
  h.requests.set("GET /api/status", pending.promise);
  const polling = h.run("poll()");
  await h.switchAccount();
  pending.resolve({api: 4, version: null, status: {state: "running"}});
  await polling;
  assert.equal(h.run("app.status"), null);
});

test("loading changed library data clears the old sample and ignores an older draw", async () => {
  const h = harness(), pending = deferred();
  h.requests.set("POST /api/spotcheck", sample);
  await h.draw();
  h.requests.set("POST /api/spotcheck", pending.promise);
  const drawing = h.draw();
  h.requests.set("GET /api/library", {report: {name: "new-report"}, videos: []});
  h.requests.set("GET /api/history", []);
  h.requests.set("GET /api/accuracy", null);
  await h.run("loadData()");
  pending.resolve(sample);
  await drawing;
  assert.equal(h.run("app.spot"), null);
});

test("ordinary status polls keep a sample when the library version has not changed", async () => {
  const h = harness();
  h.requests.set("POST /api/spotcheck", sample);
  await h.draw();
  h.requests.set("GET /api/status", {api: 4, version: null, status: {state: "idle"}});
  await h.run("poll()");
  assert.equal(h.run("app.spot[0].name"), "Old Alice");
});
