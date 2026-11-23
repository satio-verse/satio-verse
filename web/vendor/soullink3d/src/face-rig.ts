import * as THREE from 'three';
import { VRMLookAtBoneApplier, type VRM } from '@pixiv/three-vrm';
import { headLimits, eyeLimits, type HeadAngles, type EyeAngles } from './directions';

export interface RigCapabilities {
  head: boolean;
  leftEye: boolean;
  rightEye: boolean;
  eyeControl: 'bone-authored-range' | 'expression-approximation' | 'unavailable';
  headLimits: HeadAngles;
  /** Command angles; the imported VRM range maps determine physical rotation. */
  eyeLimits: EyeAngles;
  eyeOutputLimits: { inner: number; outer: number; up: number; down: number } | null;
}
/** Angle commands in character coordinates: +yaw left, +pitch up, +roll tilt left. */
export class FaceRig {
  readonly capabilities: RigCapabilities;
  private faceBasis: THREE.Quaternion;
  private faceBasisInverse: THREE.Quaternion;
  private rotation = new THREE.Quaternion();
  private euler = new THREE.Euler(0, 0, 0, 'YXZ');
  constructor(private vrm: VRM) {
    const leftEye = Boolean(vrm.humanoid.getRawBoneNode('leftEye'));
    const rightEye = Boolean(vrm.humanoid.getRawBoneNode('rightEye'));
    const bone = vrm.lookAt?.applier instanceof VRMLookAtBoneApplier;
    const front = vrm.lookAt?.faceFront ?? new THREE.Vector3(0, 0, vrm.meta.metaVersion === '0' ? -1 : 1);
    this.faceBasis = new THREE.Quaternion().setFromUnitVectors(new THREE.Vector3(0, 0, 1), front.clone().normalize());
    this.faceBasisInverse = this.faceBasis.clone().invert();
    this.capabilities = { head: Boolean(vrm.humanoid.getNormalizedBoneNode('head')), leftEye, rightEye, eyeControl: bone && leftEye && rightEye ? 'bone-authored-range' : vrm.lookAt && !bone ? 'expression-approximation' : 'unavailable', headLimits: { ...headLimits }, eyeLimits: { ...eyeLimits }, eyeOutputLimits: bone ? {
      inner: (vrm.lookAt!.applier as VRMLookAtBoneApplier).rangeMapHorizontalInner.outputScale,
      outer: (vrm.lookAt!.applier as VRMLookAtBoneApplier).rangeMapHorizontalOuter.outputScale,
      up: (vrm.lookAt!.applier as VRMLookAtBoneApplier).rangeMapVerticalUp.outputScale,
      down: (vrm.lookAt!.applier as VRMLookAtBoneApplier).rangeMapVerticalDown.outputScale,
    } : null };
    if (vrm.lookAt) vrm.lookAt.autoUpdate = false;
    // Both inputMaxValue and outputScale belong to the imported model. Keeping them
    // prevents a generic gaze controller from stretching a shallow anime eye socket.
  }
  apply(head: HeadAngles, eyes: EyeAngles) {
    const node = this.vrm.humanoid.getNormalizedBoneNode('head');
    if (node) {
      this.euler.set(-THREE.MathUtils.degToRad(head.pitch), THREE.MathUtils.degToRad(head.yaw), -THREE.MathUtils.degToRad(head.roll), 'YXZ');
      this.rotation.setFromEuler(this.euler);
      node.quaternion.copy(this.faceBasis).multiply(this.rotation).multiply(this.faceBasisInverse);
    }
    if (this.vrm.lookAt) {
      const applier = this.vrm.lookAt.applier;
      if (applier instanceof VRMLookAtBoneApplier) {
        // FaceEngine's standard command extent represents +/-1 gaze, not the eye's
        // physical angle. Map it onto the model's input domain without rewriting
        // its range maps. For asymmetric horizontal maps, the larger domain lets
        // both eyes reach their own authored output cap at full gaze.
        const unit = (value: number, extent: number) => Number.isFinite(value) ? THREE.MathUtils.clamp(value / extent, -1, 1) : 0;
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
  private readBone(name: 'head' | 'leftEye' | 'rightEye') {
    const node = this.vrm.humanoid.getNormalizedBoneNode(name);
    if (!node) return null;
    this.rotation.copy(this.faceBasisInverse).multiply(node.quaternion).multiply(this.faceBasis);
    this.euler.setFromQuaternion(this.rotation, 'YXZ');
    return { yaw: THREE.MathUtils.radToDeg(this.euler.y), pitch: -THREE.MathUtils.radToDeg(this.euler.x), roll: -THREE.MathUtils.radToDeg(this.euler.z) };
  }
  state() { return { capabilities: structuredClone(this.capabilities), head: this.readBone('head'), leftEye: this.readBone('leftEye'), rightEye: this.readBone('rightEye') }; }
}
