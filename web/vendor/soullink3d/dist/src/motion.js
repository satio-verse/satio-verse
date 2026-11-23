// 由 tools/build_soullink.mjs 从 src/motion.ts 生成, 不要手改. 来源: SoullinkEmotion3D (Apache-2.0), 内含 soullink-emotion-sdk @ 02cd75ac.
const bound = (n, lo, hi, fallback)=>Math.min(hi, Math.max(lo, Number.isFinite(n) ? n : fallback));
export function emotionMotionFactors(vad) {
    const a = bound(vad.arousal, -1, 1, 0), d = bound(vad.dominance, -1, 1, 0);
    const strength = Math.min(1, Math.hypot(vad.valence, a, d) / Math.sqrt(3));
    return {
        amplitude: bound(1 + .3 * a + .15 * d + .1 * strength, .45, 1.55, 1),
        speed: bound(1 + .25 * a + .1 * d, .5, 1.5, 1)
    };
}
export function resolveAction(name, options, settings, vad) {
    if (![
        'nod',
        'shake',
        'blink'
    ].includes(name)) throw new Error(`Unknown action: ${name}`);
    const linked = options.emotion ?? settings.emotionMotion;
    const factors = linked ? emotionMotionFactors(vad) : {
        amplitude: 1,
        speed: 1
    };
    const amplitude = bound(options.amplitude ?? (name === 'blink' && linked ? 1 : settings.motionAmplitude) * factors.amplitude, 0, 1, 0);
    const speed = bound(options.speed ?? (options.duration !== undefined ? 1 : settings.motionSpeed * factors.speed), .25, 3, 1);
    const duration = bound(options.duration ?? (name === 'blink' ? .23 : settings.motionDuration), name === 'blink' ? .08 : .25, 8, 1.1);
    const cycleDuration = bound(duration / speed, name === 'blink' ? .06 : .15, 12, 1.1);
    const repetitions = Math.round(bound(options.repetitions ?? settings.motionRepetitions, 1, 6, 1));
    return {
        name,
        amplitude,
        cycleDuration,
        repetitions,
        totalDuration: cycleDuration * repetitions,
        emotionLinked: linked && (options.amplitude === undefined || options.duration === undefined && options.speed === undefined)
    };
}
export function damp(value, velocity, target, seconds, dt) {
    const omega = 4.75 / Math.max(.04, seconds), offset = value - target;
    const c = velocity + omega * offset, e = Math.exp(-omega * dt);
    return {
        value: target + (offset + c * dt) * e,
        velocity: (velocity - omega * c * dt) * e
    };
}
function ease(t) {
    return t * t * t * (10 + t * (-15 + t * 6));
}
function easeDerivative(t) {
    return 30 * t * t * (1 - t) * (1 - t);
}
function wave(name, phase) {
    const points = name === 'shake' ? [
        [
            0,
            0
        ],
        [
            .25,
            -1
        ],
        [
            .75,
            1
        ],
        [
            1,
            0
        ]
    ] : [
        [
            0,
            0
        ],
        [
            name === 'blink' ? .4 : .5,
            1
        ],
        [
            1,
            0
        ]
    ];
    for(let i = 1; i < points.length; i++){
        const [left, from] = points[i - 1], [right, to] = points[i];
        if (phase <= right) {
            const t = bound((phase - left) / (right - left), 0, 1, 0);
            return {
                value: from + (to - from) * ease(t),
                velocity: (to - from) * easeDerivative(t) / (right - left)
            };
        }
    }
    return {
        value: 0,
        velocity: 0
    };
}
export class MotionTimeline {
    clips = new Map();
    sampleClip(clip, now) {
        const elapsed = Math.max(0, now - clip.start), { resolved } = clip;
        let value = 0, velocity = 0;
        if (!clip.stop && elapsed < resolved.totalDuration) {
            const phase = elapsed / resolved.cycleDuration % 1, w = wave(resolved.name, phase);
            value = w.value * resolved.amplitude;
            velocity = w.velocity * resolved.amplitude / resolved.cycleDuration;
        }
        if (elapsed < clip.blend) {
            const t = elapsed / clip.blend;
            value += (2 * t ** 3 - 3 * t ** 2 + 1) * clip.from + (t ** 3 - 2 * t ** 2 + t) * clip.blend * clip.velocity;
            velocity += (6 * t ** 2 - 6 * t) * clip.from / clip.blend + (3 * t ** 2 - 4 * t + 1) * clip.velocity;
        }
        const limited = bound(value, resolved.name === 'blink' ? 0 : -1, 1, 0);
        return {
            value: limited,
            velocity: limited === value ? velocity : 0
        };
    }
    play(resolved, now) {
        const previous = this.clips.get(resolved.name);
        const from = previous ? this.sampleClip(previous, now) : {
            value: 0,
            velocity: 0
        };
        this.clips.set(resolved.name, {
            resolved,
            start: now,
            from: from.value,
            velocity: from.velocity,
            blend: Math.min(.25, resolved.cycleDuration * .35)
        });
        return {
            ...resolved
        };
    }
    stop(now) {
        for (const [name, clip] of this.clips){
            const from = this.sampleClip(clip, now);
            this.clips.set(name, {
                resolved: {
                    ...clip.resolved,
                    amplitude: 0,
                    totalDuration: .25
                },
                start: now,
                from: from.value,
                velocity: from.velocity,
                blend: .25,
                stop: true
            });
        }
    }
    reset() {
        this.clips.clear();
    }
    clear(name) {
        this.clips.delete(name);
    }
    sample(now) {
        const state = [];
        for (const [name, clip] of this.clips){
            const duration = Math.max(clip.resolved.totalDuration, clip.blend);
            if (now - clip.start >= duration - 1e-9) {
                this.clips.delete(name);
                continue;
            }
            state.push({
                ...clip.resolved,
                progress: bound((now - clip.start) / duration, 0, 1, 0),
                ...this.sampleClip(clip, now)
            });
        }
        return state;
    }
}
