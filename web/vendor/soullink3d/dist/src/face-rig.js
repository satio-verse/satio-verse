// 由 tools/build_soullink.mjs 从 src/face-rig.ts 生成, 不要手改. 来源: SoullinkEmotion3D (Apache-2.0), 内含 soullink-emotion-sdk @ 02cd75ac.
import * as THREE from 'three';
import { VRMLookAtBoneApplier } from '@pixiv/three-vrm';
import { headLimits, eyeLimits } from './directions.js';
export class FaceRig {
    vrm;
    capabilities;
    faceBasis;
    faceBasisInverse;
    rotation = new THREE.Quaternion();
    euler = new THREE.Euler(0, 0, 0, 'YXZ');
    constructor(vrm){
        this.vrm = vrm;
        const leftEye = Boolean(vrm.humanoid.getRawBoneNode('leftEye'));
        const rightEye = Boolean(vrm.humanoid.getRawBoneNode('rightEye'));
        const bone = vrm.lookAt?.applier instanceof VRMLookAtBoneApplier;
        const front = vrm.lookAt?.faceFront ?? new THREE.Vector3(0, 0, vrm.meta.metaVersion === '0' ? -1 : 1);
        this.faceBasis = new THREE.Quaternion().setFromUnitVectors(new THREE.Vector3(0, 0, 1), front.clone().normalize());
        this.faceBasisInverse = this.faceBasis.clone().invert();
        this.capabilities = {
            head: Boolean(vrm.humanoid.getNormalizedBoneNode('head')),
            leftEye,
            rightEye,
            eyeControl: bone && leftEye && rightEye ? 'bone-authored-range' : vrm.lookAt && !bone ? 'expression-approximation' : 'unavailable',
            headLimits: {
                ...headLimits
            },
            eyeLimits: {
                ...eyeLimits
            },
            eyeOutputLimits: bone ? {
                inner: vrm.lookAt.applier.rangeMapHorizontalInner.outputScale,
                outer: vrm.lookAt.applier.rangeMapHorizontalOuter.outputScale,
                up: vrm.lookAt.applier.rangeMapVerticalUp.outputScale,
                down: vrm.lookAt.applier.rangeMapVerticalDown.outputScale
            } : null
        };
        if (vrm.lookAt) vrm.lookAt.autoUpdate = false;
    }
    apply(head, eyes) {
        const node = this.vrm.humanoid.getNormalizedBoneNode('head');
        if (node) {
            this.euler.set(-THREE.MathUtils.degToRad(head.pitch), THREE.MathUtils.degToRad(head.yaw), -THREE.MathUtils.degToRad(head.roll), 'YXZ');
            this.rotation.setFromEuler(this.euler);
            node.quaternion.copy(this.faceBasis).multiply(this.rotation).multiply(this.faceBasisInverse);
        }
        if (this.vrm.lookAt) {
            const applier = this.vrm.lookAt.applier;
            if (applier instanceof VRMLookAtBoneApplier) {
                const unit = (value, extent)=>Number.isFinite(value) ? THREE.MathUtils.clamp(value / extent, -1, 1) : 0;
                const yaw = unit(eyes.yaw, eyeLimits.yaw), pitch = unit(eyes.pitch, eyeLimits.pitch);
                const horizontal = Math.max(applier.rangeMapHorizontalInner.inputMaxValue, applier.rangeMapHorizontalOuter.inputMaxValue);
                const vertical = pitch >= 0 ? applier.rangeMapVerticalDown : applier.rangeMapVerticalUp;
                this.vrm.lookAt.yaw = yaw * horizontal;
                this.vrm.lookAt.pitch = -pitch * vertical.inputMaxValue;
            } else {
                this.vrm.lookAt.yaw = eyes.yaw;
                this.vrm.lookAt.pitch = -eyes.pitch;
            }
        }
    }
    readBone(name) {
        const node = this.vrm.humanoid.getNormalizedBoneNode(name);
        if (!node) return null;
        this.rotation.copy(this.faceBasisInverse).multiply(node.quaternion).multiply(this.faceBasis);
        this.euler.setFromQuaternion(this.rotation, 'YXZ');
        return {
            yaw: THREE.MathUtils.radToDeg(this.euler.y),
            pitch: -THREE.MathUtils.radToDeg(this.euler.x),
            roll: -THREE.MathUtils.radToDeg(this.euler.z)
        };
    }
    state() {
        return {
            capabilities: structuredClone(this.capabilities),
            head: this.readBone('head'),
            leftEye: this.readBone('leftEye'),
            rightEye: this.readBone('rightEye')
        };
    }
}
