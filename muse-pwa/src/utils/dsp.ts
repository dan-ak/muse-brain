// src/utils/dsp.ts

export const SAMPLE_RATE = 256;
export const WINDOW_SIZE = 512; // 2 seconds of data for FFT window
export const CHANNELS = 4;

export const BANDS = {
  delta: [0.5, 4] as [number, number],
  theta: [4, 8] as [number, number],
  alpha: [8, 13] as [number, number],
  beta: [13, 30] as [number, number],
  gamma: [30, 100] as [number, number],
};

export interface BandPowers {
  delta: number;
  theta: number;
  alpha: number;
  beta: number;
  gamma: number;
}

// Sanitize and interpolate data to fill in NaNs or missing samples
export function sanitizeAndInterpolate(data: number[]): number[] {
  const validData = data.filter(
    (val) => val !== null && !isNaN(val) && typeof val === "number"
  );
  if (validData.length === 0) return new Array(data.length).fill(0);

  let lastValidValue = validData[0];
  return data.map((val) => {
    if (val !== null && !isNaN(val) && typeof val === "number") {
      lastValidValue = val;
      return val;
    }
    return lastValidValue;
  });
}

// Calculate power spectrum using Discrete Fourier Transform (DFT)
export function calculatePowerSpectrum(data: number[]): number[] {
  const n = data.length;
  const spectrum = new Array(n / 2).fill(0);

  for (let k = 0; k < n / 2; k++) {
    let real = 0;
    let imag = 0;
    for (let t = 0; t < n; t++) {
      const angle = (2 * Math.PI * t * k) / n;
      real += data[t] * Math.cos(angle);
      imag -= data[t] * Math.sin(angle);
    }
    spectrum[k] = (real * real + imag * imag) / n;
  }

  return spectrum;
}

// Mains hum leaks in when an electrode is not making proper skin contact. It is
// a contact problem, not a fit problem, and it needs a different remedy — wet
// the pad and clear hair — so it is worth reporting separately from signal
// amplitude. In a real recording the forehead sensors sat around 25-35x while
// poorly-seated ear clips reached 1600-7500x, so the two are far apart.
//
// Both mains frequencies are checked and the worse one wins: this rig is
// developed at 50 Hz in Europe and will be used at 60 Hz in the US, and nobody
// wants to remember to change a constant before travelling.
export const MAINS_FREQUENCIES = [50, 60];

// Above this, a channel is hum rather than EEG. Set well clear of the ~35x seen
// on healthy contacts so ordinary variation does not trip it.
export const MAINS_RATIO_BAD = 100;

/** Power at mains frequency relative to the surrounding broadband floor.
 *
 * Returns 1 when there is nothing to compare against, so a silent channel reads
 * as clean rather than alarming — a dead electrode is already reported as
 * disconnected and does not need a second complaint.
 */
export function mainsRatio(powerSpectrumData: number[]): number {
  const freqResolution = SAMPLE_RATE / WINDOW_SIZE;
  const bin = (hz: number) => Math.round(hz / freqResolution);

  // Median of 30-45 Hz as the reference floor: high enough to sit above the
  // EEG bands, and a median rather than a mean so the hum peak itself cannot
  // inflate the very baseline it is being measured against.
  const floorBins = powerSpectrumData
    .slice(bin(30), bin(45))
    .filter((v) => Number.isFinite(v) && v > 0)
    .sort((a, b) => a - b);
  if (floorBins.length === 0) return 1;
  const floor = floorBins[Math.floor(floorBins.length / 2)];
  if (!(floor > 0)) return 1;

  let worst = 0;
  for (const hz of MAINS_FREQUENCIES) {
    // A 1 Hz window either side, since mains drifts and generators wander.
    const lo = Math.max(0, bin(hz - 1));
    const hi = Math.min(powerSpectrumData.length - 1, bin(hz + 1));
    for (let i = lo; i <= hi; i++) {
      const v = powerSpectrumData[i];
      if (Number.isFinite(v) && v > worst) worst = v;
    }
  }

  return worst / floor;
}

// Sum the power within each frequency band
export function powerByBand(powerSpectrumData: number[]): BandPowers {
  const result = {
    delta: 0,
    theta: 0,
    alpha: 0,
    beta: 0,
    gamma: 0,
  };
  const freqResolution = SAMPLE_RATE / WINDOW_SIZE; // e.g. 256 / 512 = 0.5 Hz per bin

  for (const [bandName, [low, high]] of Object.entries(BANDS)) {
    const lowIndex = Math.max(1, Math.floor(low / freqResolution));
    const highIndex = Math.min(
      powerSpectrumData.length - 1,
      Math.ceil(high / freqResolution)
    );
    const bandPower = powerSpectrumData
      .slice(lowIndex, highIndex)
      .reduce((a, b) => a + b, 0);
    
    result[bandName as keyof BandPowers] = bandPower;
  }

  return result;
}
