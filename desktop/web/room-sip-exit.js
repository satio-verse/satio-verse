// Time parameterization only. Actual joint limits, contact and filtering remain downstream.
export const SIP_EXIT_SECONDS = 3;
export function sipExitWeight(elapsed, duration = SIP_EXIT_SECONDS) {
  if (!Number.isFinite(elapsed) || !Number.isFinite(duration) || duration <= 0) throw new TypeError('Invalid sip exit time');
  const t = Math.max(0, Math.min(1, elapsed / duration));
  return 1 - t * t * t * (10 + t * (-15 + 6 * t));
}
