/** 以米输入的动作高度分类. GLB float32 边界/中心相减会把名义 0.6 m 变成 0.599999994 m;
 * 仅给阈值 10 微米的舍入容差, 不把真实的毫米高度差吞掉. 站位预检与执行必须共用. */
export function handActionBand(height) {
  const eps = 1e-5;
  return height < 0.6 - eps ? 'low' : height < 1.0 - eps ? 'middle' : 'high';
}
