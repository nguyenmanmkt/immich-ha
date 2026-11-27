// Observation only: these results never participate in writer admission.
export function funnelUrl(value) {
  if (!value) return null;
  const url = new URL(value);
  if (url.protocol !== 'https:' || !/^[a-z0-9.-]+\.ts\.net$/.test(url.hostname) ||
      url.username || url.password || !['', '8443', '10000'].includes(url.port) ||
      url.pathname !== '/' || url.search || url.hash) throw new Error('Invalid Funnel URL');
  return url.origin;
}

export async function probeFunnel(value, fetcher = fetch, clock = Date.now) {
  const url = funnelUrl(value);
  if (!url) return null;
  const started = clock();
  let response;
  try {
    // Use a unique URL so an edge cache cannot replay a previous owner's ping.
    response = await fetcher(url + '/api/server/ping?_immich_ha_probe=' + crypto.randomUUID(), {
      redirect: 'manual', signal: AbortSignal.timeout(5000),
      headers: { 'User-Agent': 'ImmichHA-FunnelHealth/1.0', 'Cache-Control': 'no-cache, no-store' },
      cf: { cacheTtl: 0, cacheEverything: false },
    });
    const node = response.headers.get('X-Immich-HA-Node');
    const epoch = response.headers.get('X-Immich-HA-Epoch');
    let ping = false;
    if (response.status === 200) {
      const reader = response.body?.getReader();
      if (reader) {
        let text = '', bytes = 0;
        const decoder = new TextDecoder();
        try {
          while (true) {
            const chunk = await reader.read();
            if (chunk.done) break;
            bytes += chunk.value.byteLength;
            if (bytes > 1024) throw new Error('Unexpected ping body');
            text += decoder.decode(chunk.value, { stream: true });
          }
          text += decoder.decode();
          ping = JSON.parse(text).res === 'pong';
        } finally { await reader.cancel(); }
      }
    } else await response.body?.cancel();
    const ok = ping && ['primary', 'backup'].includes(node) && /^\d{1,15}$/.test(epoch || '') && Number(epoch) > 0;
    return { url, checkedAt: clock(), ok, status: response.status,
      node: ['primary', 'backup'].includes(node) ? node : null,
      epoch: ok ? Number(epoch) : null, latencyMs: Math.max(0, clock() - started),
      error: ok ? null : 'Funnel chưa trả về Immich và định danh hợp lệ' };
  } catch (error) {
    try { await response?.body?.cancel(); } catch { /* The reader may already own the stream. */ }
    return { url, checkedAt: clock(), ok: false, status: response?.status ?? null,
      node: null, epoch: null, latencyMs: Math.max(0, clock() - started),
      error: ['TimeoutError', 'AbortError'].includes(error.name) ? 'Kiểm tra quá 5 giây' : 'Không xác nhận được đường Funnel' };
  }
}

export function funnelStatus(value, reading, now, owner, epoch) {
  const url = funnelUrl(value);
  if (!url) return null;
  if (!reading || reading.url !== url) return { url, state: 'unknown' };
  const fresh = Number.isFinite(reading.checkedAt) && now >= reading.checkedAt && now - reading.checkedAt <= 180000;
  return { ...reading, state: !fresh ? 'stale' : !reading.ok ? 'unavailable' :
    reading.node !== owner || reading.epoch !== epoch ? 'route-mismatch' : 'healthy' };
}
