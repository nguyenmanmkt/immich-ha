// Apply credentials only to origin subrequests, never to browser responses.
export function originAccessHeaders(env, input = {}) {
  const headers = new Headers(input);
  // Caller-supplied credentials must never decide the gateway's identity.
  headers.delete("CF-Access-Client-Id");
  headers.delete("CF-Access-Client-Secret");
  headers.delete("Cf-Access-Jwt-Assertion");
  const id = env.ORIGIN_ACCESS_CLIENT_ID;
  const secret = env.ORIGIN_ACCESS_CLIENT_SECRET;
  if (!id && !secret) return headers; // Existing Nginx-only deployments.
  if (typeof id !== "string" || typeof secret !== "string" ||
      !id.endsWith(".access") || secret.length < 32 || /[\r\n]/.test(id + secret)) {
    throw new Error("Invalid origin Access service token configuration");
  }
  headers.set("CF-Access-Client-Id", id);
  headers.set("CF-Access-Client-Secret", secret);
  return headers;
}
