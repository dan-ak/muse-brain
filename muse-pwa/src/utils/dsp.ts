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
