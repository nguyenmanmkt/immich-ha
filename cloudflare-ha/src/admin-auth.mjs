const cache = new Map();

function decode(value) {
  if (!/^[A-Za-z0-9_-]+$/.test(value)) throw new Error("Invalid JWT encoding");
  const binary = atob(value.replace(/-/g, "+").replace(/_/g, "/").padEnd(Math.ceil(value.length / 4) * 4, "="));
  return Uint8Array.from(binary, (char) => char.charCodeAt(0));
}

export async function verifyAccessJWT(token, config, now = Date.now(), fetcher = fetch) {
  if (!/^[a-z0-9][a-z0-9.-]*\.cloudflareaccess\.com$/.test(config.teamDomain || "") || !config.audience) {
    throw new Error("Chưa cấu hình Cloudflare Access cho dashboard");
  }
  if (typeof token !== "string" || token.length > 20_000) throw new Error("Cần đăng nhập Cloudflare Access");
  const parts = token.split(".");
  if (parts.length !== 3) throw new Error("Invalid JWT");
  const header = JSON.parse(new TextDecoder().decode(decode(parts[0])));
  const claims = JSON.parse(new TextDecoder().decode(decode(parts[1])));
  const issuer = `https://${config.teamDomain}`;
  const seconds = now / 1000;
  if (header.alg !== "RS256" || typeof header.kid !== "string" ||
      claims.iss !== issuer || !Number.isFinite(claims.exp) || claims.exp <= seconds ||
      (claims.nbf !== undefined && (!Number.isFinite(claims.nbf) || claims.nbf > seconds + 30)) ||
      (claims.iat !== undefined && (!Number.isFinite(claims.iat) || claims.iat > seconds + 30)) ||
      !(Array.isArray(claims.aud) ? claims.aud : [claims.aud]).includes(config.audience)) {
    throw new Error("Phiên đăng nhập không hợp lệ hoặc đã hết hạn");
  }
  const email = typeof claims.email === "string" ? claims.email.toLowerCase() : "";
  const allowed = config.emails || [];
  if (!email || !allowed.length || !allowed.map((value) => value.toLowerCase()).includes(email)) {
    throw new Error("Tài khoản này không có quyền quản trị");
  }
  let entry = cache.get(issuer);
  if (!entry || entry.expires <= now || !entry.keys.some((key) => key.kid === header.kid)) {
    const response = await fetcher(issuer + "/cdn-cgi/access/certs", {
      signal: AbortSignal.timeout(5000), headers: { Accept: "application/json" },
    });
    if (!response.ok) throw new Error("Không kiểm tra được phiên đăng nhập");
    const result = await response.json();
    if (!Array.isArray(result.keys) || result.keys.length > 30) throw new Error("Invalid Access signing keys");
    entry = { expires: now + 5 * 60_000, keys: result.keys };
    cache.set(issuer, entry);
  }
  const jwk = entry.keys.find((key) => key.kid === header.kid && key.kty === "RSA");
  if (!jwk) throw new Error("Unknown Access signing key");
  const key = await crypto.subtle.importKey("jwk", jwk, { name: "RSASSA-PKCS1-v1_5", hash: "SHA-256" }, false, ["verify"]);
  const valid = await crypto.subtle.verify("RSASSA-PKCS1-v1_5", key, decode(parts[2]), new TextEncoder().encode(parts[0] + "." + parts[1]));
  if (!valid) throw new Error("Invalid Access signature");
  return { email, subject: claims.sub || email };
}

export async function authenticateAdmin(request, env) {
  let token = request.headers.get("Cf-Access-Jwt-Assertion");
  if (!token) {
    const cookie = (request.headers.get("Cookie") || "").split(";")
      .map((value) => value.trim()).find((value) => value.startsWith("CF_Authorization="));
    token = cookie ? cookie.slice("CF_Authorization=".length) : "";
  }
  let emails;
  try { emails = JSON.parse(env.ADMIN_EMAILS || "[]"); } catch { emails = []; }
  return verifyAccessJWT(token, { teamDomain: env.ACCESS_TEAM_DOMAIN, audience: env.ACCESS_AUD, emails });
}

export function requireSameOrigin(request, env) {
  const expected = `https://${env.ADMIN_HOST}`;
  if (request.headers.get("Origin") !== expected || request.headers.get("X-Immich-HA-UI") !== "1" ||
      !request.headers.get("Content-Type")?.startsWith("application/json")) {
    throw new Error("Yêu cầu điều khiển phải xuất phát từ dashboard");
  }
}
