/* Same-origin AudioWorklet module. Do not inline as a blob: URL: deployments
   can allow script-src 'self' while correctly refusing blob: scripts. */
'use strict';
class PcmTap extends AudioWorkletProcessor {
  constructor(options) {
    super();
    this.channels = options.processorOptions?.channels === 2 ? 2 : 1;
  }
  process(inputs) {
    const input = inputs[0];
    if (input && input[0]) {
      const left = input[0], right = input[1] || left;
      const out = new Float32Array(left.length * this.channels);
      for (let i = 0; i < left.length; i++) {
        if (this.channels === 2) {
          out[i * 2] = left[i];
          out[i * 2 + 1] = right[i];
        } else {
          out[i] = (left[i] + right[i]) * 0.5;
        }
      }
      this.port.postMessage(out, [out.buffer]);
    }
    return true;
  }
}
registerProcessor('pcm-tap', PcmTap);
