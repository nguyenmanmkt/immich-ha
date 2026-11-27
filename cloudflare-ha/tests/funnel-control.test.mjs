import test from 'node:test';
import assert from 'node:assert/strict';
import { initialFunnelControl, setFunnel, reportFunnel, publicFunnelControl } from '../src/funnel-control.mjs';

test('Funnel switch requires matching fresh witness confirmation and rejects late reports',()=>{
  const c=initialFunnelControl();
  reportFunnel(c,{enabled:true,revision:0},1000);
  assert.equal(publicFunnelControl(c,1000).confirmed,true);
  reportFunnel(c,{enabled:true,revision:null},1050);
  assert.equal(publicFunnelControl(c,1050).confirmed,true);
  setFunnel(c,false,1100);
  reportFunnel(c,{enabled:true,revision:null},1150);
  assert.equal(publicFunnelControl(c,1100).confirmed,false);
  reportFunnel(c,{enabled:true,revision:0},1200);
  assert.equal(c.enabled,false);assert.equal(publicFunnelControl(c,1200).confirmed,false);
  reportFunnel(c,{enabled:false,revision:1},1300);
  assert.equal(publicFunnelControl(c,1300).confirmed,true);
  assert.equal(publicFunnelControl(c,92000).confirmed,false);
  assert.equal(publicFunnelControl(c,92000).actualEnabled,null);
  assert.throws(()=>setFunnel(c,'false',1400));
});
