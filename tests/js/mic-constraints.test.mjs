import { test } from 'node:test';
import assert from 'node:assert/strict';
import { micConstraints, micSettings } from '../../web/audio.js';

// 页面取麦克风时要浏览器做哪些处理 (服务端在 hello 的 mic 里给, 见 wm/echo.py 的 mic_processing).
test('without a hint the browser keeps all three on, as before', () => {
  assert.deepEqual(micConstraints(), { audio: { channelCount: 1, echoCancellation: true, noiseSuppression: true, autoGainControl: true } });
  assert.deepEqual(micConstraints(null).audio, micConstraints().audio);
});

test('the server hint turns noise suppression and auto gain off, keeps echo cancellation', () => {
  const a = micConstraints({ echoCancellation: true, noiseSuppression: false, autoGainControl: false }).audio;
  assert.equal(a.echoCancellation, true);
  assert.equal(a.noiseSuppression, false);
  assert.equal(a.autoGainControl, false);
  assert.equal(a.channelCount, 1);
});

test('echo cancellation may be a mode string; junk values are ignored', () => {
  assert.equal(micConstraints({ echoCancellation: 'all' }).audio.echoCancellation, 'all');
  const a = micConstraints({ noiseSuppression: 'no', autoGainControl: 0, deviceId: 'x' }).audio;
  assert.equal(a.noiseSuppression, true);
  assert.equal(a.autoGainControl, true);
  assert.equal(a.deviceId, undefined);
});

test('settings report only the processing fields the browser actually applied', () => {
  const track = { getSettings: () => ({ echoCancellation: true, noiseSuppression: false, autoGainControl: false,
                                        sampleRate: 48000, channelCount: 1, deviceId: 'secret-id', groupId: 'g' }) };
  assert.deepEqual(micSettings(track), { echoCancellation: true, noiseSuppression: false, autoGainControl: false,
                                         sampleRate: 48000, channelCount: 1 });
  assert.deepEqual(micSettings(null), {});
  assert.deepEqual(micSettings({}), {});
});
