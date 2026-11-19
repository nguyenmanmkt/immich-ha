// Explicitly couple both directions. HTTP 101 alone is not a Socket.IO check.
export function bridgeWebSockets(upstream, downstream) {
  const close = (socket, code = 1000, reason = "") => {
    // 1005/1006/1015 are received-only codes, never valid on the wire.
    const valid = code === 1000 || [1001, 1002, 1003, 1007, 1008, 1009, 1010, 1011, 1012, 1013, 1014].includes(code) || code >= 3000 && code <= 4999;
    try { socket.close(valid ? code : 1000, reason.slice(0, 100)); } catch {}
  };
  for (const [source, destination] of [[upstream, downstream], [downstream, upstream]]) {
    source.addEventListener("message", (event) => {
      try { destination.send(event.data); }
      catch { close(source, 1011, "Proxy connection failed"); close(destination, 1011, "Proxy connection failed"); }
    });
    source.addEventListener("close", (event) => close(destination, event.code, event.reason || ""));
    source.addEventListener("error", () => {
      close(source, 1011, "Proxy connection failed"); close(destination, 1011, "Proxy connection failed");
    });
  }
  downstream.accept();
  upstream.accept();
}

export function websocketResponse(response, node, epoch) {
  if (!response.webSocket) throw new Error("Origin did not provide a WebSocket");
  const [client, server] = Object.values(new WebSocketPair());
  bridgeWebSockets(response.webSocket, server);
  const headers = new Headers({ "Cache-Control": "no-store", "X-Immich-HA-Node": node, "X-Immich-HA-Epoch": String(epoch) });
  const protocol = response.headers.get("Sec-WebSocket-Protocol");
  if (protocol) headers.set("Sec-WebSocket-Protocol", protocol);
  return new Response(null, { status: 101, webSocket: client, headers });
}
