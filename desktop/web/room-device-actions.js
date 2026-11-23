// V8 visible device interactions. No OS/network input, invented app content or overturn physics.
import * as THREE from 'three';
import { POSES } from './room-poses.js';
import { buildGrid, walkable, toCell } from '/static/nav.js';
import { createBodyPartBoxes } from '/static/body-collision-parts.js';

const UP = new THREE.Vector3(0, 1, 0), states = new WeakMap();
const PHONE = 'IR_Item_iPhone18ProMax', CHAIR = 'desk_chair';
const SOFT = { pillow: .05, bed: .05, armchair: .04 };
const wrap = a => Math.atan2(Math.sin(a), Math.cos(a));
const seconds = a => Math.max(2, Math.min(60, Number(a?.seconds) || a?.stay?.[0] || 12));
function requireV8(r) { if (r.scene?.sceneId !== 'casa-luce-v8') throw new Error('这个设备动作尚未适配此房间'); }
function node(r, id) { const n = r.props.node(id); if (!n) throw new Error(`设备节点缺失: ${id}`); n.updateWorldMatrix(true, true); return n; }
const center = n => new THREE.Box3().setFromObject(n).getCenter(new THREE.Vector3());
function emptyHands(r) { if (Object.values(r.layer.items).some(i => i.held)) throw new Error('请先放好手里的物件'); }
function record(r, action) { return (r.deviceLast = { action, phase: 'starting', ok: false, scope: 'room-animation', osInput: false }); }

// Immutable definitions; each pose's targets/options belong to its own room instance.
POSES.devicePhoneView = { halflife: .3, fk: { neck: [16, 0, 0], chest: [3, 0, 0] },
  ik: { hand: c => ({ target: c.bone('chest').addScaledVector(c.fwd, .26 * c.s).addScaledVector(UP, -.08 * c.s)
    .addScaledVector(c.left, .08 * c.side * c.s), hint: c.left.clone().multiplyScalar(c.side).addScaledVector(UP, -.7), palm: UP.clone().negate() }) } };
const typingHand = side => c => ({ target: c.opts[side].clone().addScaledVector(UP, .004 * (1 + Math.sin(c.t * 9 + (side === 'left' ? 0 : Math.PI)))),
  hint: c.left.clone().multiplyScalar(side === 'left' ? 1 : -1).addScaledVector(UP, -.6), palm: UP.clone().negate() });
POSES.deviceTyping = { halflife: .2, fk: { spine: [8, 0, 0], chest: [12, 0, 0], neck: [-4, 0, 0] },
  ik: { leftHand: typingHand('left'), rightHand: typingHand('right') } };

/** Rotate authored world AABBs about the floor pivot, recomputing corners from the original bounds.
 * Y up; radians; world column vectors. Never rotate an already expanded AABB repeatedly.
 */
export function rotatedBounds(box, pivot, radians) {
  if (![...box.min, ...box.max, ...pivot, radians].every(Number.isFinite)) throw new Error('椅子几何无效');
  const c = Math.cos(radians), s = Math.sin(radians), points = [];
  for (const x of [box.min[0], box.max[0]]) for (const z of [box.min[2], box.max[2]]) {
    const dx = x - pivot[0], dz = z - pivot[2]; points.push([pivot[0] + c * dx + s * dz, pivot[2] - s * dx + c * dz]);
  }
  return { ...box, min: [Math.min(...points.map(p => p[0])), box.min[1], Math.min(...points.map(p => p[1]))],
    max: [Math.max(...points.map(p => p[0])), box.max[1], Math.max(...points.map(p => p[1]))] };
}
function chairState(r) {
  requireV8(r);
  if (states.has(r)) return states.get(r);
  const n = node(r, 'Asset_Chair'), entry = r.scene.objects.find(o => o.id === CHAIR);
  const group = r.scene.collisionParts?.find(g => g.id === CHAIR);
  if (!entry?.seat || !group?.parts?.length) throw new Error('椅子的座位或细碰撞配置缺失');
  const scale = n.getWorldScale(new THREE.Vector3());
  if (scale.toArray().some(v => Math.abs(v - 1) > 1e-5)) throw new Error('椅子缩放尚未适配');
  const quaternion = n.getWorldQuaternion(new THREE.Quaternion()), fwd = new THREE.Vector3(0, 0, 1).applyQuaternion(quaternion);
  if (Math.abs(fwd.y) > 1e-5) throw new Error('椅子不是直立姿态');
  const state = { node: n, entry, group, baseGroup: structuredClone(group), pivot: n.getWorldPosition(new THREE.Vector3()).toArray(),
    baseQuaternion: quaternion, baseYaw: Math.atan2(fwd.x, fwd.z), yaw: Math.atan2(fwd.x, fwd.z) };
  state.basePivot=state.pivot.slice();state.seatOffset=[entry.seat.pos[0]-state.pivot[0],entry.seat.pos[1]-state.pivot[2]];
  states.set(r, state); return state;
}
function geometryAt(state, yaw) {
  const parts = state.baseGroup.parts.map(p => {
    const out=rotatedBounds(p,state.basePivot,wrap(yaw-state.baseYaw));
    for(let k=0;k<3;k++){out.min[k]+=state.pivot[k]-state.basePivot[k];out.max[k]+=state.pivot[k]-state.basePivot[k];}
    return out;
  });
  return { id: CHAIR, parts, min: [0, 1, 2].map(k => Math.min(...parts.map(p => p.min[k]))),
    max: [0, 1, 2].map(k => Math.max(...parts.map(p => p.max[k]))) };
}
function applyChair(r, yaw) {
  const s = chairState(r), g = geometryAt(s, yaw), delta = wrap(yaw - s.baseYaw);
  const worldQ = new THREE.Quaternion().setFromAxisAngle(UP, delta).multiply(s.baseQuaternion);
  const parentQ = s.node.parent?.getWorldQuaternion(new THREE.Quaternion()) || new THREE.Quaternion();
  s.node.position.copy(s.node.parent ? s.node.parent.worldToLocal(new THREE.Vector3(...s.pivot)) : new THREE.Vector3(...s.pivot));
  s.node.quaternion.copy(parentQ.invert().multiply(worldQ)); s.node.updateMatrixWorld(true);
  Object.assign(s.group, g); s.entry.seat.yaw = wrap(yaw);
  s.entry.seat.pos=[s.pivot[0]+s.seatOffset[0],s.pivot[2]+s.seatOffset[1]];
  s.entry.box = { ...s.entry.box, center: [(g.min[0] + g.max[0]) / 2, (g.min[2] + g.max[2]) / 2],
    size: [g.max[0] - g.min[0], g.max[2] - g.min[2]], base: g.min[1], height: g.max[1] - g.min[1] };
  r.layer.targets[CHAIR] = s.entry;
  r.layer.grid = buildGrid(r.scene);
  const avatar = r.layer.avatar || r.v;
  if (avatar) avatar._collision = null;
  if (r.body) r.body._partBoxes = createBodyPartBoxes(r.scene, SOFT);
  if (r.rigid?.layer) {
    const w = r.rigid.world;
    for (const id of [...w.items.keys()]) if (id.startsWith(CHAIR + '#')) w.remove(id);
    g.parts.forEach((p, k) => w.addFixedBox(`${CHAIR}#${k}`, p.min, p.max));
  }
  s.yaw = wrap(yaw); r.props.cache.dirty = true;
}

/** Call once after Props/Body exist. Profile seat yaw is the desired front direction.
 * The authored Asset_Chair front is +Z transformed by its world quaternion.
 * Restoring changes only the visual chair plus all matching collision/navigation representations.
 */
export function prepareDeviceRoom(r) {
  if (r.scene?.sceneId !== 'casa-luce-v8') return false;
  const s = chairState(r), saved = r.props.layout.devices?.chairYaw;
  applyChair(r, Number.isFinite(saved) ? saved : s.entry.seat.yaw);
  return true;
}

function chairSweepClear(r, state, target, char = r.layer.char) {
  const delta = wrap(target - state.yaw), other = r.scene.collisionParts.filter(g => g.id !== CHAIR);
  for (let i = 0; i <= 24; i++) {
    const g = geometryAt(state, state.yaw + delta * i / 24);
    for (const p of g.parts) {
      if (char) {
        const x = Math.max(p.min[0], Math.min(p.max[0], char.x)), z = Math.max(p.min[2], Math.min(p.max[2], char.z));
        if (Math.hypot(x - char.x, z - char.z) < (r.scene.agentRadius || .22)) return false;
      }
      for (const o of other) for (const q of o.parts) {
        if (p.min.every((v, k) => v < q.max[k] - .002 && p.max[k] > q.min[k] + .002)) return false;
      }
    }
  }
  return true;
}
async function approachChair(ctx, targetYaw) {
  const r=ctx.r,s=chairState(r),[x,z]=s.entry.approach;
  const out=new THREE.Vector3(x-s.pivot[0],0,z-s.pivot[2]).normalize();
  for(const extra of [.12,.2,.3]) {
    const at=[x+out.x*extra,z+out.z*extra];
    if(!walkable(r.layer.grid,...toCell(r.layer.grid,...at))||!chairSweepClear(r,s,targetYaw,{x:at[0],z:at[1]}))continue;
    await ctx.walk(at);
    if(chairSweepClear(r,s,targetYaw))return;
  }
  throw new Error('椅子旁边没有能安全扶着转动的站位');
}
function rotateRider(L,pivot,delta) {
  const rotate=p=>{const x=p.x-pivot[0],z=p.z-pivot[2],c=Math.cos(delta),s=Math.sin(delta);p.x=pivot[0]+c*x+s*z;p.z=pivot[2]-s*x+c*z;};
  rotate(L.char);L.char.yaw=wrap(L.char.yaw+delta);
  const seats=new Set([L.seated?.seat,L.action?.seat?.info].filter(Boolean));
  for(const seat of seats)seat.yaw=wrap(seat.yaw+delta);
  if(L.action?.anchor){rotate(L.action.anchor);L.action.anchor.yaw=wrap(L.action.anchor.yaw+delta);}
  for(const f of Object.values(L.feet||{}))for(const key of ['lock','seatT'])if(f[key]?.isVector3)rotate(f[key]);
  L.sim={...L.sim,x:L.char.x,z:L.char.z,vx:0,vz:0,ax:0,az:0};L.simRot={yaw:L.char.yaw,w:0};
}
function riderSweepClear(r,s,targetYaw,translation=new THREE.Vector3()) {
  const L=r.layer,scale=L.hipHeight/.78,points=[];
  for(const [name,radius] of [['head',.1],['hips',.11],['leftLowerLeg',.06],['rightLowerLeg',.06],['leftFoot',.05],['rightFoot',.05]]){
    const bone=L.nodes[name];if(!bone)return false;const p=bone.getWorldPosition(new THREE.Vector3());
    if(name==='head')p.add(new THREE.Vector3(0,.09*scale,0).applyQuaternion(bone.getWorldQuaternion(new THREE.Quaternion())));
    points.push({p,radius:radius*scale});
  }
  const delta=wrap(targetYaw-s.yaw),pivot=new THREE.Vector3(...s.pivot);
  const boxes=r.scene.collisionParts.filter(g=>g.id!==CHAIR).flatMap(g=>g.parts.map(p=>new THREE.Box3(new THREE.Vector3(...p.min),new THREE.Vector3(...p.max))));
  for(let k=0;k<=24;k++)for(const {p,radius} of points){const at=p.clone().sub(pivot).applyAxisAngle(UP,delta*k/24).add(pivot).addScaledVector(translation,k/24);
    if(boxes.some(b=>b.distanceToPoint(at)<radius-.015))return false;
  }
  return true;
}
export async function turnDeviceChair(ctx, targetYaw, onFrame = null, withRider = false) {
  const r = ctx.r, s = chairState(r);
  if (!Number.isFinite(targetYaw)) throw new Error('椅子角度无效');
  if (withRider) {
    if(r.layer.seated?.seat?.id!==CHAIR||!r.layer.action?.loop||r.layer.reaching)throw new Error('坐姿尚未稳定，不能连人调向');
    if(!riderSweepClear(r,s,targetYaw))throw new Error('转向时身体会碰到桌架，不能继续');
  } else if (r.layer.seated || r.layer.action || r.layer.reaching) throw new Error('请先离开椅子，等动作完成再调向');
  if (r.rigid) { await r.rigid.ready; if (r.rigid.failed || !r.rigid.layer || !r.rigid.world?.addFixedBox) throw new Error('刚体未就绪，不能转动椅子'); }
  if (!chairSweepClear(r, s, targetYaw, withRider ? null : r.layer.char)) throw new Error('椅子转动的空间被挡住了');
  const from = s.yaw, delta = wrap(targetYaw - from);
  r.props.toLive(s.node);
  let failure = null;
  try { await r.props.tween(1.4, t => {
    if (failure) return;
    try {
      if (ctx.hurry) throw new Error('椅子调向已停止');
      const next=from+delta*t,previous=s.yaw;
      applyChair(r, next);
      if(withRider)rotateRider(r.layer,s.pivot,wrap(next-previous));
      if (onFrame?.(s.yaw) === false) throw new Error('手离开了椅子，停止继续调向');
    } catch (error) { failure = error; }
  }); }
  finally { r.props.toRoom(s.node); }
  (r.props.layout.devices ??= {}).chairYaw = s.yaw; r.props.save();
  if (failure) throw failure;
  return s.yaw;
}

/** Roll the occupied chair continuously; every visual, body, navigation and rigid representation follows. */
export async function slideDeviceChair(ctx,to) {
  const r=ctx.r,L=r.layer,s=chairState(r),from=s.pivot.slice(),delta=new THREE.Vector3(...to).sub(new THREE.Vector3(...from));
  if(to.length!==3||!to.every(Number.isFinite)||Math.abs(delta.y)>1e-6||delta.length()>.4)throw new Error('椅子滚动目标无效');
  if(L.seated?.seat?.id!==CHAIR||!L.action?.loop)throw new Error('请先坐稳再滚动椅子');
  if(r.rigid){await r.rigid.ready;if(r.rigid.failed||!r.rigid.layer)throw new Error('刚体尚未就绪');}
  if(!riderSweepClear(r,s,s.yaw,delta))throw new Error('椅子滚动时身体会碰到家具');
  for(let k=0;k<=16;k++){
    const candidate={...s,pivot:from.map((v,j)=>v+delta.getComponent(j)*k/16)};
    if(!chairSweepClear(r,candidate,s.yaw,null))throw new Error('椅子滚动路线被家具挡住了');
  }
  let failure=null;r.props.toLive(s.node);
  try{await r.props.tween(1.6,t=>{
    if(failure)return;
    try{
      if(ctx.hurry)throw new Error('椅子滚动已停止');
      const next=from.map((v,j)=>v+delta.getComponent(j)*t),dx=next[0]-s.pivot[0],dz=next[2]-s.pivot[2];
      s.pivot=next;applyChair(r,s.yaw);L.char.x+=dx;L.char.z+=dz;
      for(const seat of new Set([L.seated.seat,L.action?.seat?.info].filter(Boolean)))seat.pos=[s.entry.seat.pos[0],s.entry.seat.pos[1]];
      if(L.action?.anchor){L.action.anchor.x+=dx;L.action.anchor.z+=dz;}
      for(const f of Object.values(L.feet||{}))for(const key of ['lock','seatT'])if(f[key]?.isVector3){f[key].x+=dx;f[key].z+=dz;}
      L.sim={...L.sim,x:L.char.x,z:L.char.z,vx:0,vz:0,ax:0,az:0};
    }catch(e){failure=e;}
  });}finally{r.props.toRoom(s.node);}
  if(failure)throw failure;
}

/** Inject existing helpers to avoid a module cycle with room-acts.js. */
export function createDeviceScripts({ grab, place, homeOf }) {
  return {
    async device_phone(ctx, a) {
      const r = ctx.r; requireV8(r); emptyHands(r); node(r, PHONE);
      const result = record(r, 'phone'), id = r.props.asItem(PHONE), item = r.layer.items[id];
      if (!item) throw new Error('手机没有可用的物件代理');
      item.grip = 'book';
      const home = homeOf(r, id, '手机原位', null), origin = item.mesh.position.clone(), homeYaw = r.props.glb[id].yawNow || 0;
      r.layer.targets[home].itemId = id;
      let returning = false;
      const returnHome = async () => {
        r.poses.set(null);
        if (returning || !item.held) return;
        returning = true;
        try {
          await place(ctx, home);
          if (!item.physical) {
            const g = r.props.glb[id], from = item.mesh.rotation.y, to = (g.proxyYaw0 || 0) + homeYaw;
            await r.props.tween(.45, t => { item.mesh.rotation.y = from + wrap(to - from) * t; });
          } else {
            // The rigid layer owns release; do not snap a still-moving physical phone to its home.
            for (let i = 0; i < 30 && (r.rigid?.layer?.grips[id] || item.mesh.position.distanceTo(origin) > .035); i++)
              await ctx.wait(.1, { hurry: false });
          }
          r.props.follow(); result.returnError = item.mesh.position.distanceTo(origin);
        }
        finally { returning = false; }
      };
      ctx.later(async () => { await returnHome(); r.props.settle(id); });
      ctx.say('她去拿桌上的手机'); r.props.wake(id);
      await grab(ctx, id);
      if (!item.held) throw new Error('手机没有实际拿到手里');
      r.layer.targets[home].approach = [r.layer.char.x, r.layer.char.z];
      result.phase = 'viewing'; result.hand = item.held;
      r.poses.set('devicePhoneView', { side: item.held });
      ctx.say('她拿着手机看了一会儿');
      const target = r.layer.targets.__devicePhone = { id: '__devicePhone', name: '手机屏幕', look: origin.toArray() };
      const off = r.props.every(() => { target.look = center(r.props.glb[id].node).toArray(); });
      ctx.later(off); r.layer.lookAt('__devicePhone', seconds(a) + 1);
      try { await ctx.wait(seconds(a)); } finally { off(); r.poses.set(null); }
      await ctx.wait(.7, { hurry: false }); result.phase = 'returning'; await returnHome();
      if (item.held || !(result.returnError < .035)) throw new Error('手机尚未归回原位');
      r.props.settle(id); result.phase = 'complete'; result.ok = true;
      ctx.life.actionDetail={text:'已在小窝拿起手机观看并放回原位；没有操作真实手机或应用。',scope:'room-animation',osInput:false};
    },
    async device_computer(ctx, a) {
      const r = ctx.r; requireV8(r); emptyHands(r);
      const keyboard = node(r, 'Asset_Keyboard'), screen = node(r, 'V8_Monitor_Preview');
      const result = record(r, 'computer');
      const s = chairState(r), screenAt = center(screen), facing = Math.atan2(screenAt.x - s.pivot[0], screenAt.z - s.pivot[2]);
      if (r.layer.seated) await ctx.run({ do: 'stand' });
      const boardingYaw=Math.PI/2;
      await approachChair(ctx,boardingYaw);
      if(Math.abs(wrap(s.yaw-boardingYaw))>.01)await turnDeviceChair(ctx,boardingYaw);
      await ctx.sit(CHAIR);
      if (!r.layer.seated) throw new Error('尚未坐到电脑椅上');
      let seated=true;const homePivot=s.pivot.slice();
      const leave=async()=>{if(!seated||!r.layer.seated)return;r.poses.set(null);await ctx.wait(.7,{hurry:false});
        if(Math.hypot(s.pivot[0]-homePivot[0],s.pivot[2]-homePivot[2])>.001)await slideDeviceChair({...ctx,r,hurry:false},homePivot);
        await turnDeviceChair({...ctx,r,hurry:false},boardingYaw,null,true);await ctx.run({do:'stand'});seated=false;};
      ctx.later(leave);
      result.phase='swivel_to_screen';await turnDeviceChair(ctx,facing,null,true);
      const box = new THREE.Box3().setFromObject(keyboard), middle = box.getCenter(new THREE.Vector3());
      // Targets are above the actual keyboard near edge; wrist motion is an input animation, not keypress delivery.
      const f = new THREE.Vector3(Math.sin(s.entry.seat.yaw), 0, Math.cos(s.entry.seat.yaw)), left = new THREE.Vector3(f.z, 0, -f.x);
      result.phase='roll_to_keyboard';await slideDeviceChair(ctx,homePivot.map((v,k)=>v+f.getComponent(k)*.35));
      const near = middle.clone().addScaledVector(f, -.065); near.y = box.max.y + .012;
      const palms = { left: near.clone().addScaledVector(left, .08), right: near.clone().addScaledVector(left, -.08) };
      const targets={left:palms.left.clone(),right:palms.right.clone()};
      const look = r.layer.targets.__deviceScreen = { id: '__deviceScreen', name: '场景显示器', look: screenAt.toArray() };
      result.phase = 'typing'; result.keyboardPalmTargets = Object.fromEntries(Object.entries(palms).map(([k, v]) => [k, v.toArray()])); result.screen = look.look;
      let streak=0,bestStreak=0;const gaps=[];
      const off=r.props.every(()=>{
        const current={};
        for(const side of ['left','right']){
          const palm=r.props._palm(side),wrist=r.layer.nodes[side+'Hand'].getWorldPosition(new THREE.Vector3());
          if(palm){targets[side].copy(palms[side]).sub(palm.C.clone().sub(wrist));current[side]=palm.C.distanceTo(palms[side]);}
        }
        if(r.poses.name==='deviceTyping'&&r.poses.t>1.5){gaps.push(current);streak=current.left<.06&&current.right<.06?streak+1:0;bestStreak=Math.max(bestStreak,streak);}
      });ctx.later(off);
      r.layer.lookAt('__deviceScreen', seconds(a) + 1); r.poses.set('deviceTyping', targets);
      ctx.later(() => { if (r.poses.name === 'deviceTyping') r.poses.set(null); });
      ctx.say('她坐在电脑前看屏幕、敲了敲键盘');
      try { await ctx.wait(seconds(a)); } finally { off(); r.poses.set(null); }
      result.keyboardContactFrames=bestStreak;result.keyboardHandGaps=gaps;
      result.phase='swivel_to_exit';await leave();
      if(bestStreak<12)throw new Error('双手还没有实际够到键盘，输入动作未完成');
      ctx.life.actionDetail={text:'已在小窝坐到电脑前、看屏并完成键盘输入动画；没有操作真实电脑。',scope:'room-animation',osInput:false};
      result.phase = 'complete'; result.ok = true;
    },
    async device_chair(ctx, a) {
      const r=ctx.r;requireV8(r);emptyHands(r);const result=record(r,'chair_turn'),s=chairState(r),boardingYaw=Math.PI/2;
      if(r.layer.seated)await ctx.run({do:'stand'});
      await approachChair(ctx,boardingYaw);
      if(Math.abs(wrap(s.yaw-boardingYaw))>.01)await turnDeviceChair(ctx,boardingYaw);
      await ctx.sit(CHAIR);
      let finished=false;
      ctx.later(async()=>{if(finished||!r.layer.seated)return;
        await turnDeviceChair({...ctx,r,hurry:false},boardingYaw,null,true);await ctx.run({do:'stand'});});
      result.beforeYaw=s.yaw;result.phase='seated_turn';
      ctx.say('她坐在椅子上，转了个方向');
      result.afterYaw=await turnDeviceChair(ctx,0,null,true);
      await ctx.wait(2,{hurry:false});
      result.phase='standing_up';await ctx.run({do:'stand'});finished=true;
      result.phase='complete';result.ok=true;result.overturn=false;result.method='seated-swivel';
      ctx.life.actionDetail={text:'已在小窝坐着转动椅子并安全离开；这是水平转向，不是翻倒或站立手推。',scope:'room-animation',overturn:false};
    },
  };
}
