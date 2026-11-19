"""Restore saved automation only after an enabled streaming cutover is healthy.

Also usable when the cutover's bounded wait finished before the next probe.
"""
import json
from pathlib import Path
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import deployment as m
def main():
    before=json.loads((m.ROOT/'generated/streaming-before-cutover.json').read_text())
    live=m.control()
    assert live['owner']==before['owner'] and live['epoch']==before['epoch'], 'Owner/epoch changed; inspect instead'
    assert live['ready'] and live['standbyEligible'] and live['primaryHealthy'] and live['backupHealthy']
    for node,a in live['agents'].items():
        assert live['serverTime']-a['at']<180000
        s=a['metrics']['postgresStreaming']
        assert s.get('available') is not False
        if node!=live['owner']:
            assert s['inRecovery'] and s['receiverConnected'] and s['pendingBytes']==0
        else:assert not s['inRecovery']
    m.control('set-recovery',enabled=before.get('recovery',{}).get('enabled',False))
    pref=before.get('preference',{})
    if pref.get('node'):m.control('set-preference',target=pref['node'],enabled=pref.get('enabled',False))
    m.control('set-auto',enabled=before.get('autoRequested',before.get('auto',False)))
    receipt=m.ROOT/'generated/streaming-cutover.json'
    value=json.loads(receipt.read_text()) if receipt.exists() else {}
    value.update(verified=True,source=live['owner'],epoch=live['epoch'],finalizedAt=live['serverTime'])
    receipt.write_text(json.dumps(value,indent=2))
    print('VERIFIED: streaming healthy, automation restored from pre-upgrade settings')


if __name__ == '__main__':
    main()
