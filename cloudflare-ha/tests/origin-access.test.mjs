import test from "node:test";
import assert from "node:assert/strict";
import { originAccessHeaders } from "../src/origin-access.mjs";

test("uses gateway credentials instead of credentials supplied by the caller", () => {
  const env = { ORIGIN_ACCESS_CLIENT_ID: "gateway.access", ORIGIN_ACCESS_CLIENT_SECRET: "s".repeat(48) };
  const incoming = { "CF-Access-Client-Id": "caller.access", "CF-Access-Client-Secret": "caller-secret",
    "Cf-Access-Jwt-Assertion": "caller-jwt", Authorization: "Bearer immich-session", Range: "bytes=0-99" };
  const headers = originAccessHeaders(env, incoming);
  assert.equal(headers.get("CF-Access-Client-Id"), env.ORIGIN_ACCESS_CLIENT_ID);
  assert.equal(headers.get("CF-Access-Client-Secret"), env.ORIGIN_ACCESS_CLIENT_SECRET);
  assert.equal(headers.get("Cf-Access-Jwt-Assertion"), null);
  assert.equal(headers.get("Authorization"), "Bearer immich-session");
  assert.equal(headers.get("Range"), "bytes=0-99");
  assert.equal(incoming["CF-Access-Client-Id"], "caller.access");
});

test("legacy setup strips caller Access identity and sends no service credentials", () => {
  const headers = originAccessHeaders({}, { "CF-Access-Client-Id": "caller.access", "CF-Access-Client-Secret": "secret" });
  assert.equal(headers.has("CF-Access-Client-Id"), false);
  assert.equal(headers.has("CF-Access-Client-Secret"), false);
});

test("partial or invalid configuration fails without including credentials in the error", () => {
  for (const env of [ { ORIGIN_ACCESS_CLIENT_ID: "gateway.access" },
    { ORIGIN_ACCESS_CLIENT_SECRET: "s".repeat(48) },
    { ORIGIN_ACCESS_CLIENT_ID: "gateway.access", ORIGIN_ACCESS_CLIENT_SECRET: "bad\r\n" + "s".repeat(48) } ]) {
    assert.throws(() => originAccessHeaders(env), { message: "Invalid origin Access service token configuration" });
  }
});
