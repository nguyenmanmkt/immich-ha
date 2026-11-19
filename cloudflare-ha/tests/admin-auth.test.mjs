import test from "node:test";
import assert from "node:assert/strict";
import { verifyAccessJWT, requireSameOrigin } from "../src/admin-auth.mjs";

const pair = await crypto.subtle.generateKey({ name: "RSASSA-PKCS1-v1_5", modulusLength: 2048,
  publicExponent: new Uint8Array([1, 0, 1]), hash: "SHA-256" }, true, ["sign", "verify"]);
const jwk = { ...await crypto.subtle.exportKey("jwk", pair.publicKey), kid: "test-key" };
const config = { teamDomain: "test-team.cloudflareaccess.com", audience: "application-123", emails: ["admin@example.net"] };
const now = 1_800_000_000_000;
const encode = (value) => Buffer.from(JSON.stringify(value)).toString("base64url");
async function token(extra = {}) {
  const input = encode({ alg: "RS256", kid: jwk.kid, jku: "https://untrusted.example/keys" }) + "." +
    encode({ iss: "https://" + config.teamDomain, exp: now / 1000 + 300, aud: [config.audience], email: "admin@example.net", ...extra });
  const signature = await crypto.subtle.sign("RSASSA-PKCS1-v1_5", pair.privateKey, new TextEncoder().encode(input));
  return input + "." + Buffer.from(signature).toString("base64url");
}
async function keys(url) {
  assert.equal(url, "https://test-team.cloudflareaccess.com/cdn-cgi/access/certs");
  return Response.json({ keys: [jwk] });
}

test("Access verifies signature against the configured issuer, ignoring JWT key URLs", async () => {
  const actor = await verifyAccessJWT(await token(), config, now, keys);
  assert.equal(actor.email, "admin@example.net");
});

test("Access rejects wrong audience, issuer, email, expired and tampered tokens", async () => {
  for (const claims of [{ aud: ["another-app"] }, { iss: "https://wrong.cloudflareaccess.com" },
    { email: "stranger@example.net" }, { exp: now / 1000 - 1 }]) {
    await assert.rejects(verifyAccessJWT(await token(claims), config, now, keys));
  }
  const parts = (await token()).split(".");
  parts[1] = encode({ iss: "https://" + config.teamDomain, exp: now / 1000 + 300, aud: [config.audience], email: "ADMIN@example.net", sub: "tampered" });
  await assert.rejects(verifyAccessJWT(parts.join("."), config, now, keys));
  await assert.rejects(verifyAccessJWT(await token(), { ...config, audience: "" }, now, keys));
});

test("control POST requires the configured dashboard origin and explicit JSON UI header", () => {
  const env = { ADMIN_HOST: "new-control.example.net" };
  const headers = { Origin: "https://new-control.example.net", "X-Immich-HA-UI": "1", "Content-Type": "application/json" };
  assert.doesNotThrow(() => requireSameOrigin(new Request("https://new-control.example.net/api/action", { method: "POST", headers }), env));
  for (const extra of [{ Origin: "https://old-control.example.net" }, { "X-Immich-HA-UI": "" }, { "Content-Type": "text/plain" }]) {
    assert.throws(() => requireSameOrigin(new Request("https://new-control.example.net/api/action", { method: "POST", headers: { ...headers, ...extra } }), env));
  }
});
