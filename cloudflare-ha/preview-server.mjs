// Local, read-only UI preview. Never contacts Cloudflare or either Immich node.
import http from "node:http";
import { readFile } from "node:fs/promises";
import { initialState, heartbeat, recordProbe, status } from "./src/controller.mjs";

const project = JSON.parse(await readFile(new URL("./project.json", import.meta.url), "utf8"));
const GiB = 1024 ** 3;
function previewState() {
  const now = Date.now();
  const s = initialState();
  s.auto = true;
  for (const node of ["primary", "backup"]) {
    const metrics = {
      runtimeV2: true, appMode: node === "primary" ? "active" : "standby",
      appVersion: "3.4.1", postgresVersion: "16.16",
      disk: node === "primary" ? { total: 931 * GiB, used: 154 * GiB, free: 730 * GiB } : { total: 118 * GiB, used: 107 * GiB, free: 5.5 * GiB },
      memory: { total: 6 * GiB, used: (node === "primary" ? 2.4 : 1.1) * GiB },
      backup: { lastDumpAt: now - 22 * 60000, count: 24 },
      seed: node === "backup" ? { source: "primary", sourceEpoch: 1, appliedAt: now - 21 * 60000 } : null,
      syncthing: { deviceId: node + "-preview-device", folders: { "immich-library": { paused: false, state: "idle", needItems: 0 }, "immich-database": { paused: false, state: "idle", needItems: 0 } } },
    };
    const payload = { session: node + "_preview_session_1234", ready: true, epoch: 1,
      localHealthy: true, enforcing: true, protocolVersion: 2, canPromote: node === "backup", metrics };
    heartbeat(s, node, payload, now - 8000);
    recordProbe(s, node, { ok: true, status: 200 }, now - 5000);
    if (node === "primary") heartbeat(s, node, payload, now - 3000);
  }
  s.audit = [
    { at: now - 30 * 60000, actor: "coordinator", action: "Kiểm tra hệ thống", details: "Hai server phản hồi bình thường · dữ liệu mô phỏng" },
    { at: now - 22 * 60000, actor: "agent", action: "Sao lưu database", details: "Bản sao hoàn chỉnh đã sẵn sàng · dữ liệu mô phỏng" },
  ];
  return { ...status(s, now), nodes: project.nodes, actorEmail: project.access.emails[0], publicUrl: "https://" + project.publicHost, preview: true };
}

const files = { "/": ["web/index.html", "text/html"], "/assets/admin.css": ["web/admin.css", "text/css"], "/assets/admin.js": ["web/admin.js", "text/javascript"], "/assets/status-model.mjs": ["web/status-model.mjs", "text/javascript"] };
const server = http.createServer(async (request, response) => {
  try {
    const path = new URL(request.url, "http://localhost").pathname;
    if (path === "/api/status") {
      response.writeHead(200, { "Content-Type": "application/json", "Cache-Control": "no-store" });
      return response.end(JSON.stringify(previewState()));
    }
    if (path === "/api/action") {
      response.writeHead(409, { "Content-Type": "application/json" });
      return response.end(JSON.stringify({ error: "Đây là bản xem trước. Không có thay đổi nào được gửi tới server thật." }));
    }
    if (!files[path]) { response.writeHead(404); return response.end("Not found"); }
    const [filename, type] = files[path];
    response.writeHead(200, { "Content-Type": type + "; charset=utf-8", "Cache-Control": "no-store" });
    response.end(await readFile(new URL(filename, import.meta.url)));
  } catch {
    response.writeHead(500); response.end("Preview error");
  }
});
server.listen(8788, "127.0.0.1", () => console.log("Immich Control preview: http://127.0.0.1:8788"));
