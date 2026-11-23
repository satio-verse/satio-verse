// 模型能力表 (数据, 不是逻辑): 面部动作单元 (AU) -> 标准 Live2D 参数, 情绪 -> 姿势 (表情文件) 的默认对应.
// 换模型 / 加模型时只改这里 (或模型自己的 pet-model.json 的 emotions); 模型里没有的参数, 表情层载入时会去掉并留记录 (见 expression-layer.js 的 report).
// 参数名是 Cubism 标准名 (Eustia 的 49 个参数里这些都有). 系数的符号: 眼睛开合 -1 = 合上; 眉毛 Y +1 = 抬眉.

/** 8 种情绪 (服务端 wm/intent.py 的 EMOTIONS). */
export const EMOTION_LABELS = ['neutral', 'happy', 'excited', 'calm', 'sad', 'shy', 'angry', 'surprised'];

/** AU 名 -> [[参数名, 系数], ...]. 强度 0..1 乘系数后以"加"的方式叠到参数上 (和表情文件的 Add 同一个语义). */
export const AU_TABLE = {
  eyeClose: [['ParamEyeLOpen', -1], ['ParamEyeROpen', -1]],     // 眼皮下垂、闭眼
  eyeWide: [['ParamEyeLOpen', 0.25], ['ParamEyeROpen', 0.25]],  // 睁大
  browSoft: [['ParamBrowLY', -0.35], ['ParamBrowRY', -0.35]],   // 眉头放松、微微下压
  browUp: [['ParamBrowLY', 1], ['ParamBrowRY', 1]],             // 扬眉 (au01 / au02)
  mouthOpen: [['ParamMouthOpenY', 1]],                          // 嘴微张 (吸气)
  mouthYawn: [['ParamMouthOpenY', 0.9]],                        // 哈欠
  cheek: [['ParamCheek', 1]],                                   // 脸红
  breathDeep: [['ParamBreath', 1]],                             // 深呼吸 / 叹气
};

/**
 * 每个模型的情绪 -> 姿势权重 ({姿势名: 权重}). pet-model.json 的 emotions 写了就以它为准 (字符串 = 一个姿势权重 1);
 * 这里是它没写的情绪的后备. 没有对应 = 平静 (收掉表情). Eustia 的外观与改前一致, 所以这里不放东西.
 */
export const MODEL_CAPS = {
  Eustia: { emotions: {} },
};

/** 合并 pet-model.json 的 emotions 和 MODEL_CAPS, 返回 {标签: {姿势名: 权重}} (8 个标签都有, 没有姿势的是 {}). */
export function emotionTable(pet) {
  const base = MODEL_CAPS[pet?.name]?.emotions || {};
  const out = {};
  for (const label of EMOTION_LABELS) {
    const v = pet?.emotions?.[label] ?? base[label];
    out[label] = typeof v === 'string' ? { [v]: 1 } : v && typeof v === 'object' ? { ...v } : {};
  }
  return out;
}
