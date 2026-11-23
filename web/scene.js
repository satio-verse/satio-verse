// 场景图的可视化 (地面、三面墙、家具、装饰) 和跟拍镜头.
// 家具是程序生成的几何体 (圆角盒、圆柱), 材质用画布生成的纹理 (木地板、墙面、布料、地毯、挂画), 不依赖外部资产.
import * as THREE from 'three';
import { buildGrid } from './nav.js';
import { declaredCollisionParts } from './scene-colliders.js';

const mat = (color, extra = {}) => new THREE.MeshStandardMaterial({ color, roughness: 0.85, metalness: 0, ...extra });

/** 固定种子的伪随机数 (每次生成的纹理一样). */
function rng(seed) {
  let x = seed >>> 0 || 1;
  return () => { x ^= x << 13; x ^= x >>> 17; x ^= x << 5; return ((x >>> 0) % 100000) / 100000; };
}

function canvasTex(w, h, draw, repeat = null) {
  const c = document.createElement('canvas');
  c.width = w;
  c.height = h;
  draw(c.getContext('2d'), w, h);
  const t = new THREE.CanvasTexture(c);
  t.colorSpace = THREE.SRGBColorSpace;
  t.anisotropy = 4;
  if (repeat) { t.wrapS = t.wrapT = THREE.RepeatWrapping; t.repeat.set(...repeat); }
  return t;
}

/** 木地板: 0.18 m 宽的长条, 每块深浅和纹理略有不同, 接缝错开. 一张纹理覆盖 1.8 m x 1.8 m. */
function woodTexture() {
  return canvasTex(1024, 1024, (g, w, h) => {
    const r = rng(42), rows = 10, ph = h / rows;
    for (let j = 0; j < rows; j++) {
      let x = -r() * 400;
      while (x < w) {
        const len = 300 + r() * 360, tone = 0.86 + r() * 0.2;
        g.fillStyle = `rgb(${Math.round(196 * tone)},${Math.round(160 * tone)},${Math.round(122 * tone)})`;
        g.fillRect(x, j * ph, len, ph);
        for (let k = 0; k < 14; k++) {  // 木纹
          g.strokeStyle = `rgba(90,60,35,${0.05 + r() * 0.08})`;
          g.lineWidth = 1 + r() * 2;
          const y = j * ph + r() * ph;
          g.beginPath();
          g.moveTo(x, y);
          g.bezierCurveTo(x + len * 0.3, y + (r() - 0.5) * 8, x + len * 0.7, y + (r() - 0.5) * 8, x + len, y + (r() - 0.5) * 6);
          g.stroke();
        }
        g.fillStyle = 'rgba(70,45,25,0.35)';
        g.fillRect(x, j * ph, 2, ph);
        x += len;
      }
      g.fillStyle = 'rgba(70,45,25,0.35)';
      g.fillRect(0, j * ph, w, 2);
    }
  }, [1, 1]);
}

/** 墙面: 很淡的颗粒和明暗起伏 (乳胶漆质感). */
function plasterTexture(rgb) {
  return canvasTex(512, 512, (g, w, h) => {
    g.fillStyle = `rgb(${rgb})`;
    g.fillRect(0, 0, w, h);
    const r = rng(7);
    for (let i = 0; i < 9000; i++) {
      g.fillStyle = `rgba(${r() > 0.5 ? '255,255,255' : '0,0,0'},${0.012 + r() * 0.02})`;
      g.fillRect(r() * w, r() * h, 1 + r() * 3, 1 + r() * 3);
    }
  }, [3, 2]);
}

/** 布料: 细密的经纬线. */
function fabricTexture(color) {
  return canvasTex(256, 256, (g, w, h) => {
    g.fillStyle = color;
    g.fillRect(0, 0, w, h);
    for (let i = 0; i < w; i += 3) {
      g.fillStyle = `rgba(255,255,255,${i % 6 ? 0.03 : 0.06})`;
      g.fillRect(i, 0, 1, h);
      g.fillStyle = `rgba(0,0,0,${i % 6 ? 0.03 : 0.05})`;
      g.fillRect(0, i, w, 1);
    }
  }, [4, 4]);
}

/** 地毯: 边框 + 中间的菱形花纹. */
function rugTexture() {
  return canvasTex(1024, 820, (g, w, h) => {
    g.fillStyle = '#cdbca5'; g.fillRect(0, 0, w, h);
    g.fillStyle = '#8e5f4a'; g.fillRect(24, 24, w - 48, h - 48);
    g.fillStyle = '#d9c9b1'; g.fillRect(60, 60, w - 120, h - 120);
    g.strokeStyle = 'rgba(142,95,74,0.55)';
    g.lineWidth = 6;
    for (let x = 60; x < w - 60; x += 110) {
      for (let y = 60; y < h - 60; y += 110) {
        g.beginPath(); g.moveTo(x + 55, y + 15); g.lineTo(x + 95, y + 55); g.lineTo(x + 55, y + 95); g.lineTo(x + 15, y + 55); g.closePath(); g.stroke();
      }
    }
    const r = rng(5);
    for (let i = 0; i < 20000; i++) { g.fillStyle = `rgba(0,0,0,${r() * 0.04})`; g.fillRect(r() * w, r() * h, 2, 2); }
  });
}

/** 挂画: 随种子生成的抽象色块 (暖色调, 和房间协调). */
function artTexture(seed) {
  const palette = ['#e8b48a', '#c9785d', '#7a9e9f', '#e9d8a6', '#94a3c4', '#b5838d', '#6d8f6a'];
  return canvasTex(512, 384, (g, w, h) => {
    const r = rng(seed);
    g.fillStyle = '#f4efe6';
    g.fillRect(0, 0, w, h);
    for (let i = 0; i < 6; i++) {
      g.fillStyle = palette[Math.floor(r() * palette.length)];
      g.globalAlpha = 0.75 + r() * 0.25;
      g.beginPath();
      if (r() > 0.5) g.arc(r() * w, r() * h, 40 + r() * 120, 0, Math.PI * 2);
      else g.rect(r() * w * 0.7, r() * h * 0.7, 80 + r() * 200, 60 + r() * 160);
      g.fill();
    }
    g.globalAlpha = 1;
  });
}

/** 电视画面: 黄昏的湖面. */
function screenTexture() {
  return canvasTex(512, 288, (g, w, h) => {
    const sky = g.createLinearGradient(0, 0, 0, h * 0.6);
    sky.addColorStop(0, '#2b3a67'); sky.addColorStop(1, '#f2a65a');
    g.fillStyle = sky; g.fillRect(0, 0, w, h * 0.6);
    g.fillStyle = '#ffd38a'; g.beginPath(); g.arc(w * 0.62, h * 0.55, 34, 0, Math.PI * 2); g.fill();
    const lake = g.createLinearGradient(0, h * 0.6, 0, h);
    lake.addColorStop(0, '#d98b56'); lake.addColorStop(1, '#203050');
    g.fillStyle = lake; g.fillRect(0, h * 0.6, w, h * 0.4);
    g.fillStyle = '#1c2438';
    g.beginPath(); g.moveTo(0, h * 0.6); g.lineTo(w * 0.2, h * 0.42); g.lineTo(w * 0.38, h * 0.6); g.fill();
  });
}

/** 窗外: 天空渐变 + 远处的树影. */
function skyTexture() {
  return canvasTex(256, 256, (c, w, h) => {
    const grad = c.createLinearGradient(0, 0, 0, h);
    grad.addColorStop(0, '#9fd0f5'); grad.addColorStop(1, '#e8f4fb');
    c.fillStyle = grad; c.fillRect(0, 0, w, h);
    c.fillStyle = 'rgba(110,160,110,0.55)';
    for (let i = 0; i < 9; i++) { c.beginPath(); c.arc(i * 32 + 10, h - 20 - (i % 3) * 14, 30 + (i % 4) * 8, 0, Math.PI * 2); c.fill(); }
  });
}

/** 钟面. */
function clockTexture() {
  return canvasTex(256, 256, (c) => {
    c.fillStyle = '#fbf8f2'; c.beginPath(); c.arc(128, 128, 124, 0, Math.PI * 2); c.fill();
    c.strokeStyle = '#3b302a'; c.lineWidth = 10; c.stroke();
    c.lineCap = 'round';
    for (let i = 0; i < 12; i++) {
      const a = (i / 12) * Math.PI * 2;
      c.lineWidth = 6;
      c.beginPath(); c.moveTo(128 + Math.sin(a) * 100, 128 - Math.cos(a) * 100); c.lineTo(128 + Math.sin(a) * 112, 128 - Math.cos(a) * 112); c.stroke();
    }
    c.lineWidth = 8; c.beginPath(); c.moveTo(128, 128); c.lineTo(178, 98); c.stroke();
    c.lineWidth = 5; c.beginPath(); c.moveTo(128, 128); c.lineTo(108, 43); c.stroke();
  });
}

const TEX = {};
const tex = (key, make) => (TEX[key] ??= make());
const fabric = (color) => tex(`fabric-${color}`, () => fabricTexture(color));

function box(w, h, d, material, x, y, z) {
  const m = new THREE.Mesh(new THREE.BoxGeometry(w, h, d), material);
  m.position.set(x, y, z);
  return m;
}

/** 圆角盒 (沙发坐垫、抱枕、柜子): 圆角矩形截面沿 Z 挤出, 边缘倒角. */
function rbox(w, h, d, r, material, x, y, z) {
  r = Math.min(r, w / 2 - 1e-3, h / 2 - 1e-3, d / 2 - 1e-3);
  const s = new THREE.Shape(), a = w / 2 - r, b = h / 2 - r;
  s.moveTo(-a, -h / 2); s.lineTo(a, -h / 2); s.quadraticCurveTo(w / 2, -h / 2, w / 2, -b);
  s.lineTo(w / 2, b); s.quadraticCurveTo(w / 2, h / 2, a, h / 2); s.lineTo(-a, h / 2);
  s.quadraticCurveTo(-w / 2, h / 2, -w / 2, b); s.lineTo(-w / 2, -b); s.quadraticCurveTo(-w / 2, -h / 2, -a, -h / 2);
  const geo = new THREE.ExtrudeGeometry(s, { depth: d - 2 * r, bevelEnabled: true, bevelThickness: r, bevelSize: 0, bevelSegments: 3, curveSegments: 4 });
  geo.translate(0, 0, -(d - 2 * r) / 2);
  const m = new THREE.Mesh(geo, material);
  m.position.set(x, y, z);
  return m;
}

function cyl(rt, rb, h, material, x, y, z, seg = 24) {
  const m = new THREE.Mesh(new THREE.CylinderGeometry(rt, rb, h, seg), material);
  m.position.set(x, y, z);
  return m;
}

const CORNERS = [[-1, -1], [1, -1], [-1, 1], [1, 1]];

/** 给部件标上碰撞里的角色 (见 collisionParts): seat 座面、back 靠背、arm 扶手、soft 软垫 (抱枕)、leg 脚. 不标就是实心. */
const part = (mesh, role) => { mesh.userData.part = role; return mesh; };

function furniture(o) {
  const g = new THREE.Group();
  g.name = o.id;
  g.userData.object = o.id;
  const [cx, cz] = o.box.center, [sx, sz] = o.box.size, h = o.box.height, base = o.box.base || 0;
  const color = new THREE.Color(o.color || '#a0a0a0');
  const main = mat(color), dark = mat(color.clone().multiplyScalar(0.75));
  const wood = mat('#ffffff', { map: tex('wood', woodTexture), roughness: 0.6 });
  switch (o.kind) {
    case 'sofa': {
      // 靠背贴墙 (-X 侧), 座面朝 +X: 底座 + 两块坐垫 + 靠背垫 + 扶手 + 抱枕 + 木脚
      const fab = mat(color, { map: fabric(o.color), roughness: 0.95 });
      const fabDark = mat(color.clone().multiplyScalar(0.82), { map: fabric(o.color), roughness: 0.95 });
      g.add(part(rbox(sx, h * 0.3, sz, 0.04, fabDark, cx, h * 0.22, cz), 'seat'));
      for (const k of [-1, 1]) g.add(part(rbox(sx * 0.7, h * 0.18, sz * 0.4, 0.06, fab, cx + sx * 0.12, h * 0.46, cz + k * sz * 0.2), 'seat'));
      g.add(part(rbox(sx * 0.26, h * 0.62, sz * 0.86, 0.07, fab, cx - sx * 0.35, h * 0.62, cz), 'back'));
      for (const k of [-1, 1]) g.add(part(rbox(sx, h * 0.72, sz * 0.1, 0.05, fabDark, cx, h * 0.38, cz + k * sz * 0.45), 'arm'));
      const p1 = rbox(0.12, 0.36, 0.38, 0.06, mat('#e9d8a6', { map: fabric('#e9d8a6') }), cx - sx * 0.18, h * 0.7, cz - sz * 0.3);
      p1.rotation.z = -0.25;
      const p2 = rbox(0.12, 0.34, 0.36, 0.06, mat('#c9785d', { map: fabric('#c9785d') }), cx - sx * 0.18, h * 0.69, cz + sz * 0.31);
      p2.rotation.set(0.15, 0, -0.3);
      g.add(part(p1, 'soft'), part(p2, 'soft'));
      for (const [dx, dz] of CORNERS) g.add(part(cyl(0.025, 0.018, h * 0.08, mat('#5a4030'), cx + dx * (sx / 2 - 0.06), h * 0.04, cz + dz * (sz / 2 - 0.06), 10), 'leg'));
      break;
    }
    case 'desk': {
      g.add(rbox(sx, 0.04, sz, 0.01, wood, cx, h - 0.02, cz));
      for (const [dx, dz] of CORNERS) g.add(box(0.045, h - 0.04, 0.045, dark, cx + dx * (sx / 2 - 0.05), (h - 0.04) / 2, cz + dz * (sz / 2 - 0.05)));
      g.add(rbox(sx * 0.35, 0.16, sz * 0.85, 0.01, dark, cx + sx * 0.28, h - 0.12, cz));  // 抽屉
      // 桌上: 显示器 + 键盘 + 杯子 + 一摞书 (书的位置避开可拿起的那本)
      const black = mat('#25272b', { roughness: 0.5 });
      g.add(box(0.5, 0.3, 0.02, black, cx - 0.1, h + 0.27, cz - sz * 0.32));
      g.add(new THREE.Mesh(new THREE.PlaneGeometry(0.47, 0.27), mat('#9fb7d6', { emissive: '#4a6a96', emissiveIntensity: 0.5 }))
        .translateX(cx - 0.1).translateY(h + 0.27).translateZ(cz - sz * 0.32 + 0.011));
      g.add(box(0.04, 0.12, 0.04, black, cx - 0.1, h + 0.06, cz - sz * 0.33), box(0.18, 0.012, 0.12, black, cx - 0.1, h + 0.006, cz - sz * 0.33));
      g.add(box(0.36, 0.015, 0.12, mat('#d8d8d8'), cx - 0.1, h + 0.008, cz + 0.02));
      g.add(cyl(0.035, 0.03, 0.09, mat('#e86f51', { roughness: 0.4 }), cx + 0.32, h + 0.045, cz + 0.12, 16));
      ['#5a8fc9', '#c96a5a', '#d8b45a'].forEach((c, i) => g.add(box(0.2, 0.035, 0.26, mat(c), cx + 0.42, h + 0.018 + i * 0.036, cz - 0.12 + i * 0.01)));
      break;
    }
    case 'lamp': {
      g.add(cyl(0.07, 0.08, 0.03, dark, 0, base + 0.015, 0, 20));
      g.add(cyl(0.012, 0.012, h * 0.7, dark, 0, base + h * 0.35, 0, 8));
      const shade = new THREE.Mesh(new THREE.ConeGeometry(0.11, 0.16, 24, 1, true), mat(color, { emissive: color, emissiveIntensity: 0.6, side: THREE.DoubleSide }));
      shade.position.y = base + h * 0.8;
      g.add(shade);
      const light = new THREE.PointLight(0xffe2a8, 1.2, 3, 2);
      light.position.y = base + h * 0.72;
      g.add(light);
      g.position.set(cx, 0, cz);
      return g;
    }
    case 'window': {
      const glass = new THREE.Mesh(new THREE.PlaneGeometry(sx, h), new THREE.MeshBasicMaterial({ map: tex('sky', skyTexture) }));
      glass.position.set(cx, base + h / 2, cz + 0.01);
      g.add(glass);
      const frame = mat('#f5f2ec');
      g.add(box(sx + 0.08, 0.05, 0.06, frame, cx, base, cz + 0.02), box(sx + 0.08, 0.05, 0.06, frame, cx, base + h, cz + 0.02));
      g.add(box(0.05, h, 0.06, frame, cx - sx / 2, base + h / 2, cz + 0.02), box(0.05, h, 0.06, frame, cx + sx / 2, base + h / 2, cz + 0.02));
      g.add(box(0.03, h, 0.04, frame, cx, base + h / 2, cz + 0.02));
      g.add(box(sx + 0.2, 0.03, 0.16, frame, cx, base - 0.02, cz + 0.08));  // 窗台和一小盆绿植
      g.add(cyl(0.05, 0.04, 0.09, mat('#c58a62'), cx + sx * 0.32, base + 0.045, cz + 0.08, 14));
      g.add(new THREE.Mesh(new THREE.SphereGeometry(0.07, 12, 10), mat('#6fa86b')).translateX(cx + sx * 0.32).translateY(base + 0.13).translateZ(cz + 0.08));
      break;
    }
    case 'door': {
      g.add(box(0.05, h, sz, main, cx - 0.02, h / 2, cz));
      const frame = mat('#f0ebe2');
      g.add(box(0.07, 0.06, sz + 0.12, frame, cx - 0.03, h + 0.03, cz), box(0.07, h, 0.06, frame, cx - 0.03, h / 2, cz - sz / 2 - 0.03),
        box(0.07, h, 0.06, frame, cx - 0.03, h / 2, cz + sz / 2 + 0.03));
      for (const y of [0.55, 1.45]) g.add(box(0.01, 0.6, sz * 0.7, dark, cx - 0.05, y, cz));  // 门板凹槽
      g.add(new THREE.Mesh(new THREE.SphereGeometry(0.035, 12, 8), mat('#d9c27a', { metalness: 0.5, roughness: 0.35 }))
        .translateX(cx - 0.07).translateY(1.0).translateZ(cz + sz * 0.38));
      break;
    }
    case 'shelf': {
      // 开放式书架 (开口朝 -X): 侧板 + 背板 + 5 层隔板, 书、小盆栽、收纳盒
      g.add(box(sx, h, 0.03, main, cx, h / 2, cz - sz / 2), box(sx, h, 0.03, main, cx, h / 2, cz + sz / 2), box(0.02, h, sz, dark, cx + sx / 2 - 0.01, h / 2, cz));
      const books = ['#c96a5a', '#5a8fc9', '#d8b45a', '#6aa874', '#8e6ac9', '#e9d8a6', '#3d6bb3'];
      const r = rng(3);
      for (let k = 0; k < 5; k++) {
        const y = 0.04 + (k * (h - 0.04)) / 4.6;
        g.add(box(sx, 0.025, sz, main, cx, y, cz));
        if (k === 4) break;
        let z = cz - sz / 2 + 0.05;
        while (z < cz + sz / 2 - 0.08) {
          const kind = r();
          if (kind < 0.75) {
            const t = 0.025 + r() * 0.025, ht = 0.22 + r() * 0.08;
            const bk = box(sx * 0.7, ht, t, mat(books[Math.floor(r() * books.length)]), cx + 0.03, y + 0.0125 + ht / 2, z + t / 2);
            if (r() < 0.12) bk.rotation.x = 0.25;
            g.add(bk);
            z += t + 0.003;
          } else if (kind < 0.88) {
            g.add(cyl(0.05, 0.04, 0.09, mat('#e8e2d8'), cx + 0.04, y + 0.058, z + 0.07, 14));
            g.add(new THREE.Mesh(new THREE.SphereGeometry(0.075, 12, 10), mat('#6fa86b')).translateX(cx + 0.04).translateY(y + 0.15).translateZ(z + 0.07));
            z += 0.16;
          } else {
            g.add(rbox(sx * 0.75, 0.16, 0.2, 0.01, mat('#c9b8a3'), cx + 0.03, y + 0.093, z + 0.1));
            z += 0.22;
          }
        }
      }
      break;
    }
    case 'plant':
    case 'tallplant': {
      const tall = o.kind === 'tallplant';
      g.add(cyl(tall ? 0.16 : 0.2, tall ? 0.12 : 0.15, tall ? 0.34 : 0.38, mat(tall ? '#e8e2d8' : '#c58a62'), 0, tall ? 0.17 : 0.19, 0, 20));
      const leaf = mat(color, { roughness: 0.7 }), leaf2 = mat(color.clone().multiplyScalar(0.8), { roughness: 0.7 });
      if (tall) {
        g.add(cyl(0.015, 0.02, h - 0.3, mat('#6b4f3a'), 0, 0.3 + (h - 0.3) / 2, 0, 8));
        const r = rng(9);
        for (let i = 0; i < 18; i++) {
          const lf = new THREE.Mesh(new THREE.SphereGeometry(0.09, 10, 8), i % 2 ? leaf : leaf2);
          const a = r() * Math.PI * 2, y = 0.75 + r() * (h - 0.8), rad = 0.08 + r() * 0.12;
          lf.scale.set(1.3, 0.35, 0.7);
          lf.rotation.set(0, a, 0.4);
          lf.position.set(Math.cos(a) * rad, y, Math.sin(a) * rad);
          g.add(lf);
        }
      } else {
        for (const [dx, dy, dz, r] of [[0, 0.75, 0, 0.32], [0.14, 0.95, 0.06, 0.24], [-0.12, 1.02, -0.05, 0.22], [0.02, 1.15, 0.02, 0.18]]) {
          g.add(new THREE.Mesh(new THREE.SphereGeometry(r, 16, 12), leaf).translateX(dx).translateY(dy).translateZ(dz));
        }
      }
      g.position.set(cx, 0, cz);
      return g;
    }
    case 'coffeetable': {
      g.add(rbox(sx, 0.035, sz, 0.012, wood, cx, h - 0.0175, cz));
      g.add(rbox(sx * 0.9, 0.02, sz * 0.9, 0.008, wood, cx, 0.12, cz));  // 下层隔板和杂志
      for (const [dx, dz] of CORNERS) g.add(cyl(0.02, 0.016, h - 0.035, dark, cx + dx * (sx / 2 - 0.05), (h - 0.035) / 2, cz + dz * (sz / 2 - 0.06), 12));
      g.add(box(0.2, 0.025, 0.28, mat('#94a3c4'), cx + 0.02, 0.143, cz + 0.12));
      break;
    }
    case 'tv': {
      g.add(box(0.04, h, sz, mat('#18191c', { roughness: 0.35, metalness: 0.2 }), cx, base + h / 2, cz));
      const screen = new THREE.Mesh(new THREE.PlaneGeometry(sz - 0.04, h - 0.04), new THREE.MeshBasicMaterial({ map: tex('screen', screenTexture) }));
      screen.rotation.y = -Math.PI / 2;
      screen.position.set(cx - 0.021, base + h / 2, cz);
      g.add(screen);
      break;
    }
    case 'console': {
      g.add(rbox(sx, h, sz, 0.015, wood, cx, h / 2, cz));
      for (const k of [-1, 1]) g.add(box(0.01, h * 0.6, sz * 0.42, dark, cx - sx / 2 - 0.002, h * 0.5, cz + k * sz * 0.23));  // 柜门
      g.add(box(0.1, 0.18, 0.1, mat('#e8e2d8'), cx, h + 0.09, cz + sz * 0.42));  // 音箱
      break;
    }
    case 'sidetable': {
      g.add(cyl(sx / 2, sx / 2, 0.03, wood, cx, h - 0.015, cz, 28));
      g.add(cyl(0.025, 0.03, h - 0.03, dark, cx, (h - 0.03) / 2, cz, 12), cyl(0.14, 0.16, 0.02, dark, cx, 0.01, cz, 24));
      g.add(cyl(0.05, 0.06, 0.03, mat('#d8cbb6'), cx, h + 0.015, cz, 16), cyl(0.01, 0.01, 0.26, mat('#b59c7a'), cx, h + 0.16, cz, 8));
      g.add(cyl(0.08, 0.12, 0.16, mat('#f3e3c3', { emissive: '#f2d38b', emissiveIntensity: 0.5, side: THREE.DoubleSide }), cx, h + 0.33, cz, 24));
      const light = new THREE.PointLight(0xffd9a0, 0.9, 2.6, 2);
      light.position.set(cx, h + 0.3, cz);
      g.add(light);
      break;
    }
    case 'chair': {
      const seat = mat(color, { map: fabric(o.color) }), steel = mat('#3a3d42', { metalness: 0.4, roughness: 0.5 });
      g.add(part(rbox(sx, 0.07, sz, 0.025, seat, cx, 0.46, cz), 'seat'), part(rbox(sx, 0.38, 0.06, 0.025, seat, cx, 0.72, cz + sz / 2 - 0.03), 'back'));
      g.add(cyl(0.025, 0.025, 0.36, steel, cx, 0.25, cz, 10));
      for (let i = 0; i < 5; i++) {
        const a = (i / 5) * Math.PI * 2;
        const leg = box(0.03, 0.03, 0.24, steel, cx + Math.sin(a) * 0.11, 0.05, cz + Math.cos(a) * 0.11);
        leg.rotation.y = a;
        g.add(leg);
      }
      break;
    }
    case 'rug': {
      const rug = new THREE.Mesh(new THREE.PlaneGeometry(sx, sz), mat('#ffffff', { map: tex('rug', rugTexture), roughness: 1 }));
      rug.rotation.x = -Math.PI / 2;
      rug.position.set(cx, 0.004, cz);
      g.add(rug);
      break;
    }
    case 'art': {
      const onLeft = o.wall === 'left', onBack = o.wall === 'back';
      const w = onBack ? sx : sz;
      g.add(box(onBack ? w + 0.06 : 0.03, h + 0.06, onBack ? 0.03 : w + 0.06, mat('#3b302a'), cx, base + h / 2, cz));
      const pic = new THREE.Mesh(new THREE.PlaneGeometry(w, h), mat('#ffffff', { map: artTexture(o.seed || 1), roughness: 0.9 }));
      pic.position.set(cx + (onLeft ? 0.017 : onBack ? 0 : -0.017), base + h / 2, cz + (onBack ? 0.017 : 0));
      pic.rotation.y = onLeft ? Math.PI / 2 : onBack ? 0 : -Math.PI / 2;
      g.add(pic);
      break;
    }
    case 'clock': {
      const disc = new THREE.Mesh(new THREE.CircleGeometry(sz / 2, 40), mat('#ffffff', { map: tex('clock', clockTexture), roughness: 0.6 }));
      disc.rotation.y = -Math.PI / 2;
      disc.position.set(cx - 0.02, base + h / 2, cz);
      g.add(disc);
      break;
    }
    case 'curtain': {
      // 垂坠的布: 一排竖向褶皱 + 窗帘杆
      const cloth = mat(color, { map: fabric(o.color), roughness: 0.95 });
      const folds = 7;
      for (let i = 0; i < folds; i++) {
        const c = cyl(sx / folds / 1.4, sx / folds / 1.2, h, cloth, cx - sx / 2 + (i + 0.5) * (sx / folds), base + h / 2, cz, 10);
        c.scale.z = 0.6;
        g.add(c);
      }
      const rod = cyl(0.012, 0.012, 0.6, mat('#8a7a66', { metalness: 0.4 }), cx, base + h + 0.04, cz, 8);
      rod.rotation.z = Math.PI / 2;
      g.add(rod);
      break;
    }
    default:
      g.add(box(sx, h, sz, main, cx, base + h / 2, cz));
  }
  return g;
}

/**
 * 家具的碰撞体: 按房间里实际搭出来的每个部件 (桌面、桌腿、坐垫、靠背、扶手、书……) 各取一个包围盒, 按物体分组.
 * 以前每件家具只有场景 JSON 里的一个实心包围盒: 茶几、书桌底下是空的也算实心, 坐到沙发上整件沙发都不算.
 * 新的家具、新的场景不用另外写碰撞体, 搭出来的样子就是碰撞的样子; 部件的角色 (座面、靠背……) 见 part().
 * 不算的: 墙上的东西、地毯这类薄的、能拿起的东西、虚拟目标, 以及平面 (屏幕、画、钟面).
 * 返回 [{id, min, max, parts: [{part, min, max}]}] (物体的 min/max 是部件的总包围盒, 用来粗筛).
 */
export function collisionParts(room, scene) {
  const declared = declaredCollisionParts(scene);
  if (declared !== null) return declared;
  room.updateMatrixWorld(true);
  const keep = new Map([...scene.objects, ...(scene.decor || [])]
    .filter((o) => o.box && !o.virtual && !o.wall && !o.item && o.box.height >= 0.05).map((o) => [o.id, o]));
  const out = [], box = new THREE.Box3();
  room.traverse((g) => {
    if (!keep.has(g.userData.object)) return;
    const parts = [];
    g.traverse((m) => {
      if (!m.isMesh || m.geometry.type === 'PlaneGeometry' || m.geometry.type === 'CircleGeometry') return;
      box.setFromObject(m);
      parts.push({ part: m.userData.part || 'solid', min: box.min.toArray(), max: box.max.toArray() });
    });
    if (!parts.length) return;
    const min = [0, 1, 2].map((k) => Math.min(...parts.map((p) => p.min[k])));
    const max = [0, 1, 2].map((k) => Math.max(...parts.map((p) => p.max[k])));
    out.push({ id: g.userData.object, min, max, parts });
  });
  return out;
}

/** 由场景 JSON 生成房间: 地板、三面墙 (前方开口给镜头)、踢脚线和顶角线、家具和装饰. */
export function buildRoom(scene) {
  const group = new THREE.Group();
  group.name = 'room';
  const xs = scene.floor.map((p) => p[0]), zs = scene.floor.map((p) => p[1]);
  const [x0, x1, z0, z1] = [Math.min(...xs), Math.max(...xs), Math.min(...zs), Math.max(...zs)];
  const W = x1 - x0, D = z1 - z0, H = scene.wallHeight || 2.6;
  const woodTex = tex('wood', woodTexture).clone();
  woodTex.repeat.set(W / 1.8, D / 1.8);
  woodTex.needsUpdate = true;
  const floor = new THREE.Mesh(new THREE.PlaneGeometry(W, D), mat('#ffffff', { map: woodTex, roughness: 0.55 }));
  floor.rotation.x = -Math.PI / 2;
  floor.position.set((x0 + x1) / 2, 0, (z0 + z1) / 2);
  group.add(floor);
  const wall = mat('#ffffff', { map: tex('plaster', () => plasterTexture('238,232,224')), side: THREE.DoubleSide, roughness: 0.95 });
  const accent = mat('#ffffff', { map: tex('plaster-accent', () => plasterTexture('203,214,206')), side: THREE.DoubleSide, roughness: 0.95 });
  const trim = mat('#f3eee6');
  const back = new THREE.Mesh(new THREE.PlaneGeometry(W, H), wall);
  back.position.set((x0 + x1) / 2, H / 2, z0);
  const left = new THREE.Mesh(new THREE.PlaneGeometry(D, H), accent);  // 沙发后面的墙刷成浅灰绿, 房间有层次
  left.rotation.y = Math.PI / 2;
  left.position.set(x0, H / 2, (z0 + z1) / 2);
  const right = new THREE.Mesh(new THREE.PlaneGeometry(D, H), wall);
  right.rotation.y = -Math.PI / 2;
  right.position.set(x1, H / 2, (z0 + z1) / 2);
  group.add(back, left, right);
  for (const [y, hh] of [[0.05, 0.1], [H - 0.04, 0.08]]) {  // 踢脚线、顶角线
    group.add(box(W, hh, 0.025, trim, (x0 + x1) / 2, y, z0 + 0.012));
    group.add(box(0.025, hh, D, trim, x0 + 0.012, y, (z0 + z1) / 2), box(0.025, hh, D, trim, x1 - 0.012, y, (z0 + z1) / 2));
  }
  for (const o of [...scene.objects, ...(scene.decor || [])]) if (o.box) group.add(furniture(o));
  // 阴影: 墙、地板、地毯、挂画只接收; 家具既投射也接收
  const flat = new Set([floor, back, left, right]);
  group.traverse((m) => {
    if (!m.isMesh) return;
    m.receiveShadow = true;
    m.castShadow = !flat.has(m) && m.geometry.type !== 'PlaneGeometry' && m.geometry.type !== 'CircleGeometry';
  });
  return group;
}

/** 调试用: 把可行走格子画成半透明点阵. */
export function buildNavDebug(scene) {
  const g = buildGrid(scene);
  const pts = [];
  for (let j = 0; j < g.h; j++) for (let i = 0; i < g.w; i++) if (g.open[j * g.w + i]) pts.push(g.x0 + (i + 0.5) * g.cell, 0.01, g.z0 + (j + 0.5) * g.cell);
  const geo = new THREE.BufferGeometry();
  geo.setAttribute('position', new THREE.Float32BufferAttribute(pts, 3));
  const p = new THREE.Points(geo, new THREE.PointsMaterial({ color: 0x4b63d8, size: 0.03, transparent: true, opacity: 0.5 }));
  p.visible = false;
  return p;
}

/** 脚下的柔和阴影, 让角色"站"在地上. */
export function contactShadow() {
  const c = document.createElement('canvas');
  c.width = c.height = 64;
  const ctx = c.getContext('2d');
  const grad = ctx.createRadialGradient(32, 32, 0, 32, 32, 32);
  grad.addColorStop(0, 'rgba(0,0,0,0.35)');
  grad.addColorStop(1, 'rgba(0,0,0,0)');
  ctx.fillStyle = grad;
  ctx.fillRect(0, 0, 64, 64);
  const m = new THREE.Mesh(new THREE.PlaneGeometry(0.9, 0.9),
    new THREE.MeshBasicMaterial({ map: new THREE.CanvasTexture(c), transparent: true, depthWrite: false }));
  m.rotation.x = -Math.PI / 2;
  m.position.y = 0.006;
  return m;
}

/**
 * 跟拍镜头: 近景 (从用户方向看角色, 和原来的取景一致) 与全景 (房间斜前方俯视) 之间平滑切换.
 * 自动模式下, 角色在走动、离开用户站位或背对用户时用全景, 停在用户面前时回到近景.
 */
export class CameraRig {
  constructor(camera, scene) {
    this.camera = camera;
    this.scene = scene;
    this.mode = 'auto';
    this.t = 1;               // 0 = 近景, 1 = 全景
    this.pos = camera.position.clone();
    this.look = new THREE.Vector3();
    this.fov = camera.fov;
  }

  update(dt, { char, portrait, active, userSpot }) {
    let goal = this.mode === 'portrait' ? 0 : this.mode === 'room' ? 1 : 0;
    if (this.mode === 'auto') {
      const far = Math.hypot(char.x - userSpot[0], char.z - userSpot[1]) > 0.6;
      const away = Math.abs(Math.atan2(Math.sin(char.yaw), Math.cos(char.yaw))) > 1.0;
      goal = active || far || away ? 1 : 0;
    }
    this.t += (goal - this.t) * (1 - Math.exp(-dt * (goal > this.t ? 3.5 : 1.6)));
    // 全景: 从用户一侧斜上方跟拍, 横向往房间中线收一点; 舞台很窄 (竖屏) 时拉远, 保证横向能看到约 3.2 米
    const aspect = this.camera.aspect || 1;
    const fovWide = 38, tan = Math.tan((fovWide * Math.PI) / 360);
    const back = Math.max(3.6, 3.2 / (2 * tan * aspect) * 0.75);
    const near = new THREE.Vector3(char.x, portrait.cy + 0.03, char.z + portrait.dist);
    const nearLook = new THREE.Vector3(char.x, portrait.cy, char.z);
    const wide = new THREE.Vector3(char.x * 0.75, 1.2 + back * 0.32, char.z + back);
    const wideLook = new THREE.Vector3(char.x * 0.9, 0.85, char.z - 0.2);
    const e = this.t * this.t * (3 - 2 * this.t);
    const k = 1 - Math.exp(-dt * 6);
    this.pos.lerp(near.lerp(wide, e), k);
    this.look.lerp(nearLook.lerp(wideLook, e), k);
    this.fov += (portrait.fov + (fovWide - portrait.fov) * e - this.fov) * k;
    this.camera.position.copy(this.pos);
    if (Math.abs(this.camera.fov - this.fov) > 1e-3) { this.camera.fov = this.fov; this.camera.updateProjectionMatrix(); }
    this.camera.lookAt(this.look);
  }
}
