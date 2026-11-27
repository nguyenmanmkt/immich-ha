export function initialFunnelControl() { return { enabled: true, revision: 0, report: null }; }
export function setFunnel(control, enabled, now) {
  if (typeof enabled !== 'boolean') throw new Error('Invalid Funnel setting');
  if (control.enabled !== enabled) { control.enabled = enabled; control.revision++; control.requestedAt = now; }
}
export function reportFunnel(control, report, now) {
  if (!report || ![true,false,null].includes(report.enabled)) throw new Error('Invalid Funnel report');
  // A fresh unchanged observation can retain an already acknowledged revision.
  // Never carry that acknowledgement over a new setting request.
  const previous=control.report;
  const revision=Number.isSafeInteger(report.revision) ? report.revision :
    previous?.revision===control.revision && previous.enabled===report.enabled && report.enabled===control.enabled && !report.error ? previous.revision : null;
  control.report = { enabled: report.enabled, revision,
    at: now, error: report.error ? String(report.error).slice(0,160) : null };
}
export function publicFunnelControl(control, now) {
  const r=control.report;
  const fresh=r && now>=r.at && now-r.at<=90000;
  return { enabled:control.enabled, revision:control.revision, actualEnabled:fresh?r.enabled:null,
    confirmed:!!fresh && r.revision===control.revision && r.enabled===control.enabled && !r.error,
    error:fresh?r.error:null, reportedAt:r?.at || null };
}
