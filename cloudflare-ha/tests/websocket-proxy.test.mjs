import test from "node:test";
import assert from "node:assert/strict";
import { bridgeWebSockets } from "../src/websocket-proxy.mjs";

class Socket extends EventTarget {
  sent = []; closed = []; accepted = false;
  accept() { this.accepted = true; }
  send(data) { this.sent.push(data); }
  close(code, reason) { this.closed.push({ code, reason }); }
  emit(name, fields) { const event = new Event(name); Object.assign(event, fields); this.dispatchEvent(event); }
}
test("Socket.IO connection request and version event pass in opposite directions", () => {
  const origin = new Socket(), browser = new Socket();
  bridgeWebSockets(origin, browser);
  assert.ok(origin.accepted && browser.accepted);
  browser.emit("message", { data: "40" });
  assert.deepEqual(origin.sent, ["40"]);
  origin.emit("message", { data: '42["on_server_version",{"major":3,"minor":4,"patch":1}]' });
  assert.deepEqual(browser.sent, ['42["on_server_version",{"major":3,"minor":4,"patch":1}]']);
  const binary = new Uint8Array([1, 2]).buffer;
  browser.emit("message", { data: binary });
  assert.equal(origin.sent[1], binary);
});
test("closing either peer closes the other using a valid close code", () => {
  const a = new Socket(), b = new Socket(); bridgeWebSockets(a, b);
  a.emit("close", { code: 1006, reason: "" });
  assert.equal(b.closed[0].code, 1000);
  b.emit("error", {});
  assert.equal(a.closed[0].code, 1011);
});
