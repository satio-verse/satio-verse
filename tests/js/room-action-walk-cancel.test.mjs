import { test } from 'node:test';
import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import { register } from 'node:module';

const THREE_URL = new URL('../../web/vendor/three/build/three.module.js', import.meta.url).href;
const STATIC_URL = new URL('../../web/', import.meta.url).href;
const PET_URL = new URL('../../desktop/web/', import.meta.url).href;
register(`data:text/javascript,${encodeURIComponent(`export async function resolve(s,c,n) {
  if (s === 'three') return {url:${JSON.stringify(THREE_URL)},shortCircuit:true};
  if (s.startsWith('/static/')) return {url:new URL(s.slice(8),${JSON.stringify(STATIC_URL)}).href,shortCircuit:true};
  if (s.startsWith('/pet/')) return {url:new URL(s.slice(5),${JSON.stringify(PET_URL)}).href,shortCircuit:true};
  return n(s,c); }`)}`);
globalThis.location = { search: '' };
const source = await readFile(new URL('../../desktop/web/room-acts.js', import.meta.url), 'utf8');
const { Life, SCRIPTS } = await import(`data:text/javascript;base64,${Buffer.from(source + '\nexport { SCRIPTS };').toString('base64')}`);
const delay = ms => new Promise(resolve => setTimeout(resolve, ms));
const flush = () => new Promise(resolve => setImmediate(resolve));
function fixture(name, script, module = { Life, SCRIPTS }) {
  const events = [], arrivals = [];
  module.SCRIPTS[name] = script;
  const layer = {
    grid: {x0:0,z0:0,w:2,h:2,cell:1,open:new Uint8Array([1,1,1,1])}, mode:'idle', path:null,
    _walkTo(goal, options) { this.mode='go';this.path=[goal];arrivals.push(options.after);events.push('walk');return true; },
    run(step, done) { this.mode='go';this.path=[[1,1]];arrivals.push(() => done(null));events.push(step.do); },
    stop() { this.path=null;this.mode='idle';events.push('stop'); },
  };
  const r = { scene:{life:[{id:'coffee',name:'合成测试',script:name}]}, busy:()=>false, props:{fed:[]}, layer,
    needs:{stock:{beans:1},v:{energy:80},fatigue:0,worst:()=>({gap:0}),save(){}}, body:{speed:1,injured:false,log:[]},
    practice:{history:[],skills:{}},status(){},log(){} };
  const life = new module.Life(r);life.fast=true;life.start=()=>{};life._ask=async()=>null;
  return {life,layer,events,arrivals};
}

for (const route of ['walk','go']) test(`${route}: cancelling an in-flight walk stops its target and settles without the lost arrival callback`, async () => {
  const f = fixture('__cancel_walk_'+route, async ctx => {
    if(route==='walk')await ctx._walk([.5,.5]);else await ctx.run({do:'go',to:'target'});
    f.events.push('forward');
  });
  let terminal;const p=f.life.requests.request('coffee',{requestId:'A'});p.then(out=>{terminal=out;});await flush();
  assert.equal(f.arrivals.length,1);f.life.requests.stop('停止');await delay(100);
  const observed={terminal,path:f.layer.path,stops:f.events.filter(x=>x==='stop').length};
  // Always settle the unfixed implementation too, so the red test does not wait for STEP_S.
  f.arrivals[0]();await p;await flush();
  assert.equal(observed.path,null);assert.equal(observed.stops,1);assert.equal(observed.terminal?.outcome,'cancelled');
  assert.ok(!f.events.includes('forward'));assert.equal(f.life.requests.active,null);
});

test('the same cancellation cannot interrupt the normal cleanup walk or a later request', async () => {
  const f=fixture('__cancel_cleanup_walk',async ctx=>{
    ctx.later(async()=>{await ctx._walk([1,1]);f.events.push('cleanup-completed');});
    await ctx._walk([.5,.5]);
  });
  let first;const p=f.life.requests.request('coffee',{requestId:'A'});p.then(out=>{first=out;});await flush();
  f.life.requests.stop('停止');await delay(100);
  const interruptedBeforeOldCallback=f.arrivals.length===2;
  if(!interruptedBeforeOldCallback){f.arrivals[0]();await flush();}
  assert.equal(f.arrivals.length,2);assert.equal(first,undefined);
  f.life.requests.stop('重复停止');f.arrivals[0]();await delay(80);
  const beforeCleanupFinish={first,path:f.layer.path,stops:f.events.filter(x=>x==='stop').length};
  f.arrivals[1]();await p;await flush();
  assert.equal(interruptedBeforeOldCallback,true);assert.equal(beforeCleanupFinish.first,undefined);assert.notEqual(beforeCleanupFinish.path,null);
  assert.equal(beforeCleanupFinish.stops,1);assert.ok(f.events.includes('cleanup-completed'));
  assert.equal(first.outcome,'cancelled');assert.equal(first.cleanupConfirmed,true);
});

for(const route of ['walk','go'])test(`${route}: stop during stand-up never starts the deferred old walking goal`,async()=>{
  let standDone;const f=fixture('__stand_cancel_'+route,async ctx=>{
    if(route==='walk')await ctx._walk([.5,.5]);else await ctx.run({do:'go',to:'target'});
  });
  f.layer.seated={};f.layer.action={};
  f.layer.standUp=done=>{standDone=()=>{f.layer.seated=null;f.layer.action=null;done();};};
  // Real Locomotion.run(go) stands before its goTo callback.
  f.layer.run=(step,done)=>{
    const begin=()=>{f.events.push('old-goal');f.arrivals.push(()=>done(null));};
    if(f.layer.seated)f.layer.standUp(begin);else begin();
  };
  const p=f.life.requests.request('coffee');await flush();assert.equal(typeof standDone,'function');
  f.life.requests.stop('站起中停止');await delay(70);assert.equal(f.events.includes('stop'),false,'do not abort a still-active stand-up clip');
  standDone();await flush();
  const beforeArrival=[...f.events];for(const done of f.arrivals)done();const out=await p;await flush();
  assert.ok(!beforeArrival.includes('old-goal'));assert.equal(out.outcome,'cancelled');
});

test('autonomous walking without a request is not stopped by request cancellation',async()=>{
  const f=fixture('__autonomous_walk',ctx=>ctx._walk([.5,.5]));
  assert.equal(f.life.goDo('coffee'),true);await flush();f.life.requests.stop('没有对应请求');await delay(100);
  assert.equal(f.events.includes('stop'),false);assert.notEqual(f.layer.path,null);
  f.arrivals[0]();await flush();await flush();assert.equal(f.life.done[0].ok,true);
});

test('completed walking clears cancellation watching before the next request starts',async()=>{
  const f=fixture('__completed_walk',ctx=>ctx._walk([.5,.5]));
  const a=f.life.requests.request('coffee',{requestId:'A'});await flush();f.arrivals[0]();await a;await flush();
  const b=f.life.requests.request('coffee',{requestId:'B'});await flush();await delay(100);
  assert.equal(f.events.includes('stop'),false);assert.equal(f.life.requests.active.id,'B');
  f.arrivals[1]();const out=await b;assert.equal(out.ok,true);
});

test('late arrival from a cancelled walk cannot stop or complete a later request',async()=>{
  const f=fixture('__late_cancelled_arrival',ctx=>ctx._walk([.5,.5]));
  const a=f.life.requests.request('coffee',{requestId:'A'});await flush();f.life.requests.stop('停止A');await a;await flush();
  const b=f.life.requests.request('coffee',{requestId:'B'});await flush();
  f.arrivals[0]();await delay(100);
  assert.equal(f.life.requests.active.id,'B');assert.notEqual(f.layer.path,null);assert.equal(f.events.filter(x=>x==='stop').length,1);
  f.arrivals[1]();assert.equal((await b).ok,true);
});

test('a stop adapter failure is not reported as confirmed cleanup',async()=>{
  const f=fixture('__stop_adapter_failure',ctx=>ctx._walk([.5,.5]));
  f.layer.stop=()=>{throw new Error('synthetic stop failure');};
  const p=f.life.requests.request('coffee',{requestId:'A'});await flush();f.life.requests.stop('停止');
  const out=await p;await flush();
  assert.equal(out.cleanupConfirmed,false);assert.equal(out.outcome,'cancel_unconfirmed');assert.equal(out.completed,false);
  assert.equal(f.life.requests.blocked,true);assert.match(f.life.recoveryError,/停止行走未确认/);
  f.arrivals[0]();await flush();assert.equal(f.life.requests.active.id,'A');
});

// Only this isolated module copy shortens STEP_S; live runtime keeps its 45 s boundary.
assert.equal((source.match(/const STEP_S = 45;/g)||[]).length,1);
const deadlineModule=await import(`data:text/javascript;base64,${Buffer.from(source.replace('const STEP_S = 45;','const STEP_S = 0.08;')+'\nexport { SCRIPTS };').toString('base64')}`);
for(const route of ['walk','go'])for(const cancel of [false,true])test(`${route}: timed-out stand continuation cannot replace a fresh cleanup walk (cancel=${cancel})`,async()=>{
  let oldStand, started;const cleanupStarted=new Promise(resolve=>{started=resolve;});
  const f=fixture('__timeout_cleanup_'+route+cancel,async ctx=>{
    ctx.later(async()=>{
      f.layer.seated=null;f.layer.action=null; // Physical collaborator now permits the cleanup's independent walking step.
      const cleanup=ctx._walk([1,1]);started();await cleanup;f.events.push('cleanup-done');
    });
    if(route==='walk')await ctx._walk([.5,.5]);else await ctx.run({do:'go',to:'old-target'});
  },deadlineModule);
  f.layer.seated={};f.layer.action={};f.layer.standUp=done=>{oldStand=done;};
  const realStop=f.layer.stop;f.layer.stop=function(){if(this.action)return;realStop.call(this);};
  f.layer.run=(_step,done)=>{f.events.push('old-goal');f.arrivals.push(()=>done(null));};
  const p=f.life.requests.request('coffee',{requestId:'A'});await flush();if(cancel)f.life.requests.stop('取消');
  await cleanupStarted;const before={count:f.arrivals.length,path:JSON.stringify(f.layer.path)};
  oldStand();oldStand();await flush();
  const after={count:f.arrivals.length,path:JSON.stringify(f.layer.path),old:f.events.includes('old-goal')};
  for(const done of f.arrivals)done();const out=await p;await flush();
  assert.equal(after.count,before.count);assert.equal(after.path,before.path);assert.equal(after.old,false);
  assert.ok(f.events.includes('cleanup-done'));assert.equal(out.ok,false);assert.equal(out.cleanupConfirmed,false);
  assert.equal(f.life.requests.blocked,true,'the timed-out physical stand was not confirmed by stop');
});

test('a timed-out physical stand retains its lease and blocks B instead of assuming completion',async()=>{
  let oldStand;const f=fixture('__timeout_then_B',ctx=>ctx._walk([.5,.5]),deadlineModule);
  f.layer.seated={};f.layer.action={};f.layer.standUp=done=>{oldStand=done;};
  const realStop=f.layer.stop;f.layer.stop=function(){if(this.action)return;realStop.call(this);};
  const a=f.life.requests.request('coffee',{requestId:'A'});const out=await a;await flush();
  assert.equal(out.cleanupConfirmed,false);assert.equal(f.life.requests.blocked,true);assert.ok(f.layer.action);
  const b=await f.life.requests.request('coffee',{requestId:'B'});
  oldStand();await flush();assert.equal(f.arrivals.length,0);assert.equal(f.life.requests.active.id,'A');
  assert.equal(b.started,false);assert.equal(b.ok,false);
});

test('a throwing stop at the step deadline still settles as unconfirmed instead of leaving a pending Promise',async()=>{
  const f=fixture('__deadline_stop_throw',ctx=>ctx._walk([.5,.5]),deadlineModule);
  f.layer.stop=()=>{throw new Error('deadline adapter failed');};
  const out=await f.life.requests.request('coffee',{requestId:'A'});await flush();
  assert.equal(out.ok,false);assert.equal(out.cleanupConfirmed,false);assert.equal(f.life.requests.blocked,true);
  assert.match(f.life.recoveryError,/deadline adapter failed/);f.arrivals[0]();
});

test('run forwards a typed Error by identity with its code and cause intact',async()=>{
  const cause=new Error('synthetic palm mismatch');
  const original=Object.assign(new Error('杯子接触尚未到位',{cause}),{code:'CUP_PICK_CONTACT'});
  let caught;
  const f=fixture('__typed_run_error',async ctx=>{
    try{await ctx.run({do:'pick',to:'cup'});}catch(error){caught=error;throw error;}
  });
  f.layer.run=(_step,done)=>{done(original);done(null);};
  const out=await f.life.requests.request('coffee',{requestId:'typed-error'});await flush();
  assert.equal(caught,original);assert.equal(caught.code,'CUP_PICK_CONTACT');assert.equal(caught.cause,cause);
  assert.equal(out.ok,false);assert.equal(out.completed,false);assert.equal(out.reason,original.message);
});

test('run keeps string error callbacks compatible with Error rejection',async()=>{
  let caught;
  const f=fixture('__string_run_error',async ctx=>{
    try{await ctx.run({do:'pick',to:'cup'});}catch(error){caught=error;throw error;}
  });
  f.layer.run=(_step,done)=>done('普通字符串错误');
  const out=await f.life.requests.request('coffee',{requestId:'string-error'});await flush();
  assert.ok(caught instanceof Error);assert.equal(caught.message,'普通字符串错误');assert.equal(caught.code,undefined);
  assert.equal(out.ok,false);assert.equal(out.reason,'普通字符串错误');
});

test('a late typed run callback after cancellation cannot settle or fail the next request',async()=>{
  const callbacks=[];const f=fixture('__late_typed_run_error',ctx=>ctx.run({do:'go',to:'target'}));
  f.layer.run=(step,done)=>{f.layer.mode='go';f.layer.path=[[1,1]];callbacks.push(done);f.events.push(step.do);};
  const a=f.life.requests.request('coffee',{requestId:'A'});await flush();f.life.requests.stop('停止A');
  const first=await a;await flush();assert.equal(first.outcome,'cancelled');
  let second;const b=f.life.requests.request('coffee',{requestId:'B'});b.then(out=>{second=out;});await flush();
  const late=Object.assign(new Error('旧取物已取消'),{code:'CUP_PICK_CANCELLED'});
  assert.doesNotThrow(()=>{callbacks[0](late);callbacks[0](null);callbacks[0](late);});await flush();
  assert.equal(second,undefined);assert.equal(f.life.requests.active.id,'B');assert.notEqual(f.layer.path,null);
  assert.equal(f.events.filter(x=>x==='stop').length,1);
  f.layer.path=null;f.layer.mode='idle';callbacks[1](null);
  assert.equal((await b).ok,true);assert.equal(await f.life.requests.request('coffee',{requestId:'A'}),first);
});

// Synthetic collaborator follows the real cup task protocol; requests and Life/Ctx are real.
function cupFixture(name,script,module={Life,SCRIPTS}) {
  const f=fixture(name,script,module);f.tasks=[];f.cupCancels=[];f.pickPhase='clip';
  f.layer.items={cup:{id:'cup',held:null},cleanupCup:{id:'cleanupCup',held:null}};
  f.layer.run=(step,done)=>{
    assert.equal(step.do,'pick');const task={item:f.layer.items[step.to],bound:false,resolved:false,finished:false};
    task.finish=()=>{
      if(task.finished)return;task.finished=true;
      if(f.layer._cupPickTask===task){f.layer._cupPickTask=null;f.layer.path=null;f.layer.action=null;}
      done(task.bound?null:task.error||Object.assign(new Error('未绑定'),{code:'CUP_PICK_CONTACT'}));
    };
    task.finalize=()=>{if(task.resolved||task.finished)return;if(task.isOperationActive?.()===false){f.layer._cancelCupPick('owner inactive',task);return;}task.bound=task.resolved=true;task.item.held='right';};
    task.arrive=()=>{if(f.layer._cupPickTask!==task||task.resolved||task.finished)return;if(task.isOperationActive?.()===false){f.layer._cancelCupPick('owner inactive',task);return;}f.layer.path=null;f.layer.action={cupTask:task};};
    f.layer._cupPickTask=task;f.tasks.push(task);
    if(f.pickPhase==='approach')f.layer.path=[[1,1]];else f.layer.action={cupTask:task};
  };
  f.layer._cancelCupPick=(reason,expected)=>{
    const task=f.layer._cupPickTask;
    if(!task||(expected&&task!==expected)||task.bound||task.finished)return false;
    f.cupCancels.push({task,expected,reason});task.resolved=true;task.error=Object.assign(new Error(reason),{code:'CUP_PICK_CANCELLED'});
    if(f.layer.action?.cupTask!==task){f.layer.path=null;task.finish();}
    return true;
  };
  f.layer.stop=()=>{f.layer._cancelCupPick('layer stop');if(f.layer.action)return;f.layer.path=null;f.events.push('stop');};
  return f;
}

test('request cancellation revokes its cup approach before an arrival can start binding',async()=>{
  const f=cupFixture('__cup_cancel_approach',ctx=>ctx.run({do:'pick',to:'cup'}));f.pickPhase='approach';
  let terminal;const p=f.life.requests.request('coffee',{requestId:'A'});p.then(out=>{terminal=out;});await flush();
  const task=f.tasks[0];f.life.requests.stop('停止取杯');await delay(70);
  const observed={resolved:task.resolved,finished:task.finished,path:f.layer.path,terminal};
  task.error=Object.assign(new Error('red-test cleanup'),{code:'CUP_PICK_CANCELLED'});task.finish();await p;
  task.arrive();task.finalize();
  assert.equal(observed.resolved,true);assert.equal(observed.finished,true);assert.equal(observed.path,null);
  assert.equal(observed.terminal?.outcome,'cancelled');assert.equal(task.item.held,null);
  assert.equal(f.cupCancels[0].expected,task);
});

test('cup cancellation waits for the clip and does not cancel a fresh cleanup pick',async()=>{
  let ctxRef;const f=cupFixture('__cup_cancel_clip_cleanup',async ctx=>{
    ctxRef=ctx;ctx.later(()=>ctx.run({do:'pick',to:'cleanupCup'}));await ctx.run({do:'pick',to:'cup'});
  });
  let terminal;const p=f.life.requests.request('coffee',{requestId:'A'});p.then(out=>{terminal=out;});await flush();
  const task=f.tasks[0];f.life.requests.stop('停止取杯');await delay(70);
  const revoked=task.resolved;const busy=await f.life.requests.request('coffee',{requestId:'B'});
  assert.equal(terminal,undefined);assert.equal(ctxRef.cleaning,false);assert.equal(busy.started,false);
  task.error??=new Error('red-test cleanup');task.finish();await flush();
  const cleanup=f.tasks[1];assert.ok(cleanup);assert.equal(ctxRef.cleaning,true);
  f.life.requests.stop('重复停止');await delay(70);const cleanupRevoked=cleanup.resolved;
  cleanup.finalize();cleanup.finish();const out=await p;
  assert.equal(revoked,true);assert.equal(cleanupRevoked,false);assert.equal(out.outcome,'cancelled');assert.equal(out.cleanupConfirmed,true);
  assert.equal(f.cupCancels.every(row=>row.expected===task),true);
});

test('cancellation preserves an already bound cup for ordinary cleanup',async()=>{
  const f=cupFixture('__cup_cancel_bound',async ctx=>{
    ctx.later(()=>{f.layer.items.cup.held=null;f.events.push('ordinary-put');});await ctx.run({do:'pick',to:'cup'});
  });
  const p=f.life.requests.request('coffee',{requestId:'A'});await flush();const task=f.tasks[0];task.finalize();
  f.life.requests.stop('拿到后停止');await delay(70);
  assert.equal(task.item.held,'right');assert.equal(task.error,undefined);assert.equal(f.cupCancels.length,0);
  task.finish();const out=await p;assert.equal(out.outcome,'cancelled');assert.equal(out.cleanupConfirmed,true);
  assert.equal(task.item.held,null);assert.ok(f.events.includes('ordinary-put'));
});

test('timed-out cup binding is revoked but the unfinished clip keeps the request blocked',async()=>{
  const f=cupFixture('__cup_timeout',ctx=>ctx.run({do:'pick',to:'cup'}),deadlineModule);
  const p=f.life.requests.request('coffee',{requestId:'A'});await flush();const task=f.tasks[0];
  const out=await p;assert.equal(task.resolved,true);assert.ok(f.layer.action);assert.equal(out.cleanupConfirmed,false);
  assert.equal(f.life.requests.blocked,true);assert.equal((await f.life.requests.request('coffee',{requestId:'B'})).started,false);
  assert.equal(f.cupCancels[0].expected,task,'operation revokes its task before generic layer.stop');
  task.finalize();task.finish();await flush();assert.equal(task.item.held,null);assert.equal(f.life.requests.active.id,'A');
});

test('cancelled cup A late arrival and finish cannot revoke or settle cup B',async()=>{
  const f=cupFixture('__cup_late_A',ctx=>ctx.run({do:'pick',to:'cup'}));f.pickPhase='approach';
  const a=f.life.requests.request('coffee',{requestId:'A'});await flush();const old=f.tasks[0];
  f.life.requests.stop('停止A');await delay(70);const revoked=old.resolved;
  old.error??=new Error('red-test cleanup');old.finish();await a;await flush();
  f.pickPhase='clip';let done;const b=f.life.requests.request('coffee',{requestId:'B'});b.then(out=>{done=out;});await flush();
  const next=f.tasks[1];old.arrive();old.finalize();old.finish();await delay(70);
  assert.equal(done,undefined);assert.equal(next.resolved,false);assert.equal(f.layer._cupPickTask,next);assert.equal(f.life.requests.active.id,'B');
  next.finalize();next.finish();assert.equal((await b).ok,true);assert.equal(revoked,true);
});

test('cancel during stand-up before pick never creates a deferred cup task',async()=>{
  let standDone;const f=cupFixture('__cup_stand_cancel',ctx=>ctx.run({do:'pick',to:'cup'}));
  f.layer.seated={};f.layer.action={};f.layer.standUp=done=>{standDone=()=>{f.layer.seated=null;f.layer.action=null;done();};};
  const pick=f.layer.run;f.layer.run=(step,done)=>{if(f.layer.seated)f.layer.standUp(()=>pick(step,done));else pick(step,done);};
  const p=f.life.requests.request('coffee',{requestId:'A'});await flush();f.life.requests.stop('站起中停止');await delay(50);
  assert.equal(f.tasks.length,0);standDone();await flush();const deferred=f.tasks.length;
  for(const task of f.tasks){task.error=new Error('red-test cleanup');task.finish();}const out=await p;
  assert.equal(deferred,0);assert.equal(out.outcome,'cancelled');assert.equal(out.cleanupConfirmed,true);
});

test('same-turn stop denies final cup binding before the cancellation polling interval',async()=>{
  const f=cupFixture('__cup_same_turn_cancel',ctx=>ctx.run({do:'pick',to:'cup'}));
  const p=f.life.requests.request('coffee',{requestId:'A'});await flush();const task=f.tasks[0];
  f.life.requests.stop('本帧停止');task.finalize();
  const observed={bound:task.bound,held:task.item.held,lease:f.life.requests.active?.id,guard:typeof task.isOperationActive};
  task.finish();const out=await p;
  assert.equal(observed.guard,'function');assert.equal(observed.bound,false);assert.equal(observed.held,null);
  assert.equal(observed.lease,'A');assert.equal(out.outcome,'cancelled');assert.equal(out.cleanupConfirmed,true);
});

function cupPutFixture(name,script,module={Life,SCRIPTS}) {
  const f=fixture(name,script,module);f.tasks=[];f.putCancels=[];
  const cup={id:'cup',held:'right',grip:'cup',size:{x:.08,y:.1,z:.08}};
  f.layer.items={cup};f.layer.targets={home:{itemId:'cup'}};
  f.layer.run=(step,done)=>{
    assert.equal(step.do,'put');const item=f.layer.items[f.layer.targets[step.to]?.itemId||'cup'];
    const task={kind:'put',requestedTo:step.to,item,released:false,resolved:false,finished:false};
    task.finish=()=>{
      if(task.finished)return;task.finished=true;
      if(f.layer._cupPutTask===task){f.layer._cupPutTask=null;f.layer.path=null;f.layer.action=null;}
      done(task.released?null:task.error||Object.assign(new Error('杯子未到放回位置'),{code:'CUP_PUT_CONTACT'}));
    };
    task.finalize=()=>{
      if(task.resolved||task.finished)return;
      if(task.isOperationActive?.()===false){f.layer._cancelCupPut('owner inactive',task);return;}
      task.released=task.resolved=true;item.held=null;
    };
    f.layer._cupPutTask=task;f.layer.action={cupTask:task};f.tasks.push(task);
  };
  f.layer._cancelCupPut=(reason,expected)=>{
    const task=f.layer._cupPutTask;
    if(!task||(expected&&expected!==task)||task.released||task.finished)return false;
    f.putCancels.push({task,expected});task.resolved=true;task.error=Object.assign(new Error(reason),{code:'CUP_PUT_CANCELLED'});
    if(f.layer.action?.cupTask!==task){f.layer.path=null;task.finish();}return true;
  };
  f.layer.stop=()=>{f.layer._cancelCupPut('layer stop');if(f.layer.action)return;f.layer.path=null;};
  return f;
}

test('normal cancellation permits the current put to release safely but prevents later script steps',async()=>{
  const f=cupPutFixture('__put_safe_cancel',async ctx=>{await ctx.run({do:'put',to:'home'});f.events.push('forward');});
  let terminal;const p=f.life.requests.request('coffee',{requestId:'A'});p.then(out=>{terminal=out;});await flush();
  const task=f.tasks[0];f.life.requests.stop('停止但先放好');await delay(70);
  const allowed=task.isOperationActive?.();assert.equal(terminal,undefined);assert.equal(task.item.held,'right');
  assert.equal((await f.life.requests.request('coffee',{requestId:'B'})).started,false);
  task.finalize();assert.equal(task.item.held,null);task.finish();const out=await p;
  assert.equal(allowed,true);assert.equal(f.putCancels.length,0);assert.equal(out.outcome,'cancelled');
  assert.equal(out.cleanupConfirmed,true);assert.ok(!f.events.includes('forward'));
});

test('a cleanup put remains authorized despite the original request already being cancelled',async()=>{
  const f=cupPutFixture('__put_during_cleanup',async ctx=>{ctx.later(()=>ctx.run({do:'put',to:'home'}));await ctx.wait(10);});
  const p=f.life.requests.request('coffee',{requestId:'A'});await flush();f.life.requests.stop('停止');await delay(120);
  const task=f.tasks[0];assert.ok(task);const allowed=task.isOperationActive?.();
  f.life.requests.stop('重复停止');task.finalize();task.finish();const out=await p;
  assert.equal(allowed,true);assert.equal(out.cleanupConfirmed,true);assert.equal(task.item.held,null);
});

test('a failed final put gate keeps held ownership and reports cleanup unconfirmed even without undo',async()=>{
  const f=cupPutFixture('__put_contact_failure',ctx=>ctx.run({do:'put',to:'home'}));
  const p=f.life.requests.request('coffee',{requestId:'A'});await flush();const task=f.tasks[0];task.finish();const out=await p;
  assert.equal(task.item.held,'right');assert.equal(out.ok,false);assert.equal(out.cleanupConfirmed,false);
  assert.equal(f.life.requests.blocked,true);assert.ok(f.life.recoveryError);
  assert.equal((await f.life.requests.request('coffee',{requestId:'B'})).started,false);
});

test('successful cleanup retry clears only its put obligation and leaves another held cup alone',async()=>{
  const f=cupPutFixture('__put_retry',async ctx=>{ctx.later(()=>ctx.run({do:'put',to:'home'}));await ctx.run({do:'put',to:'home'});});
  f.layer.items.other={id:'other',held:'left',grip:'cup',size:{x:.08,y:.1,z:.08}};
  const p=f.life.requests.request('coffee',{requestId:'A'});await flush();const old=f.tasks[0];old.finish();await flush();
  const next=f.tasks[1];assert.ok(next);const oldActive=old.isOperationActive?.(),newActive=next.isOperationActive?.();
  old.finalize();old.finish();assert.equal(next.item.held,'right');next.finalize();next.finish();const out=await p;
  assert.equal(oldActive,false);assert.equal(newActive,true);assert.equal(out.ok,false);assert.equal(out.cleanupConfirmed,true);
  assert.equal(f.life.requests.active,null);assert.equal(f.layer.items.other.held,'left');
});

test('put deadline revokes late release and retains the held cup and execution lease',async()=>{
  const f=cupPutFixture('__put_timeout',ctx=>ctx.run({do:'put',to:'home'}),deadlineModule);
  const p=f.life.requests.request('coffee',{requestId:'A'});await flush();const task=f.tasks[0];const out=await p;
  assert.equal(task.resolved,true);assert.equal(task.item.held,'right');assert.equal(out.cleanupConfirmed,false);
  assert.equal(f.putCancels[0].expected,task);assert.equal(task.isOperationActive(),false);
  task.finalize();task.finish();await flush();assert.equal(task.item.held,'right');
  assert.equal(f.life.requests.active.id,'A');assert.equal((await f.life.requests.request('coffee',{requestId:'B'})).started,false);
});

for(const timeout of [false,true])test(`put stand continuation respects operation lifetime while normal cancellation permits return (timeout=${timeout})`,async()=>{
  let standDone;const f=cupPutFixture('__put_stand_'+timeout,ctx=>ctx.run({do:'put',to:'home'}),timeout?deadlineModule:{Life,SCRIPTS});
  f.layer.seated={};f.layer.action={};f.layer.standUp=done=>{standDone=()=>{f.layer.seated=null;f.layer.action=null;done();};};
  const put=f.layer.run;f.layer.run=(step,done)=>{if(f.layer.seated)f.layer.standUp(()=>put(step,done));else put(step,done);};
  const p=f.life.requests.request('coffee',{requestId:'A'});await flush();
  if(timeout){const out=await p;standDone();await flush();const count=f.tasks.length;for(const t of f.tasks)t.finish();
    assert.equal(count,0);assert.equal(out.cleanupConfirmed,false);assert.equal(f.layer.items.cup.held,'right');}
  else{f.life.requests.stop('站起中停止，先放回');standDone();await flush();const task=f.tasks[0];assert.ok(task);
    const allowed=task.isOperationActive?.();task.finalize();task.finish();const out=await p;
    assert.equal(allowed,true);assert.equal(out.outcome,'cancelled');assert.equal(out.cleanupConfirmed,true);}
});
